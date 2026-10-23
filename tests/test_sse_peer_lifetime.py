"""Client cancellation must not wait for an application heartbeat."""
import errno,socket,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import patch
from gp_control_plane.http.cheroot_connection import WebStreamLifecycle
from gp_control_plane.http.stream_admission import StreamAdmission
from gp_control_plane.http.http_runtime import WebHttpRuntime

@contextmanager
def pair():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen()
        with socket.create_connection(listener.getsockname()) as client:
            server,peer=listener.accept()
            with server:yield client,server,peer

class PeerLifetimeTests(unittest.TestCase):
    def test_core_event_cancel_interrupts_wait_before_next_auth_tick(self):
        from bottle import request
        from gp_control_plane.http import core_app
        life=WebStreamLifecycle();services=SimpleNamespace(config=SimpleNamespace(output=SimpleNamespace(state_dir=Path('unused-synthetic-state'))),stream_lifecycle=life)
        request.bind({'REMOTE_ADDR':'127.0.0.1','REMOTE_PORT':'12345'})
        finished=threading.Event()
        with patch.object(core_app,'_legacy') as legacy:
            legacy.return_value._event_payloads.return_value={}
            stream=core_app._events(services,lambda *args:None,'synthetic','/api/internal/web-events/stream')
            self.assertEqual(b': keepalive\n\n',next(stream))
            def read():
                try:next(stream)
                except StopIteration:finished.set()
            worker=threading.Thread(target=read);worker.start()
            try:
                life.close_peer(('127.0.0.1',12345))
                self.assertTrue(finished.wait(.3),'terminal Event did not interrupt sleep')
            finally:
                worker.join(2);stream.close()
            self.assertFalse(worker.is_alive());self.assertEqual(0,life.active_count())

    def test_real_eof_releases_only_own_stream_and_keeps_two_active_limit(self):
        life=WebStreamLifecycle();admission=StreamAdmission()
        with pair() as (a,sa,pa),pair() as (b,sb,pb):
            permits=[admission.acquire('sse'),admission.acquire('sse')]
            self.assertIsNone(admission.acquire('sse'))
            life.bind_peer(pa,sa);life.bind_peer(pb,sb)
            life.register(pa,permits[0].release);life.register(pb,permits[1].release)
            life.observe_peers_once();self.assertEqual(2,life.active_count())
            a.close();life.observe_peers_once()
            released=admission.acquire('sse');self.assertIsNotNone(released)
            self.assertIsNone(admission.acquire('sse'));released.release()
            self.assertEqual(1,life.active_count());b.close();life.observe_peers_once()
            self.assertEqual(0,life.active_count())

    def test_peek_does_not_consume_data_or_poll_inactive_peers(self):
        life=WebStreamLifecycle();closed=[]
        with pair() as (client,server,peer):
            life.bind_peer(peer,server)
            with patch('gp_control_plane.http.cheroot_connection.select.select',side_effect=AssertionError('inactive peer polled')):life.observe_peers_once()
            life.register(peer,lambda:closed.append(True));client.sendall(b'not-EOF')
            life.observe_peers_once();self.assertEqual([],closed);self.assertEqual(b'not-EOF',server.recv(7))

    def test_reused_peer_and_stale_unbind_cannot_close_new_socket(self):
        life=WebStreamLifecycle();calls=[]
        with pair() as (_,old,peer),pair() as (_,new,_):
            life.bind_peer(peer,old);life.bind_peer(peer,new);life.register(peer,lambda:calls.append(True))
            life.unbind_peer(peer,old);life.close_peer(peer,expected_socket=old)
            self.assertIs(life._peer_sockets[peer],new);self.assertEqual([],calls)
            life.close_peer(peer,expected_socket=new);self.assertEqual([True],calls)

    def test_reset_is_terminal_unknown_socket_error_is_visible(self):
        life=WebStreamLifecycle();calls=[]
        with pair() as (_,server,peer):
            life.bind_peer(peer,server);life.register(peer,lambda:calls.append(True))
            with patch('gp_control_plane.http.cheroot_connection.select.select',side_effect=OSError(errno.EBADF,'unknown')):
                with self.assertRaises(OSError):life.observe_peers_once()
            self.assertEqual([],calls)
            with patch('gp_control_plane.http.cheroot_connection.select.select',side_effect=ConnectionResetError(errno.ECONNRESET,'peer reset')):life.observe_peers_once()
            self.assertEqual([True],calls)

    def test_observer_owned_start_stop_and_eof_wake(self):
        life=WebStreamLifecycle();done=threading.Event()
        self.assertIsNone(life._observer_thread)
        with pair() as (client,server,peer):
            life.bind_peer(peer,server);life.register(peer,done.set);life.start_observer()
            try:
                client.close();self.assertTrue(done.wait(1),'EOF callback not delivered')
            finally:life.stop_observer()
        self.assertFalse(life._observer_thread.is_alive());life.stop_observer();life.start_observer()
        self.assertFalse(life._observer_thread.is_alive())

    def test_observer_failure_does_not_skip_listener_cleanup(self):
        life=WebStreamLifecycle();runtime=WebHttpRuntime(lambda *args:[],'127.0.0.1',0,stream_lifecycle=life)
        runtime._start_entered=True;runtime._server.ready=True;life._observer_error=OSError(errno.EBADF,'unknown')
        with patch.object(runtime._server,'stop') as stop:
            with self.assertRaisesRegex(RuntimeError,'observer failed'):runtime.stop()
            stop.assert_called_once()

if __name__=='__main__':unittest.main()

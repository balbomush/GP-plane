"""Linux executable preflight: closed sockets are not foreign listeners."""
import importlib.util
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest import mock


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux socket reuse semantics')
class ListenerPreparationTests(unittest.TestCase):
    def setUp(self):
        script=Path(__file__).resolve().parents[1]/'scripts/check-core-listener.py'
        spec=importlib.util.spec_from_file_location('listener_preparation',script)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)

    def mapped_socket(self, port):
        class Probe(socket.socket):
            def bind(self, address):
                assert address==('127.0.0.1',8081)
                return super().bind(('127.0.0.1',port))
        return types.SimpleNamespace(socket=Probe,SOL_SOCKET=socket.SOL_SOCKET,SO_REUSEADDR=socket.SO_REUSEADDR)

    def test_time_wait_does_not_require_a_managed_service(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1];listener.listen()
            with socket.create_connection(('127.0.0.1',port)) as client:
                peer,_=listener.accept()
                peer.shutdown(socket.SHUT_WR)
                self.assertEqual(client.recv(1),b'')
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(peer.recv(1),b'');peer.close()
        with socket.socket() as plain:
            with self.assertRaises(OSError):plain.bind(('127.0.0.1',port))
        with mock.patch.object(self.module,'socket',self.mapped_socket(port)),mock.patch.object(self.module.subprocess,'run') as systemctl:
            self.module.check(Path('/not-installed'),'nobody')
            systemctl.assert_not_called()

    def test_foreign_live_listener_is_still_rejected(self):
        with socket.socket() as foreign:
            foreign.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            foreign.bind(('127.0.0.1',0));port=foreign.getsockname()[1];foreign.listen()
            result=types.SimpleNamespace(stdout='0\n')
            with mock.patch.object(self.module,'socket',self.mapped_socket(port)),mock.patch.object(self.module.subprocess,'run',return_value=result) as systemctl:
                with self.assertRaisesRegex(ValueError,'unidentified process'):
                    self.module.check(Path('/not-installed'),'nobody')
                systemctl.assert_called_once()

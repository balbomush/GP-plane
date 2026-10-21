"""Test-only installed Web owner. Products communicate solely through HTTP.

The mandatory artifact integration runner supplies GP_TEST_WEB_PYTHON pointing
to an isolated Web venv. Absence is an error, never a successful skip.
"""
from __future__ import annotations
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from tests.support.process_guard import OwnedProcess


def serve_web_proxy(*args, **kwargs):
    raise AssertionError('test marker must be started by start_web_process')


class InstalledWebProcess:
    def __init__(self, python: Path, port: int, core_url: str, *, deny_sqlite=False):
        self.port=port
        self._closed=False
        self.log=tempfile.TemporaryFile()
        env={key:value for key,value in os.environ.items() if key not in {'PYTHONPATH','PYTHONHOME'}}
        entry=['-m','gp_control_plane_web.cli']
        if deny_sqlite:
            entry=['-c',"import sqlite3; exec(\"def deny(*args,**kwargs):\\n raise AssertionError('Web must not open product SQLite')\"); sqlite3.connect=deny; from gp_control_plane_web.cli import main; raise SystemExit(main())"]
        self.owner=OwnedProcess([str(python),'-I',*entry,
            '--host','127.0.0.1','--port',str(port),'--core-url',core_url],
            cwd=tempfile.gettempdir(),env=env,stdout=self.log,stderr=self.log)
        self.process=self.owner.process
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                if self.process.poll() is not None:
                    raise AssertionError('installed Web exited before listener readiness')
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):
                        return
                except OSError: time.sleep(.02)
            raise TimeoutError('installed Web listener did not start')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed: return
        self._closed=True
        try:
            self.owner.close()
        finally:self.log.close()
        # A leaked listener is a teardown failure, even if assertions passed.
        deadline=time.monotonic()+2
        while True:
            with socket.socket() as probe:
                probe.settimeout(.1)
                if probe.connect_ex(('127.0.0.1',self.port))!=0:break
            if time.monotonic()>=deadline:
                raise AssertionError('installed Web listener survived teardown')
            time.sleep(.02)


def start_web_process(port: int, core_url: str, *, deny_sqlite=False):
    raw=os.environ.get('GP_TEST_WEB_PYTHON')
    if not raw:raise AssertionError('GP_TEST_WEB_PYTHON is required by artifact integration')
    python=Path(raw)
    if not python.is_absolute() or not python.is_file():raise AssertionError('Web interpreter must be an existing absolute path')
    # The test controller belongs to Core; the child belongs to Web. Refuse a
    # convenience venv containing both distributions.
    result=subprocess.run([str(python),'-I','-c',
        "import importlib.util; assert importlib.util.find_spec('gp_control_plane') is None; import gp_control_plane_web"],
        capture_output=True,timeout=10,cwd=tempfile.gettempdir())
    if result.returncode:raise AssertionError('Web environment is not independent')
    return InstalledWebProcess(python,port,core_url,deny_sqlite=deny_sqlite)

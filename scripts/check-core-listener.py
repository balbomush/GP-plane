#!/usr/bin/env python3
"""Before activation allow only the identified old Core listener on port 8081.

Read-only. Does not kill processes, modify units, inspect application data or
allocate privileges. A foreign/unknown listener is a preparation failure.
"""
import argparse
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess


def check(install_dir: Path, install_user: str) -> None:
    with socket.socket() as probe:
        # Match Cheroot's reuse policy: a closed listener can leave TIME_WAIT
        # connections without owning the port. A live listener still rejects
        # this bind and must pass the managed-process checks below.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', 8081))
        except OSError:
            pass
        else:
            return
    result = subprocess.run(['systemctl', 'show', '--property=MainPID', '--value',
                             'gp-control-plane-core.service'], text=True, capture_output=True, timeout=5, check=True)
    pid = int(result.stdout.strip() or '0')
    if pid <= 1:
        raise ValueError('Core port 8081 is occupied by an unidentified process')
    proc = Path('/proc') / str(pid)
    if proc.stat().st_uid != pwd.getpwnam(install_user).pw_uid:
        raise ValueError('Core listener is not owned by the install user')
    args = [p.decode() for p in (proc/'cmdline').read_bytes().split(b'\0') if p]
    executable = str(install_dir / '.venv/bin/gp-control-plane')
    if executable not in args or 'core' not in args:
        raise ValueError('Core listener does not match the managed installation')
    listeners = subprocess.run(['ss', '-H', '-ltnp', 'sport = :8081'], text=True,
                               capture_output=True, timeout=5, check=True).stdout.splitlines()
    if not listeners or any(set(re.findall(r'pid=(\d+)', line)) != {str(pid)} for line in listeners):
        raise ValueError('Core port 8081 has a foreign or unknown listener')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--install-user', required=True)
    args = parser.parse_args()
    try:
        check(args.install_dir, args.install_user)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f'ERROR: listener preparation failed: {error}; previous GP was preserved\n')

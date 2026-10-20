"""Core-only package boundary and preserved historical import identity."""
import importlib
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

class CoreBoundaryTests(unittest.TestCase):
    def test_compatibility_facades_are_same_core_modules(self):
        for name in ['api_server','core_app','core_runtime','core_server','routes','docs','http_body','cheroot_connection']:
            with self.subTest(name=name):
                self.assertIs(importlib.import_module('gp_control_plane.http.'+name),
                              importlib.import_module('gp_control_plane.web.'+name))

    def test_core_has_no_web_implementation_or_assets(self):
        base=ROOT/'src/gp_control_plane'
        for name in ['ui.py','ui_assets','proxy.py','core_client.py','web_app.py','app.py']:
            self.assertFalse((base/'web'/name).exists(),name)
        self.assertNotIn('gp_control_plane_web',(ROOT/'pyproject.toml').read_text())

    def test_monolith_mode_rejected_before_recovery(self):
        from unittest.mock import patch
        from gp_control_plane.http.core_runtime import create_core_runtime
        from gp_control_plane.config import AppConfig, OutputConfig
        with patch('gp_control_plane.http.api_server._recover_runtime_before_serve') as recover:
            with self.assertRaisesRegex(ValueError,'separately'):
                create_core_runtime(AppConfig(output=OutputConfig(state_dir=ROOT/'build/not-created-s2a')),'127.0.0.1',0,ui_enabled=True)
            recover.assert_not_called()

    def test_installer_preserves_web_ownership_and_checks_port_before_stop(self):
        script=(ROOT/'scripts/install-linux.sh').read_text()
        self.assertNotIn('cat > /etc/systemd/system/gp-control-plane-web.service',script)
        self.assertNotIn('gp-web.service',script)
        self.assertLess(script.index('check-core-listener.py'),script.index('phase=activation'))
        self.assertIn('legacy GP service is still active; files were preserved',script)
        self.assertIn('Core-only installation',script)

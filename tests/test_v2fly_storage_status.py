from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if os.environ.get('GP_TEST_INSTALLED') != '1':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.domain_sources import (
    list_v2fly_categories_local, v2fly_group_cache_dir, v2fly_group_manifest_path,
)
from gp_control_plane.v2fly_payloads import v2fly_storage_status_payload
from test_core_application_http import _captured_server_temporary_directory, _http_request, serve_core


class V2flyStorageStatusTests(unittest.TestCase):
    def test_unprepared_and_empty_directory_are_missing_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=state))
            for create_directory in (False, True):
                if create_directory:
                    v2fly_group_cache_dir(state).mkdir(parents=True)
                result = list_v2fly_categories_local(state)
                self.assertEqual(result['data_status'], 'missing')
                self.assertEqual(result['error_kind'], '')
                self.assertEqual(result['errors'], [])
                self.assertEqual(v2fly_storage_status_payload(config)['state'], 'missing')

    def test_manifest_and_group_read_failures_keep_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=state))
            manifest = v2fly_group_manifest_path(state)
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"categories":["example"]}', encoding='utf-8')
            original = Path.read_text
            def denied(path: Path, *args: object, **kwargs: object) -> str:
                if path == manifest:
                    raise PermissionError(13, 'synthetic manifest denial')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', autospec=True, side_effect=denied):
                self.assertEqual(v2fly_storage_status_payload(config)['state'], 'error')
            manifest.unlink()
            v2fly_group_cache_dir(state).mkdir()
            with patch.object(Path, 'iterdir', side_effect=PermissionError(13, 'synthetic group denial')):
                self.assertEqual(v2fly_storage_status_payload(config)['state'], 'error')

    def test_public_core_and_service_report_missing_error_and_legacy_ready(self) -> None:
        cases = (
            ('missing', None, 'missing'),
            ('invalid-json', b'{broken', 'error'),
            ('invalid-utf8', b'\xff', 'error'),
            ('invalid-object', b'[]', 'error'),
            ('empty-manifest', b'{}', 'error'),
            ('invalid-categories', b'{"categories":"example"}', 'error'),
            ('invalid-member', b'{"categories":[null]}', 'error'),
            ('manifest-ready', b'{"categories":["example"]}', 'ready'),
            ('legacy-ready', None, 'ready'),
            ('group-is-file', None, 'error'),
        )
        for name, content, expected in cases:
            with self.subTest(name=name), _captured_server_temporary_directory() as (raw, start_server):
                state = Path(raw) / 'state'
                config = AppConfig(output=OutputConfig(state_dir=state))
                manifest = v2fly_group_manifest_path(state)
                if content is not None:
                    manifest.parent.mkdir(parents=True, exist_ok=True)
                    manifest.write_bytes(content)
                if name in ('legacy-ready', 'invalid-json'):
                    # An existing legacy group must not hide a corrupt manifest.
                    groups = v2fly_group_cache_dir(state)
                    groups.mkdir(parents=True, exist_ok=True)
                    (groups / 'example').write_text('domain:example.com\n', encoding='utf-8')
                if name == 'group-is-file':
                    groups = v2fly_group_cache_dir(state)
                    groups.parent.mkdir(parents=True, exist_ok=True)
                    groups.write_text('not a directory', encoding='utf-8')
                port = start_server(serve_core, config).port
                for endpoint in ('/api/service/v2fly/local-storage-status', '/api/core/presets/v2fly/categories'):
                    status, _, body = _http_request(port, endpoint, authenticated=False)
                    self.assertEqual(status, 401)
                    status, _, body = _http_request(port, endpoint)
                    self.assertEqual(status, 200)
                    payload = json.loads(body)
                    storage = payload['storage'] if endpoint.endswith('/categories') else payload
                    self.assertEqual(storage['state'], expected)
                    self.assertEqual(bool(storage['group_count']), expected == 'ready')


if __name__ == '__main__':
    unittest.main()

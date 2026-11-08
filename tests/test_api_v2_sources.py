"""A failed revision probe cannot masquerade as up to date."""

from io import BytesIO
from tarfile import TarInfo, open as open_tar
from unittest.mock import patch
from urllib.error import HTTPError
import unittest

from gp_control_plane import domain_sources
from gp_control_plane.http import api_v2
from tests.support.api2_http import Api2HttpFixture


SOURCE = "/api/v2/data/sources/v2fly"


class SourceTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)
        self.configured = hasattr(api_v2, "register_sources")
        if self.configured:
            api_v2.register_sources(app, self.state)

    def test_source_probe_and_update(self):
        self.assertTrue(self.configured, "source routes are missing")
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        self.assertEqual(self.call("GET", SOURCE, b"", token)[2]["state"], "missing")
        archive_bytes = BytesIO()
        with open_tar(fileobj=archive_bytes, mode="w:gz") as archive:
            domain_bytes = b"domain:one.example\n"
            info = TarInfo("domain-list-community-pinned/data/sample")
            info.size = len(domain_bytes)
            archive.addfile(info, BytesIO(domain_bytes))
        first, second = "a" * 40, "b" * 40
        with patch.object(domain_sources, "fetch_v2fly_revision", return_value=first), patch.object(
            domain_sources, "fetch_v2fly_archive", return_value=archive_bytes.getvalue()
        ) as fetch:
            status, _, content = self.call("POST", SOURCE + "/update", b"", token)
            self.assertEqual((status, content["installed_revision"]), (200, first))
            fetch.assert_called_once_with(first)
        self.assertEqual(self.call("GET", SOURCE + "/categories/sample/domains", b"", token)[2]["items"],
                         ["one.example"])
        for revision, code, expected in [
            ("invalid", 502, "upstream_error"),
            (HTTPError("https://example.test", 429, "rate limited", {}, None), 503, "upstream_rate_limited"),
        ]:
            options = {"side_effect": revision} if isinstance(revision, Exception) else {"return_value": revision}
            with self.subTest(code=code), patch.object(domain_sources, "fetch_v2fly_revision", **options):
                for method in ("GET", "POST"):
                    status, _, body = self.call(method, SOURCE + "/update", b"", token)
                    self.assertEqual((status, body["error"]["code"]), (code, expected))
                self.assertEqual(self.call("GET", SOURCE, b"", token)[2]["installed_revision"], first)
        with patch.object(domain_sources, "fetch_v2fly_revision", return_value=second):
            status, _, body = self.call("GET", SOURCE + "/update", b"", token)
            self.assertEqual((status, body["update_available"]), (200, True))

"""HTTPX wire controls for external Core downloads."""

from unittest import TestCase
from unittest.mock import patch

import httpx

from gp_control_plane.downloads import DownloadError, get_bytes


class Body(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = False
        self.closed = False

    def __iter__(self):
        self.read = True
        yield from self.chunks

    def close(self):
        self.closed = True


class DownloadTests(TestCase):
    def get(self, handler, **kwargs):
        client_type = httpx.Client
        with patch("gp_control_plane.downloads.httpx.Client", side_effect=lambda **options: client_type(transport=httpx.MockTransport(handler), **options)):
            return get_bytes("https://example.test/file", timeout=5, limit=4, **kwargs)

    def test_never_reads_redirect_body_and_closes_both_streams(self):
        redirect, body = Body([b"arbitrary redirect body"]), Body([b"ab", b"cd"])

        def handle(request):
            if request.url.path == "/file":
                return httpx.Response(302, headers={"Location": "https://example.test/target"}, stream=redirect)
            self.assertEqual(request.url.path, "/target")
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            return httpx.Response(200, stream=body)

        self.assertEqual(self.get(handle), b"abcd")
        self.assertFalse(redirect.read)
        self.assertTrue(redirect.closed and body.closed)

    def test_rejects_https_downgrade_before_opening_the_redirect(self):
        redirect = Body([b"response"])
        seen = []

        def handle(request):
            seen.append(request.url)
            return httpx.Response(302, headers={"Location": "http://example.test/target"}, stream=redirect)

        with self.assertRaises(DownloadError):
            self.get(handle)
        self.assertEqual(len(seen), 1)
        self.assertTrue(redirect.closed)

    def test_oversize_and_rate_limit_close_streams(self):
        oversize = Body([b"abcd", b"e"])
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.get(lambda request: httpx.Response(200, stream=oversize))
        self.assertTrue(oversize.closed)
        limited = Body([b"discarded"])
        with self.assertRaises(DownloadError) as captured:
            self.get(lambda request: httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, stream=limited))
        self.assertTrue(captured.exception.rate_limited)
        self.assertTrue(limited.closed)
        self.assertFalse(limited.read)

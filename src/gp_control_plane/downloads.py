"""Bounded, verified HTTP reads for Core's release and source adapters."""

from collections.abc import Mapping
from contextlib import closing
import ssl
from time import monotonic

import httpx


class DownloadError(OSError):
    def __init__(self, *, rate_limited: bool = False) -> None:
        super().__init__("upstream HTTP request failed")
        self.rate_limited = rate_limited


def _read(response: httpx.Response, limit: int, deadline: float) -> bytes:
    response.raise_for_status()
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise ValueError("unexpected upstream content encoding")
    body = bytearray()
    for chunk in response.iter_raw():
        if monotonic() >= deadline:
            raise DownloadError()
        if len(body) + len(chunk) > limit:
            raise ValueError("upstream response exceeds its size limit")
        body.extend(chunk)
    return bytes(body)


def get_bytes(url: str, *, limit: int, timeout: float, headers: Mapping[str, str] | None = None) -> bytes:
    deadline = monotonic() + timeout
    with httpx.Client(
        headers={"Accept-Encoding": "identity", **(headers or {})},
        verify=ssl.create_default_context(),
    ) as client:
        request = client.build_request("GET", url)
        try:
            for _ in range(6):
                remaining = deadline - monotonic()
                if request.url.scheme != "https" or remaining <= 0:
                    raise DownloadError()
                request.extensions["timeout"] = httpx.Timeout(remaining).as_dict()
                with closing(client.send(request, stream=True, follow_redirects=False)) as response:
                    # Following a redirect automatically would first buffer its
                    # entire response body. HTTPX still constructs the next URL.
                    if response.next_request is None:
                        return _read(response, limit, deadline)
                    request = response.next_request
            raise DownloadError()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            rate_limited = status == 429 or (
                status == 403 and error.response.headers.get("X-RateLimit-Remaining") == "0"
            )
            raise DownloadError(rate_limited=rate_limited) from None
        except httpx.RequestError:
            raise DownloadError() from None

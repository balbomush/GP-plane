"""Frozen public error representation, independent of the HTTP transport."""

from html import escape

from bottle import HTTPResponse


def unsupported_method(method: str) -> HTTPResponse:
    message = escape(f"Unsupported method ('{method}')", quote=False)
    body = ("<!DOCTYPE HTML>\n"
            "<html lang=\"en\">\n"
            "    <head>\n"
            "        <meta charset=\"utf-8\">\n"
            "        <title>Error response</title>\n"
            "    </head>\n"
            "    <body>\n"
            "        <h1>Error response</h1>\n"
            "        <p>Error code: 501</p>\n"
            f"        <p>Message: {message}.</p>\n"
            "        <p>Error code explanation: 501 - Server does not support this operation.</p>\n"
            "    </body>\n"
            "</html>\n").encode("utf-8", "replace")
    return HTTPResponse(status=501, body=body, headers={
        "Content-Type": "text/html;charset=utf-8", "Content-Length": str(len(body)),
    })

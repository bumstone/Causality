from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from causality.http_adapter import (
    HttpAdapter,
    HttpRequest,
    HttpTransportError,
    normalize_origin,
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/final?redirect-secret=hidden")
            body = b"redirect response"
        elif self.path.startswith("/large"):
            self.send_response(200)
            body = b"x" * 65
        else:
            self.send_response(200)
            body = b"response-secret"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        size = int(self.headers.get("Content-Length", "0"))
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        self.server.received = {  # type: ignore[attr-defined]
            "authorization": self.headers.get("Authorization"),
            "public": self.headers.get("X-Public"),
            "body": self.rfile.read(size),
        }
        body = b"response-secret"
        self.send_response(201)
        self.send_header("X-Response-Secret", "never-return-this")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class _Server:
    def __enter__(self) -> ThreadingHTTPServer:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.paths = []  # type: ignore[attr-defined]
        self.server.received = {}  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _NeverOpen:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("transport must not run")


class _FailingOpen:
    def open(self, *_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("transport-secret")


class HttpOriginTests(unittest.TestCase):
    def test_scope_origin_is_canonical_and_default_ports_are_removed(self) -> None:
        self.assertEqual(
            normalize_origin("HTTPS://API.Example.COM:443", scope=True),
            "https://api.example.com",
        )
        self.assertEqual(
            normalize_origin("http://API.Example.COM:80", scope=True),
            "http://api.example.com",
        )
        self.assertEqual(
            normalize_origin("https://api.example.com:8443", scope=True),
            "https://api.example.com:8443",
        )
        self.assertEqual(
            normalize_origin("http://[2001:0db8::1]:80", scope=True),
            "http://[2001:db8::1]",
        )

    def test_request_url_returns_only_the_exact_origin(self) -> None:
        self.assertEqual(
            normalize_origin("https://Api.Example/path?q=query-secret"),
            "https://api.example",
        )

    def test_scope_rejects_ambiguous_or_non_http_values(self) -> None:
        invalid = (
            "ftp://api.example.com",
            "https://user:secret@api.example.com",
            "https://api.example.com/",
            "https://api.example.com/path",
            "https://api.example.com?q=secret",
            "https://api.example.com?",
            "https://api.example.com#fragment",
            "https://api.example.com#",
            "https://api.example.com:99999",
            "https:///missing-host",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_origin(value, scope=True)

    def test_request_url_rejects_userinfo_and_fragment(self) -> None:
        for value in (
            "https://user:secret@api.example.com/path",
            "https://api.example.com/path#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_origin(value)


class HttpAdapterTests(unittest.TestCase):
    def _url(self, server: ThreadingHTTPServer, path: str) -> str:
        host, port = server.server_address
        return f"http://{host}:{port}{path}"

    def test_send_transmits_values_but_returns_only_ledger_safe_metadata(self) -> None:
        with _Server() as server:
            request = HttpRequest(
                "post",
                self._url(server, "/submit?token=query-secret"),
                headers={"X-Public": "header-secret"},
                body=b"request-secret",
            )
            result = HttpAdapter().send(
                request,
                credential_headers={"Authorization": "Bearer credential-secret"},
            )

            self.assertEqual(result.status, 201)
            self.assertEqual(result.method, "POST")
            self.assertEqual(result.origin, normalize_origin(request.url))
            self.assertEqual(result.request_bytes, len(b"request-secret"))
            self.assertEqual(result.response_bytes, len(b"response-secret"))
            self.assertFalse(result.artifact_written)
            self.assertEqual(
                server.received,  # type: ignore[attr-defined]
                {
                    "authorization": "Bearer credential-secret",
                    "public": "header-secret",
                    "body": b"request-secret",
                },
            )
            serialized = repr(result) + repr(result.to_metadata())
            for secret in (
                "query-secret",
                "header-secret",
                "credential-secret",
                "request-secret",
                "response-secret",
                "never-return-this",
            ):
                self.assertNotIn(secret, serialized)

    def test_response_body_is_written_only_to_explicit_resolved_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _Server() as server:
            root = Path(temp_dir).resolve()
            artifact = root / "response.bin"
            result = HttpAdapter().send(
                HttpRequest("GET", self._url(server, "/body"), artifact_path=artifact)
            )

            self.assertEqual(artifact.read_bytes(), b"response-secret")
            self.assertTrue(result.artifact_written)
            self.assertEqual(
                result.artifact_sha256,
                hashlib.sha256(b"response-secret").hexdigest(),
            )

            relative = Path("response.bin")
            opener = _NeverOpen()
            with self.assertRaises(ValueError):
                HttpAdapter(opener=opener).send(
                    HttpRequest("GET", self._url(server, "/body"), artifact_path=relative)
                )
            self.assertEqual(opener.calls, 0)

            unresolved = root / "nested" / ".." / "ambiguous.bin"
            with self.assertRaises(ValueError):
                HttpAdapter(opener=opener).send(
                    HttpRequest("GET", self._url(server, "/body"), artifact_path=unresolved)
                )
            self.assertEqual(opener.calls, 0)

    def test_redirects_are_returned_and_never_followed(self) -> None:
        with _Server() as server:
            result = HttpAdapter().send(
                HttpRequest("GET", self._url(server, "/redirect"))
            )

            self.assertEqual(result.status, 302)
            self.assertEqual(server.paths, ["/redirect"])  # type: ignore[attr-defined]

    def test_default_opener_ignores_environment_proxies(self) -> None:
        with patch(
            "urllib.request.getproxies",
            return_value={"http": "http://proxy.invalid:9999"},
        ) as getproxies:
            adapter = HttpAdapter()
        getproxies.assert_not_called()
        self.assertFalse(
            any(getattr(handler, "proxies", {}) for handler in adapter._opener.handlers)
        )

    def test_request_limit_is_checked_before_transport(self) -> None:
        opener = _NeverOpen()
        adapter = HttpAdapter(opener=opener, max_request_bytes=4)
        with self.assertRaises(HttpTransportError) as caught:
            adapter.send(HttpRequest("POST", "https://api.example", body=b"12345"))
        self.assertEqual(caught.exception.code, "request_too_large")
        self.assertEqual(opener.calls, 0)

    def test_response_limit_fails_closed_without_writing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _Server() as server:
            artifact = Path(temp_dir).resolve() / "large.bin"
            with self.assertRaises(HttpTransportError) as caught:
                HttpAdapter(max_response_bytes=64).send(
                    HttpRequest("GET", self._url(server, "/large"), artifact_path=artifact)
                )
            self.assertEqual(caught.exception.code, "response_too_large")
            self.assertFalse(artifact.exists())

    def test_header_collisions_and_injection_are_rejected_before_transport(self) -> None:
        opener = _NeverOpen()
        adapter = HttpAdapter(opener=opener)
        with self.assertRaises(ValueError):
            adapter.send(
                HttpRequest("GET", "https://api.example", headers={"Authorization": "x"}),
                credential_headers={"authorization": "secret"},
            )
        with self.assertRaises(ValueError):
            adapter.send(
                HttpRequest("GET", "https://api.example"),
                credential_headers={"X-Key": "safe\r\nInjected: secret"},
            )
        self.assertEqual(opener.calls, 0)

    def test_transport_error_does_not_echo_url_query_or_underlying_error(self) -> None:
        request = HttpRequest(
            "POST",
            "https://api.example/path?token=query-secret",
            headers={"X-Secret": "header-secret"},
            body=b"body-secret",
        )
        with self.assertRaises(HttpTransportError) as caught:
            HttpAdapter(opener=_FailingOpen()).send(
                request,
                credential_headers={"Authorization": "credential-secret"},
            )
        rendered = str(caught.exception)
        self.assertIsNone(caught.exception.__cause__)
        for secret in (
            "query-secret",
            "header-secret",
            "body-secret",
            "credential-secret",
            "transport-secret",
        ):
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()

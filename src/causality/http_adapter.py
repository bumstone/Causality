"""Bounded, no-redirect HTTP transport with ledger-safe results."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit


DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_METHOD = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class HttpTransportError(RuntimeError):
    """A transport failure whose message is safe to persist in a ledger."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Opener(Protocol):
    def open(self, request: urllib.request.Request, *, timeout: float) -> Any: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _canonical_host(hostname: str) -> str:
    if "%" in hostname:
        raise ValueError("URL host must not contain a zone identifier")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        hostname = hostname.rstrip(".")
        if not hostname:
            raise ValueError("URL requires a host")
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("URL host is invalid") from None
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError("URL host is invalid")
        if any(not label for label in host.split(".")):
            raise ValueError("URL host is invalid")
        return host
    if isinstance(address, ipaddress.IPv6Address):
        return f"[{address.compressed}]"
    return str(address)


def normalize_origin(value: str, *, scope: bool = False) -> str:
    """Return a canonical HTTP(S) origin.

    Contract and server scopes use ``scope=True`` so an origin containing even
    a root path, query, or fragment is rejected instead of silently widened.
    Request URLs may contain a path/query, but userinfo and fragments are never
    accepted and none of those values are returned.
    """

    if (
        not isinstance(value, str)
        or not value
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ValueError("origin must be a non-empty URL without whitespace")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("URL is invalid") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("origin scheme must be http or https")
    if not parsed.netloc or parsed.hostname is None:
        raise ValueError("URL requires a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL userinfo is not allowed")
    if "#" in value:
        raise ValueError("URL fragments are not allowed")
    if scope and (parsed.path or "?" in value):
        raise ValueError("network scope must be an exact origin without path or query")
    if parsed.netloc.endswith(":"):
        raise ValueError("URL port is invalid")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("URL port is invalid") from None
    host = _canonical_host(parsed.hostname)
    default_port = 80 if scheme == "http" else 443
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


def _validated_headers(headers: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    normalized: set[str] = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError("HTTP header name is invalid")
        if not isinstance(value, str) or any(ch in value for ch in "\r\n\0"):
            raise ValueError("HTTP header value is invalid")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError("HTTP header value is invalid") from None
        folded = name.casefold()
        if folded in normalized:
            raise ValueError("duplicate HTTP header name")
        normalized.add(folded)
        validated[name] = value
    return validated


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout: float = 30.0
    artifact_path: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not _METHOD.fullmatch(self.method):
            raise ValueError("HTTP method is invalid")
        if not isinstance(self.url, str):
            raise ValueError("HTTP URL must be text")
        normalize_origin(self.url)
        try:
            self.url.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("HTTP URL must be ASCII with escaped path/query values") from None
        if self.body is not None and not isinstance(self.body, bytes):
            raise TypeError("HTTP body must be bytes or None")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
        ):
            raise ValueError("HTTP timeout must be finite and positive")
        if self.timeout <= 0:
            raise ValueError("HTTP timeout must be finite and positive")
        validated = _validated_headers(self.headers)
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "headers", MappingProxyType(validated))
        if self.artifact_path is not None:
            object.__setattr__(self, "artifact_path", Path(self.artifact_path))


@dataclass(frozen=True)
class HttpResult:
    """Response metadata designed to be safe for direct ledger serialization."""

    method: str
    origin: str
    status: int
    request_bytes: int
    response_bytes: int
    response_sha256: str
    artifact_written: bool
    artifact_sha256: str | None = None

    def to_metadata(self) -> dict[str, object]:
        return {
            "method": self.method,
            "origin": self.origin,
            "status": self.status,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "response_sha256": self.response_sha256,
            "artifact_written": self.artifact_written,
            "artifact_sha256": self.artifact_sha256,
        }


class HttpAdapter:
    def __init__(
        self,
        *,
        opener: _Opener | None = None,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if (
            isinstance(max_request_bytes, bool)
            or not isinstance(max_request_bytes, int)
            or max_request_bytes < 0
        ):
            raise ValueError("max_request_bytes must be a non-negative integer")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 0
        ):
            raise ValueError("max_response_bytes must be a non-negative integer")
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def send(
        self,
        request: HttpRequest,
        *,
        credential_headers: Mapping[str, str] | None = None,
        before_effect: Callable[[], None] | None = None,
    ) -> HttpResult:
        origin = normalize_origin(request.url)
        artifact = self._artifact_target(request.artifact_path)
        headers = dict(request.headers)
        credentials = _validated_headers(credential_headers or {})
        existing = {name.casefold() for name in headers}
        if any(name.casefold() in existing for name in credentials):
            raise ValueError("credential header conflicts with a request header")
        headers.update(credentials)

        try:
            wire_request = urllib.request.Request(
                request.url,
                data=request.body,
                headers=headers,
                method=request.method,
            )
        except Exception:
            raise ValueError("HTTP request could not be encoded") from None
        request_bytes = self._request_size(request, headers)
        if request_bytes > self.max_request_bytes:
            raise HttpTransportError(
                "request_too_large",
                "HTTP request exceeds the configured byte limit",
            )
        if before_effect is not None:
            before_effect()
        try:
            response = self._opener.open(wire_request, timeout=float(request.timeout))
        except urllib.error.HTTPError as exc:
            # A disabled redirect and ordinary 4xx/5xx response both arrive as
            # HTTPError. They are still bounded HTTP responses, not transport
            # failures, and the caller decides whether the status is expected.
            response = exc
        except Exception:
            raise HttpTransportError(
                "transport_failed",
                "HTTP transport failed before a response was available",
            ) from None

        try:
            status = int(response.getcode())
            if not 100 <= status <= 599:
                raise ValueError("invalid HTTP status")
            response_body = self._read_bounded(response)
        except HttpTransportError:
            raise
        except (OSError, TypeError, ValueError):
            raise HttpTransportError(
                "invalid_response",
                "HTTP transport returned an invalid response",
            ) from None
        finally:
            try:
                response.close()
            except Exception:
                pass

        digest = hashlib.sha256(response_body).hexdigest()
        if artifact is not None:
            self._write_artifact(artifact, response_body)
        return HttpResult(
            method=request.method,
            origin=origin,
            status=status,
            request_bytes=request_bytes,
            response_bytes=len(response_body),
            response_sha256=digest,
            artifact_written=artifact is not None,
            artifact_sha256=digest if artifact is not None else None,
        )

    def _artifact_target(self, artifact: Path | None) -> Path | None:
        if artifact is None:
            return None
        if not artifact.is_absolute() or artifact != artifact.resolve():
            raise ValueError("artifact_path must be an absolute, pre-resolved path")
        return artifact

    @staticmethod
    def _request_size(request: HttpRequest, headers: Mapping[str, str]) -> int:
        size = len(request.method.encode("ascii")) + len(request.url.encode("ascii")) + 3
        size += len(request.body or b"")
        return size + sum(
            len(name.encode("ascii")) + len(value.encode("latin-1")) + 4
            for name, value in headers.items()
        )

    @staticmethod
    def _write_artifact(artifact: Path, body: bytes) -> None:
        temporary: Path | None = None
        try:
            if artifact.parent.resolve() != artifact.parent:
                raise OSError("artifact parent changed")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=artifact.parent,
                prefix=f".{artifact.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, artifact)
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise HttpTransportError(
                "artifact_write_failed",
                "HTTP response artifact could not be written",
            ) from None

    def _read_bounded(self, response: Any) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while size <= self.max_response_bytes:
            remaining = self.max_response_bytes + 1 - size
            chunk = response.read(min(64 * 1024, remaining))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise HttpTransportError(
                    "invalid_response",
                    "HTTP transport returned a non-byte response body",
                )
            chunks.append(chunk)
            size += len(chunk)
        if size > self.max_response_bytes:
            raise HttpTransportError(
                "response_too_large",
                "HTTP response body exceeds the configured byte limit",
            )
        return b"".join(chunks)


__all__ = [
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "HttpAdapter",
    "HttpRequest",
    "HttpResult",
    "HttpTransportError",
    "normalize_origin",
]

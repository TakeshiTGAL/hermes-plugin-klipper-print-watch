"""Moonraker HTTP client pinned to one configured origin.

Shapes checked against ghcr.io/mainsail-crew/virtual-klipper-printer on
2026-10-06: success bodies are ``{"result": ...}``; failures are an HTTP
status plus ``{"error": {"code", "message", "traceback"}}``. ``message`` is
often ``"Unknown"`` or ``"Unauthorized"``; the short reason is the last
``HTTPError: HTTP <code>: ...`` line of the traceback. The traceback itself
is not returned.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

JSON_MAX_BYTES = 1_000_000
JSON_TIMEOUT_SECONDS = 10.0
CONTROL_TIMEOUT_SECONDS = 60.0
SNAPSHOT_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 2

Transport = Callable[[str, str, Mapping[str, str], bytes | None, float, int], tuple[int, Mapping[str, str], bytes]]


class MoonrakerError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None, next_step: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.next_step = next_step


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    @property
    def netloc(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        if self.port == default:
            return host
        return f"{host}:{self.port}"

    @property
    def base(self) -> str:
        return urlunsplit((self.scheme, self.netloc, "", "", ""))


def parse_origin(url: str) -> Origin:
    """Accept one http(s) origin. Reject paths, queries, userinfo, and other schemes."""
    raw = (url or "").strip()
    if not raw or any(ch in raw for ch in "\\\n\r\t "):
        raise MoonrakerError(
            "bad_url",
            "MOONRAKER_URL must be one http or https origin, such as http://printer.example:7125.",
            next_step="Set MOONRAKER_URL to scheme://host:port with no user, password, path, or query.",
        )
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise MoonrakerError(
            "bad_url",
            "MOONRAKER_URL must start with http:// or https:// and include a host.",
            next_step="Example: http://printer.example:7125",
        )
    if parts.username or parts.password:
        raise MoonrakerError(
            "bad_url",
            "MOONRAKER_URL must not contain a username or password.",
            next_step="Put the API key in MOONRAKER_API_KEY. It is sent as the X-Api-Key header.",
        )
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise MoonrakerError(
            "bad_url",
            "MOONRAKER_URL must not include a path, query, or fragment.",
            next_step="Use only scheme://host:port. The plugin adds Moonraker paths itself.",
        )
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return Origin(parts.scheme, parts.hostname.lower(), port)


def same_origin(origin: Origin, url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != origin.scheme or not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname.lower() == origin.host and port == origin.port


def _short_error(status: int, payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"HTTP {status}"
    err = payload.get("error")
    if not isinstance(err, dict):
        return f"HTTP {status}"
    message = str(err.get("message") or "").strip()
    traceback = str(err.get("traceback") or "")
    detail = ""
    for line in traceback.splitlines():
        if "HTTPError:" not in line or "HTTP " not in line:
            continue
        tail = line.split("HTTPError:", 1)[-1].strip()
        if ":" in tail:
            detail = tail.split(":", 1)[-1].strip()
    if message and message not in {"Unknown", "Unauthorized"}:
        text = message
    elif detail:
        text = detail
    elif message:
        text = message
    else:
        text = f"HTTP {status}"
    return text.replace("\n", " ")[:500]


def _scrub(text: str, api_key: str) -> str:
    if api_key and api_key in text:
        return text.replace(api_key, "[redacted]")
    return text


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: Origin):
        super().__init__()
        self.origin = origin
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > MAX_REDIRECTS or not same_origin(self.origin, newurl):
            raise MoonrakerError(
                "off_origin",
                "Moonraker redirected to a different host, port, or scheme.",
                status=code,
                next_step="Point MOONRAKER_URL at the printer itself. This plugin will not follow a redirect away from that origin.",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urllib_transport(origin: Origin) -> Transport:
    def send(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float, read_limit: int):
        if not same_origin(origin, url):
            raise MoonrakerError(
                "off_origin",
                "Refusing to contact a host other than the configured Moonraker origin.",
                next_step="Snapshot and API URLs must stay on MOONRAKER_URL.",
            )
        redirect = _SameOriginRedirect(origin)
        opener = urllib.request.build_opener(redirect)
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read(read_limit + 1)
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return status, hdrs, raw
        except MoonrakerError:
            raise
        except TimeoutError:
            raise MoonrakerError(
                "timeout",
                f"Moonraker did not answer within {timeout:g} seconds.",
                next_step=(
                    "Do not assume the printer moved. A command may still have reached the printer and take effect later. "
                    "Call klipper_status and read print_stats.state before sending it again."
                ),
            ) from None
        except urllib.error.HTTPError as exc:
            raw = exc.read(read_limit + 1)
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, raw
        except urllib.error.URLError as exc:
            raise MoonrakerError(
                "network",
                f"Could not reach Moonraker ({exc.reason}).",
                next_step="Check that MOONRAKER_URL is reachable from this machine and that Moonraker is running.",
            ) from None

    return send


class Moonraker:
    def __init__(self, origin: Origin, api_key: str = "", transport: Transport | None = None):
        self.origin = origin
        self.api_key = api_key.strip()
        self.transport = transport or _urllib_transport(origin)

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _url(self, path: str, query: str = "") -> str:
        if not path.startswith("/") or path.startswith("//") or ".." in path or "\\" in path:
            raise MoonrakerError("bad_path", "Refusing a Moonraker path that is not a single absolute path.")
        return urlunsplit((self.origin.scheme, self.origin.netloc, path, query, ""))

    def request_json(self, method: str, path: str, *, query: str = "", body: dict | None = None, timeout: float | None = None) -> Any:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        ctype = "application/json" if payload is not None else None
        url = self._url(path, query)
        try:
            status, _headers, raw = self.transport(
                method, url, self._headers(ctype), payload,
                JSON_TIMEOUT_SECONDS if timeout is None else timeout,
                JSON_MAX_BYTES,
            )
        except MoonrakerError:
            raise
        except TimeoutError:
            raise MoonrakerError(
                "timeout",
                "Moonraker did not answer before the timeout.",
                next_step=(
                    "Do not assume the printer moved. A command may still have reached the printer and take effect later. "
                    "Call klipper_status and read print_stats.state before sending it again."
                ),
            ) from None
        except Exception as exc:
            raise MoonrakerError(
                "network",
                f"Moonraker request failed ({type(exc).__name__}).",
                next_step="Check MOONRAKER_URL and try again. The API key is not included in this message.",
            ) from None
        if len(raw) > JSON_MAX_BYTES:
            raise MoonrakerError(
                "too_large",
                "Moonraker's response was larger than 1000000 bytes and was discarded.",
                status=status,
                next_step="Ask for a smaller object query. This plugin does not keep a truncated body.",
            )
        text = raw.decode("utf-8", "replace")
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            raise MoonrakerError(
                "bad_body",
                f"Moonraker returned HTTP {status} with a body that is not JSON.",
                status=status,
                next_step="Check that MOONRAKER_URL points at Moonraker, not a different web server.",
            ) from None
        if not isinstance(parsed, dict) or "error" in parsed or "result" not in parsed:
            message = _scrub(_short_error(status, parsed), self.api_key)
            code = "unauthorized" if status in {401, 403} else "moonraker"
            step = (
                "Check MOONRAKER_API_KEY, or leave it empty if this machine is in Moonraker trusted_clients."
                if status in {401, 403}
                else "Read the message. The printer was not changed by a failed request."
            )
            raise MoonrakerError(code, message, status=status, next_step=step)
        if status < 200 or status >= 300:
            raise MoonrakerError(
                "moonraker",
                f"Moonraker returned HTTP {status}.",
                status=status,
                next_step="The body had no error object. Treat this as a failure.",
            )
        return parsed["result"]

    def get_bytes(self, url: str, limit: int) -> tuple[bytes, str]:
        if url.startswith("/"):
            target = self._url(url.split("?", 1)[0], url.split("?", 1)[1] if "?" in url else "")
        elif same_origin(self.origin, url):
            parts = urlsplit(url)
            if parts.username or parts.password:
                raise MoonrakerError(
                    "off_origin",
                    "The webcam snapshot URL contains a username or password and was not requested.",
                    next_step="Use a snapshot URL without credentials. The API key stays in the X-Api-Key header.",
                )
            target = url
        else:
            raise MoonrakerError(
                "off_origin",
                "The webcam snapshot URL is not on the configured Moonraker origin.",
                next_step="Use a snapshot_url that is a path on Moonraker, or an absolute URL with the same scheme, host, and port.",
            )
        try:
            status, headers, raw = self.transport(
                "GET", target, self._headers(), None, SNAPSHOT_TIMEOUT_SECONDS, limit,
            )
        except MoonrakerError:
            raise
        except TimeoutError:
            raise MoonrakerError(
                "timeout",
                "The webcam download timed out, so nothing was saved.",
                next_step="Do not treat a missing still as a picture of the printer. Try klipper_status again.",
            ) from None
        except Exception as exc:
            raise MoonrakerError("network", f"Webcam download failed ({type(exc).__name__}).") from None
        if status < 200 or status >= 300:
            raise MoonrakerError("snapshot", f"Webcam snapshot returned HTTP {status}.", status=status)
        declared = str(headers.get("content-length") or "").strip()
        over_cap_step = "Lower the camera resolution or raise snapshot_max_bytes up to 5000000. Nothing was written."
        if declared.isdigit() and int(declared) > limit:
            raise MoonrakerError(
                "too_large",
                f"Webcam still declares {int(declared)} bytes, over the {limit} byte cap, and was discarded.",
                next_step=over_cap_step,
            )
        if len(raw) > limit:
            raise MoonrakerError(
                "too_large",
                f"Webcam still is over the {limit} byte cap and was discarded.",
                next_step=over_cap_step,
            )
        if declared.isdigit() and int(declared) != len(raw):
            raise MoonrakerError(
                "incomplete",
                "The webcam still did not match its Content-Length (cut short), so it was discarded.",
                next_step="Nothing was written. The camera response was not the full file.",
            )
        mime = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        return raw, mime

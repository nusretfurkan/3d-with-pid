"""Loopback HTTP transport for ``InteractiveService`` (standard library only).

Binds 127.0.0.1 only. Every request must carry ``Host: 127.0.0.1:<port>`` or
``localhost:<port>`` (DNS-rebinding defence). Mutations are ``POST`` with
``Content-Type: application/json``, a ``Content-Length`` of at most 1 MiB, an
``Origin`` (when sent) equal to this server's own origin, and a
``Sec-Fetch-Site`` (when sent) of ``same-origin`` or ``none``. No CORS headers
are ever sent, so cross-site pages cannot read responses or send JSON.

Routes: ``GET /api/state|project|history|selection``, ``GET /api/events?build_id=&after=``,
``POST /api/command|build|cancel|selection|save|reopen|propose|apply-proposal|cancel-proposal|reroute``,
``POST /api/ask|cancel-ask`` (read-only questions, ``docs/ASK.md``),
static ``web/`` files (``/`` is ``web/index.html``; ``/vendor/`` is
``web/vendor/``) and ``/artifacts/<build_id>/<relative path>`` inside this
service's own build directories. Paths are percent-decoded once and must be
plain relative names; ``..``, hidden names, backslashes and symlinks out of the
root are refused. Request input never names a file, a program or a shell
command.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import integrity
from .service import ROOT, ServiceError

WEB_ROOT = os.path.join(ROOT, "web")
MAX_BODY = 1 << 20
JSON_TYPE = "application/json"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".map": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain; charset=utf-8", ".wasm": "application/wasm",
    ".glb": "model/gltf-binary", ".hdr": "application/octet-stream", ".ktx2": "image/ktx2",
}
ARTIFACT_TYPES = {
    ".json": "application/json", ".svg": "image/svg+xml", ".html": "text/html; charset=utf-8",
    ".step": "model/step", ".stp": "model/step", ".png": "image/png", ".txt": "text/plain; charset=utf-8",
}
ARTIFACT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
                "frame-ancestors 'self'; sandbox")
STATIC_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; font-src 'self' data:; "
              "frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
API_CSP = "default-src 'none'; frame-ancestors 'none'"
FALLBACK_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Equipment 3D and P&amp;ID service</title></head>
<body style="font-family: system-ui, sans-serif; margin: 2rem; max-width: 40rem">
<h1>Interactive service is running</h1>
<p>The browser workspace (<code>web/index.html</code>) is not installed in this checkout.
The JSON interface is available at <a href="/api/state">/api/state</a>; see <code>docs/INTERACTIVE.md</code>.</p>
</body></html>
"""
POST_ROUTES = {
    "/api/command": "command", "/api/build": "build", "/api/cancel": "cancel", "/api/selection": "select",
    "/api/save": "save", "/api/reopen": "reopen", "/api/propose": "propose", "/api/apply-proposal": "apply_proposal",
    "/api/cancel-proposal": "cancel_proposal", "/api/reroute": "reroute", "/api/ask": "ask",
    "/api/cancel-ask": "cancel_ask",
}
GET_ROUTES = {"/api/state": "state", "/api/project": "project_view", "/api/history": "history_view",
              "/api/selection": "selection_view"}


class _Reject(Exception):
    def __init__(self, status, error, close=False):
        super().__init__(error)
        self.status, self.error, self.close = status, error, close


class InteractiveServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service, port=8765, web_root=WEB_ROOT, quiet=False):
        self.service = service
        self.web_root = os.path.realpath(web_root)
        self.quiet = quiet
        super().__init__(("127.0.0.1", port), _Handler)
        self.port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"


def make_server(service, port=8765, web_root=WEB_ROOT, quiet=False) -> InteractiveServer:
    return InteractiveServer(service, port, web_root, quiet)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "skid-interactive/0.2"
    sys_version = ""
    timeout = 30

    def log_message(self, format, *args):  # noqa: A002 - signature of BaseHTTPRequestHandler
        if not self.server.quiet:
            sys.stderr.write(f"[skid.interactive] {self.address_string()} {format % args}\n")

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    # --- Dispatch ------------------------------------------------------------------

    def _dispatch(self, method):
        try:
            host = self.headers.get("Host", "")
            if host not in self.server.allowed_hosts:
                raise _Reject(403, "this service answers only to its own loopback host name", close=True)
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path
            if path.startswith("/api/"):
                if method == "POST":
                    self._post(path)
                elif method in ("GET", "HEAD"):
                    self._get_api(path, parts.query, method == "HEAD")
                else:
                    raise _Reject(405, f"{method} is not supported", close=True)
            elif method not in ("GET", "HEAD"):
                raise _Reject(405, f"{method} is not supported here", close=True)
            elif path.startswith("/artifacts/"):
                self._artifact(path, method == "HEAD")
            else:
                self._static(path, method == "HEAD")
        except _Reject as exc:
            self._json(exc.status, {"error": exc.error, "diagnostics": []}, close=exc.close)
        except ServiceError as exc:
            self._json(exc.status, {"error": exc.error, "diagnostics": exc.diagnostics})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # report, never leak a traceback to the client
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"error": "internal service error", "diagnostics": [integrity.diagnostic(
                "internal_error", f"{type(exc).__name__}: {str(exc)[:200]}")]}, close=True)

    def _post(self, path):
        name = POST_ROUTES.get(path)
        if name is None:
            raise _Reject(404, f"no such endpoint {path[:80]}", close=True)
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            raise _Reject(403, "cross-origin requests are refused", close=True)
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            raise _Reject(403, f"Sec-Fetch-Site {site[:20]!r} is refused", close=True)
        media = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media != JSON_TYPE:
            raise _Reject(415, "mutations require Content-Type: application/json", close=True)
        if self.headers.get("Transfer-Encoding"):
            raise _Reject(411, "chunked request bodies are not accepted; send Content-Length", close=True)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _Reject(411, "Content-Length is required", close=True) from None
        if length < 0 or length > MAX_BODY:
            raise _Reject(413, f"request body larger than {MAX_BODY} bytes", close=True)
        data = self.rfile.read(length)
        if len(data) != length:
            raise _Reject(400, "request body ended early", close=True)
        try:
            body = integrity.parse_json(data)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise ServiceError(400, f"the request body is not strict JSON ({exc})") from None
        if not isinstance(body, dict):
            raise ServiceError(400, "the request body must be a JSON object")
        self._json(200, getattr(self.server.service, name)(body))

    def _get_api(self, path, query, head):
        service = self.server.service
        if path == "/api/events":
            params = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=4)
            build_id = params.get("build_id", [""])
            after = params.get("after", ["0"])
            if len(build_id) != 1 or len(after) != 1 or not build_id[0]:
                raise ServiceError(400, "events need exactly one build_id and at most one after")
            try:
                after_value = int(after[0])
            except ValueError:
                raise ServiceError(400, "after must be an integer sequence number") from None
            if after_value < 0:
                raise ServiceError(400, "after must be nonnegative")
            self._json(200, service.events(build_id[0], after_value), head=head)
            return
        name = GET_ROUTES.get(path)
        if name is None:
            raise _Reject(404, f"no such endpoint {path[:80]}")
        self._json(200, getattr(service, name)(), head=head)

    # --- Files ---------------------------------------------------------------------

    def _artifact(self, path, head):
        rest = path[len("/artifacts/"):]
        build_id, _, relpath = rest.partition("/")
        try:
            relpath = integrity.safe_relpath(urllib.parse.unquote(relpath, errors="strict"))
        except UnicodeDecodeError:
            raise _Reject(404, "no such artifact") from None
        extension = os.path.splitext(relpath or "")[1].lower()
        if relpath is None or extension not in ARTIFACT_TYPES:
            raise _Reject(404, "no such artifact")
        full = self.server.service.artifact_path(build_id, relpath)
        if full is None:
            raise _Reject(404, "no such artifact")
        headers = {"Content-Security-Policy": ARTIFACT_CSP}
        if extension in (".step", ".stp"):
            headers["Content-Disposition"] = f'attachment; filename="{os.path.basename(relpath)}"'
        self._file(full, ARTIFACT_TYPES[extension], headers, head)

    def _static(self, path, head):
        try:
            decoded = urllib.parse.unquote(path, errors="strict")
        except UnicodeDecodeError:
            raise _Reject(404, "not found") from None
        relpath = decoded.lstrip("/")
        if relpath == "" or relpath.endswith("/"):
            relpath += "index.html"
        index = os.path.join(self.server.web_root, "index.html")
        if relpath == "index.html" and not os.path.isfile(index):
            self._send(200, FALLBACK_PAGE, "text/html; charset=utf-8", {"Content-Security-Policy": STATIC_CSP}, head)
            return
        relpath = integrity.safe_relpath(relpath)
        extension = os.path.splitext(relpath or "")[1].lower()
        if relpath is None or extension not in STATIC_TYPES:
            raise _Reject(404, "not found")
        full = integrity.resolve_inside(self.server.web_root, relpath)
        if full is None:
            raise _Reject(404, "not found")
        self._file(full, STATIC_TYPES[extension], {"Content-Security-Policy": STATIC_CSP}, head)

    # --- Responses -----------------------------------------------------------------

    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cache-Control", "no-store")

    def _send(self, status, body, content_type, headers=None, head=False, close=False):
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, status, payload, head=False, close=False):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Content-Security-Policy": API_CSP}, head, close)

    def _file(self, full, content_type, headers, head):
        try:
            handle = open(full, "rb")
        except OSError:
            raise _Reject(404, "not found") from None
        with handle:
            size = os.fstat(handle.fileno()).st_size
            self.send_response(200)
            self._common_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                shutil.copyfileobj(handle, self.wfile, 1 << 16)

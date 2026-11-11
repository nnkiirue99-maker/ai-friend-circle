"""the host app host proxy: one self-contained file, standard library + Flask only.

Copied next to the host's app.py as ``federation_host_proxy.py``. It does not import the
friend_circle package, open any database, or read chat/memory data. It forwards the
already-authenticated user's ``/api/federation/*`` calls to the private gateway over a Unix
socket, attaching the gateway credential and the mapped local Person. Nothing the browser
sends (cookies, Authorization, forwarded headers) is passed through.

If the config file is absent, ``install`` does nothing, so the host keeps working with the
federation services removed.
"""

import http.client
import json
import os
import socket

from flask import Blueprint, Response, jsonify, request, session

CONFIG = "/etc/friend-circle/host.json"
MAX_BODY = 256 * 1024
MAX_REPLY = 2 * 1024 * 1024
PASSTHROUGH_TYPES = {
    "application/json",
    "text/html",
    "text/javascript",
    "text/css",
    "image/svg+xml",
}
TOKEN_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


class _Unix(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=25)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _token(path):
    info = os.lstat(path)
    if not os.path.isfile(path) or os.path.islink(path) or info.st_mode & 0o077:
        raise ValueError("gateway credential must be a private regular file (0600)")
    with open(path) as handle:
        token = handle.read().strip()
    if not 43 <= len(token) <= 128 or set(token) - TOKEN_CHARS:
        raise ValueError("invalid gateway credential")
    return token


def install(app, config_path=CONFIG, authenticated=None):
    """Register the proxy on ``app``. Returns False (and changes nothing) when unconfigured.

    ``authenticated`` must report the host's own verified login; the default matches a single-user host
    that marks a login with ``session['unlocked']``.
    """
    if not os.path.exists(config_path):
        return False
    with open(config_path) as handle:
        cfg = json.load(handle)
    socket_path, person, ui_origin = cfg["socket"], cfg["person"], cfg["ui_origin"]
    token = _token(cfg["token_file"])
    if not person.startswith("https://") or len(person) > 2048:
        raise ValueError("invalid local person")
    logged_in = authenticated or (lambda: bool(session.get("unlocked")))
    bp = Blueprint("federation_host_proxy", __name__, url_prefix="/api/federation")

    def fail(code, status):
        response = jsonify(error=code)
        response.status_code = status
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @bp.route("/<path:tail>", methods=["GET", "POST", "PUT"])
    def forward(tail):
        if not logged_in():
            return fail("authentication_required", 401)
        if request.method != "GET" and request.headers.get("Origin") != ui_origin:
            return fail("csrf_origin_rejected", 403)
        if request.content_length and request.content_length > MAX_BODY:
            return fail("message_too_large", 413)
        if len(tail) > 2048 or any(part in {".", "..", ""} for part in tail.split("/")):
            return fail("invalid_path", 400)
        try:
            query = request.query_string.decode("ascii")
        except UnicodeDecodeError:
            return fail("invalid_query", 400)
        if len(query) > 8192:
            return fail("query_too_large", 413)
        body = request.stream.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return fail("message_too_large", 413)
        headers = {
            "Authorization": "Bearer " + token,
            "X-FC-Local-Principal": person,
            "Origin": ui_origin,
            "Content-Type": "application/json",
        }
        # The one client preference passed through: post pictures as links (fixed value only).
        if request.headers.get("X-Federation-Media") == "links":
            headers["X-Federation-Media"] = "links"
        conn = _Unix(socket_path)
        try:
            conn.request(
                request.method,
                "/api/federation/" + tail + ("?" + query if query else ""),
                body=body,
                headers=headers,
            )
            reply = conn.getresponse()
            data = reply.read(MAX_REPLY + 1)
            status = reply.status
            media = reply.getheader("Content-Type", "application/json")
            policy = reply.getheader("Content-Security-Policy")
        except (OSError, http.client.HTTPException):
            return fail("federation_gateway_unavailable", 502)
        finally:
            conn.close()
        if len(data) > MAX_REPLY or 300 <= status < 400:
            return fail("invalid_gateway_response", 502)
        if media.split(";")[0].strip() not in PASSTHROUGH_TYPES:
            media = "application/json"
        result = Response(data, status=status, content_type=media)
        if policy:  # the drop-in UI pages carry their own strict policy
            result.headers["Content-Security-Policy"] = policy
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["Cache-Control"] = "private, no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        return result

    app.register_blueprint(bp)
    return True

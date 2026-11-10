"""Private Unix-socket host gateway. Never register this app on the public listener.

The host validates its own user login, then forwards only the mapped local Actor.
The dedicated gateway credential is unrelated to SSH, database or AI API keys.
"""

import hmac
import http.client
import os
from pathlib import Path
import socket

from flask import Flask, Blueprint, Response, g, jsonify, request
from .config import MAX_BODY
from .errors import FederationError
from .local_api import make_blueprint


PASSTHROUGH_TYPES = {
    "application/json",
    "text/html",
    "text/javascript",
    "text/css",
    "image/svg+xml",
}


def credential(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("gateway credential must be a private regular file (0600)")
    token = path.read_text().strip()
    if not 43 <= len(token) <= 128 or any(
        c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in token
    ):
        raise ValueError("invalid gateway credential")
    return token


def create_gateway(service, *, token_path, ui_origin):
    token = credential(token_path)
    app = Flask("federation_private_gateway", static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY

    @app.errorhandler(FederationError)
    def failure(exc):
        return jsonify(error=exc.code), exc.status

    @app.before_request
    def authenticate_host():
        if not hmac.compare_digest(
            request.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()
        ):
            raise FederationError("host_authentication_required", 401)
        actor = request.headers.get("X-FC-Local-Principal", "")
        service.relationships._human(actor)
        g.actor = actor

    app.register_blueprint(make_blueprint(service, lambda: g.actor, ui_origin))
    return app


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=25)
        self.socket_path = os.fspath(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def make_host_proxy(
    *, socket_path, token_path, principal, ui_origin, connection_factory=UnixConnection
):
    """Register on the HOST authenticated Flask app, not the public federation app.

    The Unix socket must be in a protected directory, with access limited to the
    Federation service and host application. Never forward arbitrary browser headers.
    """
    token = credential(token_path)
    bp = Blueprint("federation_host_proxy", __name__, url_prefix="/api/federation")

    @bp.errorhandler(FederationError)
    def failure(exc):
        return jsonify(error=exc.code), exc.status

    @bp.route("/<path:tail>", methods=["GET", "POST", "PUT"])
    def forward(tail):
        actor = principal()
        if not isinstance(actor, str) or not actor:
            raise FederationError("authentication_required", 401)
        if len(actor) > 2048 or any(ord(c) < 32 for c in actor):
            raise FederationError("invalid_principal", 403)
        if request.method != "GET" and request.headers.get("Origin") != ui_origin:
            raise FederationError("csrf_origin_rejected", 403)
        if request.content_length and request.content_length > MAX_BODY:
            raise FederationError("message_too_large", 413)
        if any(part in {".", "..", ""} for part in tail.split("/")) or len(tail) > 2048:
            raise FederationError("invalid_path")
        query = request.query_string.decode("ascii")
        if len(query) > 8192:
            raise FederationError("query_too_large", 413)
        target = "/api/federation/" + tail + ("?" + query if query else "")
        headers = {
            "Authorization": "Bearer " + token,
            "X-FC-Local-Principal": actor,
            "Origin": ui_origin,
            "Content-Type": "application/json",
        }
        body = request.stream.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise FederationError("message_too_large", 413)
        conn = connection_factory(socket_path)
        try:
            conn.request(request.method, target, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024 or 300 <= response.status < 400:
                raise FederationError("invalid_gateway_response", 502)
            media = response.getheader("Content-Type", "application/json")
            if media.split(";")[0].strip() not in PASSTHROUGH_TYPES:
                media = "application/json"
            result = Response(data, status=response.status, content_type=media)
            policy = response.getheader("Content-Security-Policy")
            if policy:
                result.headers["Content-Security-Policy"] = policy
            result.headers["Referrer-Policy"] = "no-referrer"
            result.headers["Cache-Control"] = "private, no-store"
            result.headers["X-Content-Type-Options"] = "nosniff"
            return result
        except (OSError, http.client.HTTPException) as exc:
            raise FederationError("federation_gateway_unavailable", 502) from exc
        finally:
            conn.close()

    return bp

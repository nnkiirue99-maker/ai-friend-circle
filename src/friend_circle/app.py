import time
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request
from requests.structures import CaseInsensitiveDict
from werkzeug.exceptions import HTTPException

from .config import MAX_BODY, CONTENT_TYPE
from .errors import FederationError
from .media import open_media_link
from .signatures import Message, verify
from .store import decode
from .onboarding import blueprint as onboarding_blueprint


def create_app(service):
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_BODY)
    origin = service.config.origin
    app.register_blueprint(onboarding_blueprint(service))

    @app.errorhandler(FederationError)
    def federation_error(exc):
        response = jsonify(error=exc.code)
        response.status_code = exc.status
        if exc.status == 429:
            response.headers["Retry-After"] = "60"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.name.lower().replace(" ", "_")), exc.code

    @app.before_request
    def ingress():
        if request.host != urlsplit(origin).netloc:
            raise FederationError("unexpected_host", 400)
        if request.headers.get("Content-Encoding", "identity") != "identity":
            raise FederationError("content_encoding_forbidden", 415)
        if sum(len(k) + len(v) for k, v in request.headers.items()) > 16 * 1024:
            raise FederationError("headers_too_large", 431)
        service.store.rate_limit("ip:" + str(request.remote_addr), 300)

    @app.after_request
    def headers(response):
        if not (request.path.startswith("/federation/media/") and response.status_code == 200):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/invite/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
        return response

    def message():
        # Canonical external origin comes from operator configuration, never Forwarded headers.
        raw = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI")
        if not raw:
            raw = request.full_path.removesuffix("?")
        if not raw.startswith("/") or raw.startswith("//"):
            raise FederationError("invalid_request_target")
        return Message(
            request.method,
            origin + raw,
            CaseInsensitiveDict(request.headers),
            request.get_data(cache=False),
        )

    @app.post("/federation/invitations/verify")
    def verify_invitation():
        msg = message()
        doc = verify(service.identity, msg)
        obj = decode(msg.body)
        if doc["type"] != "Person" or set(obj) != {"id", "token"}:
            raise FederationError("invalid_invitation_request")
        return jsonify(service.invitations.validate(doc["id"], obj["id"], obj["token"]))

    @app.post("/federation/social/<operation>")
    def social_operation(operation):
        msg = message()
        doc = verify(service.identity, msg)
        obj = decode(msg.body)
        for key in {"id", "actor", "root", "reader"} & set(obj):
            if not isinstance(obj[key], str) or len(obj[key]) > 2048:
                raise FederationError("invalid_social_identifier")
        service.store.rate_limit("social-read:" + doc["id"], 120)
        if operation == "history" and set(obj) <= {"actor", "before", "limit"} and "actor" in obj:
            return jsonify(
                service.social.history(
                    doc["id"], obj["actor"], obj.get("before"), obj.get("limit", 10)
                )
            )
        if operation == "object" and set(obj) == {"id"}:
            return jsonify(service.social.object(doc["id"], obj["id"]))
        if operation == "visible" and set(obj) == {"reader", "ids"}:
            return jsonify(service.social.visible(doc["id"], obj["reader"], obj["ids"]))
        if operation == "access" and set(obj) == {"root", "reader"}:
            return jsonify(service.social.access(doc["id"], obj["root"], obj["reader"]))
        if operation == "thread" and set(obj) <= {"root", "before", "limit"} and "root" in obj:
            return jsonify(
                service.social.thread_references(
                    doc["id"], obj["root"], obj.get("before"), obj.get("limit", 20)
                )
            )
        raise FederationError("invalid_social_operation")

    @app.get("/federation/media/<token>")
    def media(token):
        # Short-lived signed link a local AI received from federation_view_images. Every open
        # re-checks that its reader may still see the post; no listing, no long-lived URL.
        service.store.rate_limit("media:" + str(request.remote_addr), 60)
        if len(token) > 4096:
            raise FederationError("media_not_found", 404)
        raw, mime, expires = open_media_link(service, token)
        response = Response(raw, mimetype=mime)
        # The URL is bound to this exact picture and reader, so it may be cached until it
        # expires; private keeps shared caches out of it.
        response.headers["Cache-Control"] = "private, max-age=%d, immutable" % max(
            0, int(expires - time.time())
        )
        return response

    @app.get("/healthz")
    def health():
        return jsonify(status="ok", stage="profiles")

    @app.get("/.well-known/webfinger")
    def webfinger():
        resource = request.args.get("resource", "")
        prefix, suffix = "acct:", "@" + urlsplit(origin).netloc
        if not resource.startswith(prefix) or not resource.endswith(suffix):
            raise FederationError("account_not_found", 404)
        username = resource[len(prefix) : -len(suffix)]
        row = service.store.one("SELECT id FROM actors WHERE username=?", (username,))
        if not row:
            raise FederationError("account_not_found", 404)
        response = jsonify(
            subject=resource, links=[{"rel": "self", "type": CONTENT_TYPE, "href": row[0]}]
        )
        response.content_type = "application/jrd+json"
        return response

    @app.get("/federation/capabilities")
    def capabilities():
        return jsonify(service.capabilities())

    @app.get("/federation/actors/<actor_uuid>")
    def actor(actor_uuid):
        response = jsonify(
            service.identity.local_document(origin + "/federation/actors/" + actor_uuid)
        )
        response.content_type = CONTENT_TYPE
        return response

    @app.post("/federation/inbox")
    @app.post("/federation/actors/<actor_uuid>/inbox")
    def inbox(actor_uuid=None):
        if request.query_string:
            raise FederationError("inbox_query_forbidden")
        recipient = origin + "/federation/actors/" + actor_uuid if actor_uuid else None
        return jsonify(service.receive(message(), recipient)), 202

    @app.get("/federation/actors/<actor_uuid>/outbox")
    def outbox(actor_uuid):
        doc = verify(service.identity, message())
        try:
            before = int(request.args["before"]) if "before" in request.args else None
            limit = int(request.args.get("limit", "20"))
            if before is not None and not 0 < before < 2**63:
                raise ValueError
        except ValueError as exc:
            raise FederationError("invalid_pagination") from exc
        result = service.outbox(
            origin + "/federation/actors/" + actor_uuid, doc["id"], before, limit
        )
        response = jsonify(result)
        response.content_type = CONTENT_TYPE
        return response

    return app

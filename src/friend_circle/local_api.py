"""Optional host-application adapter. It does not import the host app or open its DB.

The host must supply its authenticated principal resolver. This adapter is NOT
registered on the public federation app. Production wiring is a later step.
"""

from pathlib import Path
import re

from flask import Blueprint, Response, jsonify, request

from . import projection
from .config import origin_of
from .errors import FederationError


UI_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; base-uri 'self'; form-action 'none'; "
    "frame-ancestors 'self'"
)


def make_blueprint(service, principal, ui_origin):
    if origin_of(ui_origin) != ui_origin:
        raise ValueError("canonical ui_origin required")
    bp = Blueprint("federation_local", __name__, url_prefix="/api/federation")

    @bp.errorhandler(FederationError)
    def failure(exc):
        return jsonify(error=exc.code), exc.status

    @bp.before_request
    def authorize():
        actor = principal()
        if not actor:
            raise FederationError("authentication_required", 401)
        service.relationships._human(actor)
        if request.method not in {"GET", "HEAD"}:
            if request.headers.get("Origin") != ui_origin:
                raise FederationError("csrf_origin_rejected", 403)
            if request.content_length is None or request.content_length > (
                262144 if request.path.startswith("/api/federation/social/") else 16384
            ):
                raise FederationError("invalid_request_size", 413)
            if request.mimetype != "application/json":
                raise FederationError("json_required", 415)
        service.store.rate_limit("local-api:" + actor, 120)

    def data():
        obj = request.get_json(silent=True)
        if not isinstance(obj, dict) or "actor" in obj or "author" in obj:
            raise FederationError("invalid_body_or_actor_override")
        return obj

    def field(obj, key):
        value = obj.get(key)
        if not isinstance(value, str) or len(value) > 2048:
            raise FederationError("invalid_" + key)
        return value

    def include_ai(obj):
        flag = obj.get("include_ai", True)
        if type(flag) is not bool:
            raise FederationError("invalid_include_ai")
        return flag

    def reader(obj=None):
        obj = request.args if obj is None else obj
        actor = obj.get("actor_id", principal())
        if not isinstance(actor, str) or len(actor) > 2048:
            raise FederationError("invalid_actor_id")
        service.social.authorize(principal(), actor)
        return actor

    @bp.get("/groups")
    def groups_list():
        members = service.social.friends(principal())
        actors = []
        for actor in members:
            profile = service.social.profile(principal(), actor, limit=1)["profile"]
            actors.append({k: profile[k] for k in ("actor_id", "name", "kind")})
        return jsonify(items=service.groups.list(principal()), members=members, actors=actors)

    @bp.post("/groups")
    def groups_save():
        obj = data()
        if set(obj) - {"id", "name", "members", "expected_revision"}:
            raise FederationError("invalid_group")
        return jsonify(
            service.groups.save(
                principal(),
                obj.get("name"),
                obj.get("members"),
                obj.get("id"),
                obj.get("expected_revision"),
            )
        )

    @bp.post("/groups/delete")
    def groups_delete():
        obj = data()
        if set(obj) - {"id", "expected_revision"}:
            raise FederationError("invalid_group")
        service.groups.delete(principal(), field(obj, "id"), obj.get("expected_revision"))
        return jsonify(ok=True)

    @bp.get("/social/feed")
    def social_feed():
        try:
            before = int(request.args["before"]) if "before" in request.args else None
            limit = int(request.args.get("limit", "20"))
        except ValueError as exc:
            raise FederationError("invalid_pagination") from exc
        return jsonify(service.social.feed(reader(), before, limit))

    # Bundled web UI for hosts that do not build their own. Served behind the host login like
    # every other route here: fixed file names only, no template input, no user-chosen path.
    webui = Path(__file__).parent / "resources" / "webui"
    ui_types = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
    }
    ui_files = {"index.html", "app.js", "sdk.js", "live.js", "styles.css"}

    def ui_file(path):
        response = Response(path.read_bytes(), content_type=ui_types[path.suffix])
        response.headers["Content-Security-Policy"] = UI_CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @bp.get("/ui")
    def ui_index():
        return ui_file(webui / "index.html")

    @bp.get("/ui/snapshot")
    def ui_snapshot():
        return jsonify(projection.snapshot(service, principal()))

    @bp.get("/ui/<name>")
    def ui_static(name):
        if name not in ui_files:
            raise FederationError("not_found", 404)
        return ui_file(webui / name)

    @bp.get("/ui/assets/<name>")
    def ui_asset(name):
        if not re.fullmatch(r"[a-z]{1,24}\.svg", name) or not (webui / "assets" / name).is_file():
            raise FederationError("not_found", 404)
        return ui_file(webui / "assets" / name)

    @bp.post("/social/feed/refresh")
    def social_feed_refresh():
        actor = reader(data())
        service.social.sync_feeds(reader=actor, budget=8.0)
        return jsonify(service.social.feed(actor))

    @bp.get("/social/profile")
    def profile_get():
        try:
            before = int(request.args["before"]) if "before" in request.args else None
            limit = int(request.args.get("limit", "10"))
        except ValueError as exc:
            raise FederationError("invalid_pagination") from exc
        return jsonify(
            service.social.profile(reader(), request.args.get("target", principal()), before, limit)
        )

    @bp.post("/social/refresh")
    def profile_refresh():
        obj = data()
        actor = reader(obj)
        service.social.refresh(actor, field(obj, "target"), obj.get("before"), obj.get("limit", 10))
        return jsonify(
            service.social.profile(actor, obj["target"], obj.get("before"), obj.get("limit", 10))
        )

    @bp.put("/social/profile")
    def profile_set():
        obj = data()
        return jsonify(
            service.social.set_profile(
                principal(),
                reader(obj),
                obj.get("profile"),
                obj.get("expected_revision"),
                obj.get("lease_seconds", 300),
            )
        )

    @bp.post("/social/posts")
    def post_create():
        obj = data()
        return jsonify(
            service.social.publish(
                principal(),
                reader(obj),
                obj.get("text", ""),
                obj.get("scope", {}),
                obj.get("media"),
            )
        ), 201

    @bp.put("/social/posts")
    def post_update():
        obj = data()
        return jsonify(
            service.social.update(
                principal(),
                field(obj, "id"),
                obj.get("text", ""),
                obj.get("scope", {}),
                obj.get("expected_revision"),
                obj.get("media"),
            )
        )

    @bp.post("/social/delete")
    def post_delete():
        service.social.delete(principal(), field(data(), "id"))
        return jsonify(ok=True)

    @bp.post("/social/history-grant")
    def history_grant():
        obj = data()
        service.social.history_grant(principal(), reader(obj), field(obj, "reader"), obj.get("ids"))
        return jsonify(ok=True)

    @bp.post("/social/interact")
    def interact():
        obj = data()
        return jsonify(
            service.social.interact(
                principal(),
                reader(obj),
                field(obj, "root"),
                field(obj, "kind"),
                obj.get("text", ""),
                obj.get("parent"),
            )
        ), 201

    @bp.get("/social/thread")
    def thread_get():
        return jsonify(service.social.thread(reader(), request.args.get("root", "")))

    @bp.post("/social/thread/refresh")
    def thread_refresh():
        obj = data()
        return jsonify(
            service.social.thread(
                reader(obj),
                field(obj, "root"),
                refresh=True,
                before=obj.get("before"),
                limit=obj.get("limit", 20),
            )
        )

    @bp.get("/invitations")
    def invitation_list():
        return jsonify(items=service.invitations.list(principal()))

    @bp.post("/invitations")
    def invitation_create():
        obj = data()
        if set(obj) - {"lifetime", "max_uses"}:
            raise FederationError("invalid_invitation_options")
        return jsonify(service.invitations.create(principal(), **obj)), 201

    @bp.post("/invitations/revoke")
    def invitation_revoke():
        service.invitations.revoke(principal(), field(data(), "id"))
        return jsonify(ok=True)

    @bp.post("/invitations/delete")
    def invitation_delete():
        service.invitations.delete(principal(), field(data(), "id"))
        return jsonify(ok=True)

    @bp.post("/invitations/clear")
    def invitation_clear():
        if data():
            raise FederationError("invalid_request")
        return jsonify(ok=True, removed=service.invitations.clear_inactive(principal()))

    @bp.post("/invitations/resolve")
    def invitation_resolve():
        return jsonify(service.invitations.resolve(principal(), field(data(), "invitation")))

    @bp.post("/invitations/redeem")
    def invitation_redeem():
        obj = data()
        return jsonify(
            service.invitations.redeem(
                principal(), field(obj, "invitation"), include_ai=include_ai(obj)
            )
        ), 202

    @bp.get("/me")
    def me():
        actors = [principal()] + service.relationships._companions(principal(), True)
        return jsonify(
            actor_id=principal(),
            actors=[service.social._profile(a) for a in actors],
            capabilities=service.capabilities(),
        )

    @bp.get("/notes")
    def notes():
        return jsonify(
            items=[
                dict(r)
                for r in service.store.all(
                    "SELECT remote,note FROM contact_notes WHERE owner=?", (principal(),)
                )
            ]
        )

    @bp.get("/resolve")
    def resolve():
        service.store.rate_limit("local-discovery:" + principal(), 20)
        return jsonify(service.identity.discover(request.args.get("handle", "")))

    @bp.get("/contacts")
    def contacts():
        try:
            limit = int(request.args.get("limit", "20"))
            before = int(request.args["before"]) if "before" in request.args else None
            if not 1 <= limit <= 100:
                raise ValueError
        except ValueError as exc:
            raise FederationError("invalid_pagination") from exc
        items = service.relationships.contacts(principal(), before, limit + 1)
        return jsonify(
            items=items[:limit],
            next_cursor=items[limit - 1]["cursor"] if len(items) > limit else None,
        )

    @bp.get("/visibility-policy")
    def visibility_get():
        return jsonify(
            service.visibility.get(principal(), request.args.get("actor_id", principal()))
        )

    @bp.put("/visibility-policy")
    def visibility_set():
        obj = data()
        if "visible_days" not in obj or set(obj) - {
            "actor_id",
            "visible_days",
            "expected_revision",
        }:
            raise FederationError("invalid_account_policy")
        actor = field(obj, "actor_id") if "actor_id" in obj else principal()
        return jsonify(
            service.visibility.set(
                principal(), actor, obj["visible_days"], obj.get("expected_revision")
            )
        )

    @bp.post("/friend-requests")
    def invite():
        obj = data()
        group = service.relationships.request(
            principal(), field(obj, "remote_actor_id"), include_ai=include_ai(obj)
        )
        return jsonify(group_id=group, status="pending"), 202

    @bp.post("/friend-requests/accept")
    def accept():
        obj = data()
        service.relationships.accept(
            principal(), field(obj, "group_id"), include_ai=include_ai(obj)
        )
        return jsonify(ok=True), 202

    @bp.post("/friend-requests/reject")
    def reject():
        service.relationships.reject(principal(), field(data(), "group_id"))
        return jsonify(ok=True), 202

    @bp.post("/contacts/note")
    def note():
        obj = data()
        service.relationships.note(principal(), field(obj, "remote_actor_id"), field(obj, "note"))
        return jsonify(ok=True)

    @bp.post("/contacts/remove")
    @bp.post("/contacts/block")
    def remove():
        service.relationships.remove(
            principal(), field(data(), "remote_actor_id"), block=request.path.endswith("/block")
        )
        return jsonify(ok=True), 202

    @bp.post("/contacts/unblock")
    def unblock():
        service.relationships.unblock(principal(), field(data(), "remote_actor_id"))
        return jsonify(ok=True)

    @bp.after_request
    def private_headers(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return bp

import argparse
import json
from pathlib import Path
import time

from .app import create_app
from .config import Config
from .service import Federation
from .worker import DeliveryWorker


def doctor(service):
    """Reach this server the way a stranger would: public DNS, real TLS, signed-in nothing."""
    origin, host = service.config.origin, service.config.origin[8:]
    results = []

    def check(name, action):
        try:
            action()
            results.append({"check": name, "ok": True})
        except Exception as exc:
            detail = getattr(exc, "code", None) or type(exc).__name__
            results.append({"check": name, "ok": False, "problem": str(detail)})

    def discovery():
        remote = service.http.get_json(origin + "/federation/capabilities")
        if remote.get("server_actor") != service.capabilities()["server_actor"]:
            raise ValueError("another_server_answers_for_this_domain")

    check("public capabilities at " + origin, discovery)
    for row in service.store.all(
        "SELECT id,username FROM actors WHERE kind IN ('person','ai') ORDER BY kind DESC"
    ):

        def lookup(row=row):
            found = service.http.get_json(
                origin + "/.well-known/webfinger?resource=acct:" + row["username"] + "@" + host
            )
            if [x.get("href") for x in found.get("links", [])] != [row["id"]]:
                raise ValueError("webfinger_points_elsewhere")
            if service.http.get_json(row["id"]).get("id") != row["id"]:
                raise ValueError("actor_document_mismatch")

        check("@" + row["username"] + "@" + host + " can be found", lookup)
    return results


def main():
    parser = argparse.ArgumentParser(description="Local administration; no remote admin endpoint")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--state", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    create = sub.add_parser("create-actor")
    create.add_argument("username")
    create.add_argument("--kind", choices=["person", "ai"], default="person")
    create.add_argument("--controller")
    sub.add_parser("actors")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("handle")
    probe = sub.add_parser("probe")
    probe.add_argument("--actor", required=True)
    probe.add_argument("--to", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("deliveries")
    sub.add_parser("cleanup-cache")
    sub.add_parser("sync-companions")
    sub.add_parser("doctor", help="check that other servers can discover this one")
    for name in ["invite", "remove-friend", "block-contact", "unblock-contact", "set-note"]:
        command = sub.add_parser(name)
        command.add_argument("--actor", required=True)
        command.add_argument("--remote", required=True)
        if name == "invite":
            command.add_argument("--without-ai", action="store_true")
        if name == "set-note":
            command.add_argument("note")
    for name in ["accept-invite", "reject-invite"]:
        command = sub.add_parser(name)
        command.add_argument("--actor", required=True)
        command.add_argument("--group", required=True)
        if name == "accept-invite":
            command.add_argument("--without-ai", action="store_true")
    command = sub.add_parser("contacts")
    command.add_argument("--actor", required=True)
    block = sub.add_parser("block-server")
    block.add_argument("origin")
    unblock = sub.add_parser("unblock-server")
    unblock.add_argument("origin")
    token = sub.add_parser("mcp-token", help="credentials for a local AI's own MCP access")
    token.add_argument("action", choices=["issue", "list", "revoke", "rotate"])
    token.add_argument("--actor", help="local AI actor id (issue, list)")
    token.add_argument("--id", help="credential id (revoke, rotate)")
    token.add_argument("--scopes", default="read,post,interact,settings")
    token.add_argument("--label", default="")
    token.add_argument("--days", type=int, help="optional lifetime in days")
    token.add_argument("--out", type=Path, help="write the secret to this new 0600 file")
    serve = sub.add_parser("serve-local")
    serve.add_argument("--port", type=int, default=5080)
    args = parser.parse_args()
    service = Federation(Config(args.origin, args.state))
    result = None
    if args.command == "init":
        result = service.capabilities()
    elif args.command == "create-actor":
        result = service.identity.create(args.username, args.kind, args.controller)
    elif args.command == "actors":
        result = [
            service.identity.local_document(r[0])
            for r in service.store.all("SELECT id FROM actors")
        ]
    elif args.command == "resolve":
        result = service.identity.discover(args.handle)
    elif args.command == "probe":
        result = service.probe(args.actor, args.to)
    elif args.command == "invite":
        result = {
            "group_id": service.relationships.request(
                args.actor, args.remote, include_ai=not args.without_ai
            )
        }
    elif args.command == "accept-invite":
        service.relationships.accept(args.actor, args.group, include_ai=not args.without_ai)
        result = {"ok": True}
    elif args.command == "reject-invite":
        service.relationships.reject(args.actor, args.group)
        result = {"ok": True}
    elif args.command in {"remove-friend", "block-contact"}:
        service.relationships.remove(args.actor, args.remote, block=args.command == "block-contact")
        result = {"ok": True}
    elif args.command == "unblock-contact":
        service.relationships.unblock(args.actor, args.remote)
        result = {"ok": True}
    elif args.command == "set-note":
        service.relationships.note(args.actor, args.remote, args.note)
        result = {"ok": True}
    elif args.command == "contacts":
        result = service.relationships.contacts(args.actor)
    elif args.command == "sync-companions":
        result = {"sent": service.relationships.sync_companions(force=True)}
    elif args.command == "cleanup-cache":
        result = service.social.cleanup()
    elif args.command == "worker":
        worker = DeliveryWorker(service)
        if args.once:
            result = {"processed": worker.run_once()}
        else:
            next_cleanup = time.time() + 60
            while True:
                if time.time() >= next_cleanup:
                    # Renew still-authorized friend caches first, then drop what lapsed.
                    try:
                        service.relationships.sync_companions()
                    except Exception as exc:  # never stop deliveries
                        print("companion sync failed:", type(exc).__name__, flush=True)
                    try:
                        service.social.sync_feeds()
                    except Exception as exc:  # a sync fault must never stop deliveries
                        print("feed sync failed:", type(exc).__name__, flush=True)
                    service.social.cleanup()
                    next_cleanup = time.time() + 60
                if not worker.run_once():
                    time.sleep(1)
    elif args.command == "deliveries":
        result = [
            dict(r)
            for r in service.store.all(
                "SELECT id,event_id,recipient,status,attempts,last_status,error FROM deliveries "
                "ORDER BY id DESC LIMIT 100"
            )
        ]
    elif args.command in {"block-server", "unblock-server"}:
        from .config import origin_of

        if origin_of(args.origin) != args.origin:
            parser.error("server origin must be canonical")
        with service.store.connect(write=True) as db:
            if args.command == "block-server":
                db.execute("INSERT OR IGNORE INTO blocks VALUES ('origin',?)", (args.origin,))
            else:
                db.execute("DELETE FROM blocks WHERE kind='origin' AND target=?", (args.origin,))
        result = {"ok": True}
    elif args.command == "doctor":
        result = doctor(service)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if all(item["ok"] for item in result) else 1)
    elif args.command == "mcp-token":
        from .credentials import Credentials

        credentials = Credentials(service.store)
        if args.action == "list":
            result = credentials.list(args.actor)
        elif args.action == "revoke":
            credentials.revoke(args.id)
            result = {"ok": True}
        else:
            issued = (
                credentials.rotate(args.id)
                if args.action == "rotate"
                else credentials.issue(
                    args.actor,
                    [x for x in args.scopes.split(",") if x],
                    args.label,
                    args.days * 86400 if args.days else None,
                )
            )
            if args.out:
                # Never echo the secret when a file was requested; create it private, no overwrite.
                import os

                fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(issued.pop("token") + "\n")
                issued["token_file"] = str(args.out)
            result = issued
    elif args.command == "serve-local":
        create_app(service).run(host="127.0.0.1", port=args.port, debug=False)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

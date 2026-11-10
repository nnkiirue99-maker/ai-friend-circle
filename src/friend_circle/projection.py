"""One read-only view of everything the logged-in human may see, for the bundled web UI.

Built only from the federation store and its authorized caches; it never contacts another
server and never touches the host's chat or memory data. The reader is always the human
themself, so content addressed only to their AI is not included.
"""

from datetime import datetime
import time
from urllib.parse import urlsplit

from .errors import FederationError
from .store import decode

POSTS = 30


def snapshot(service, human):
    social, store, now = service.social, service.store, time.time()
    service.relationships._human(human)
    actors = {}

    def see(actor):
        if actor in actors:
            return
        if social.local(actor):
            profile = social._profile(actor)
        else:
            row = store.one(
                "SELECT body FROM profile_cache WHERE reader=? AND actor=? AND expires>?",
                (human, actor, now),
            )
            profile = decode(row[0]) if row else {}
        actors[actor] = {
            "id": actor,
            "name": profile.get("name") or urlsplit(actor).hostname or "联邦用户",
            "kind": profile.get("kind") or "person",
            "avatar": profile.get("avatar"),
            "cover": profile.get("cover"),
            "bio": profile.get("bio", ""),
            "handle": profile.get("handle"),
            "revision": profile.get("revision"),
        }

    see(human)
    own_ai = [
        r[0] for r in store.all("SELECT id FROM actors WHERE controller=? AND kind='ai'", (human,))
    ]
    friends = social.friends(human)
    for actor in own_ai + friends:
        see(actor)

    # Posts: mine, my AIs' that I may read, and my friends' from the authorized cache.
    objects = {}
    for author in [human] + own_ai:
        for ref in social.history(human, author, None, 10)["items"]:
            try:
                objects[ref["id"]] = social.object(human, ref["id"])
            except FederationError:
                pass
    for item in social.feed(human, None, 20)["items"]:
        objects[item["id"]] = item

    def scope(obj):
        if obj["actor"] != human:
            return {"mode": "all", "ids": []}
        saved = store.one("SELECT body FROM post_scopes WHERE object_id=?", (obj["id"],))
        if saved:
            proposal = decode(saved[0])
            audience = proposal.get("audience", {})
            mode = audience.get("mode", "all")
            if proposal.get("visibility") == "self":
                return {"mode": "self", "ids": []}
            if mode == "group":
                return {"mode": "group", "ids": audience["groups"]}
            if mode == "selected":
                return {"mode": "selected", "ids": audience.get("include", [])}
            return {
                "mode": "excluded" if audience.get("exclude") else "all",
                "ids": audience.get("exclude", []),
            }
        row = store.one("SELECT visibility FROM social_objects WHERE id=?", (obj["id"],))
        readers = {
            r[0]
            for r in store.all("SELECT reader FROM object_audience WHERE object_id=?", (obj["id"],))
        } - {human}
        if not row or row[0] != "federated_friends" or not readers:
            return {"mode": "self", "ids": []}
        if readers >= set(friends):
            return {"mode": "all", "ids": []}
        return {"mode": "selected", "ids": sorted(readers & set(friends))}

    posts = []
    for obj in sorted(objects.values(), key=lambda x: x["published"], reverse=True)[:POSTS]:
        see(obj["actor"])
        likes, comments = [], []
        try:
            thread = social.thread(human, obj["id"])["items"]
        except FederationError:
            thread = []
        by_id = {x["id"]: x for x in thread}
        for item in sorted(thread, key=lambda x: x["published"]):
            see(item["actor"])
            if item["kind"] == "like":
                likes.append(item["actor"])
            else:
                parent_id = item.get("parent")
                parent = by_id.get(parent_id)
                reply_actor = parent["actor"] if parent else None
                if reply_actor is None and parent_id and parent_id != obj["id"]:
                    # Parent not on this page: its author is the id's own prefix.
                    head, sep, _ = parent_id.partition("/objects/")
                    reply_actor = head if sep else None
                comments.append(
                    {
                        "id": item["id"],
                        "actor": item["actor"],
                        "text": item["content"]["text"],
                        "replyTo": parent_id if reply_actor else None,
                        "replyActor": reply_actor,
                    }
                )
        posts.append(
            {
                "id": obj["id"],
                "actor": obj["actor"],
                "text": obj["content"]["text"],
                "media": obj["content"].get("media", []),
                "published": int(obj["published"] * 1000),
                "scope": scope(obj),
                "likes": likes,
                "comments": comments,
                "revision": obj["revision"],
            }
        )

    requests = []
    for contact in service.relationships.contacts(human, None, 100):
        if contact["status"] == "pending":
            see(contact["remote_human"])
            requests.append(
                {
                    "id": contact["id"],
                    "actor": contact["remote_human"],
                    "status": "pending",
                    "outgoing": bool(contact["initiated"]),
                    "ai_count": len(contact["remote_ai"]),
                }
            )

    invitations = [
        {
            "id": row["id"],
            # The secret is shown once at creation and is not recoverable afterwards.
            "code": datetime.fromtimestamp(row["created"]).strftime("%m-%d %H:%M") + " 创建",
            "days": max(1, round((row["expires"] - row["created"]) / 86400)),
            "uses": row["max_uses"],
            "used": row["uses"],
            "revoked": bool(row["revoked"])
            or row["expires"] <= now
            or row["uses"] >= row["max_uses"],
            "copyable": False,
        }
        for row in service.invitations.list(human)[:30]
    ]

    notifications = []
    for item in service.social.notifications(human, None, 20)["items"]:
        see(item["actor"])
        text = "赞了你的朋友圈"
        if item["kind"] == "comment":
            text = "评论了你的朋友圈" + ("：" + item["text"] if item["text"] else "")
        notifications.append(
            {
                "id": str(item["id"]),
                "actor": item["actor"],
                "text": text,
                "post": item["post"]["id"],
                "time": int(item["time"] * 1000) if item["time"] else "",
                "unread": item["unread"],
            }
        )

    return {
        "me": human,
        "mode": "live",
        "capabilities": {"public": False, "groups": True, "notifications": True, "maxImages": 2},
        "actors": list(actors.values()),
        "friends": friends,
        "ownAI": own_ai,
        "blocked": [
            r[0] for r in store.all("SELECT remote FROM contact_blocks WHERE owner=?", (human,))
        ],
        "notes": {
            r[0]: r[1]
            for r in store.all("SELECT remote,note FROM contact_notes WHERE owner=?", (human,))
        },
        "groups": service.groups.list(human),
        "profileDays": service.visibility.get(human, human)["visible_days"],
        "requests": requests,
        "invitations": invitations,
        "posts": posts,
        "notifications": notifications,
    }

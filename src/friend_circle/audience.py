"""Post recipients and account-wide rolling profile visibility are separate.

The content protocol is not enabled yet. This core never uses prompts as ACLs.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from .errors import FederationError


def days(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 36500):
        raise FederationError("invalid_visibility_days")
    return value


@dataclass(frozen=True)
class Audience:
    owner: str
    visibility: str
    recipients: frozenset[str]
    published_at: datetime

    def readable_by(self, actor, *, now, currently_allowed, account_visible_days=None):
        """Use the CURRENT policy; caller separately checks deletion/revocation.

        Author can read retained originals. Other readers need original audience
        membership AND current relationship authorization AND current account window.
        """
        duration = days(account_visible_days)
        if now.tzinfo is None:
            raise ValueError("timezone-aware clock required")
        if actor == self.owner:
            return True
        return (
            actor in self.recipients
            and actor in currently_allowed
            and (duration is None or self.published_at > now - timedelta(days=duration))
        )


def identifiers(value, field):
    if (
        not isinstance(value, list)
        or len(value) > 10000
        or any(not isinstance(x, str) for x in value)
    ):
        raise FederationError("invalid_" + field)
    if len(set(value)) != len(value):
        raise FederationError("duplicate_" + field)
    return set(value)


def calculate(
    actor, proposal, *, friends, local_actors, blocked, now=None, is_ai=False, ai_grant=None
):
    """Same audience rules for humans/AI; trusted AI grants can only narrow access."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone-aware clock required")
    if not isinstance(proposal, dict):
        raise FederationError("invalid_publish_proposal")
    if set(proposal) & {"ttl_days", "expires_at", "window_days", "visible_days", "max_ttl_days"}:
        raise FederationError("visibility_duration_is_account_setting")
    visibility = proposal.get("visibility", "self")
    if not isinstance(visibility, str) or visibility not in {"self", "local", "federated_friends"}:
        raise FederationError("invalid_visibility")
    scope = proposal.get("audience", {"mode": "all"})
    if not isinstance(scope, dict) or set(scope) - {"mode", "include", "exclude"}:
        raise FederationError("invalid_audience")
    mode = scope.get("mode", "all")
    if not isinstance(mode, str) or mode not in {"all", "selected"}:
        raise FederationError("invalid_audience_mode")
    include = identifiers(scope.get("include", []), "include")
    exclude = identifiers(scope.get("exclude", []), "exclude")
    if mode == "all" and include:
        raise FederationError("all_mode_cannot_include_list")
    base = (
        {actor}
        if visibility == "self"
        else set(local_actors)
        if visibility == "local"
        else set(friends)
    )
    if (include | exclude) - base:
        raise FederationError("audience_contains_unauthorized_actor", 403)
    selected = (base if mode == "all" else include) - exclude - set(blocked)
    if visibility == "self":
        if mode != "all" or exclude:
            raise FederationError("self_visibility_has_no_audience_options")
        selected = {actor}
    if is_ai:
        if (
            not ai_grant
            or not ai_grant.get("enabled")
            or "publish" not in ai_grant.get("actions", [])
            or ai_grant.get("revoked")
            or now.timestamp() >= ai_grant.get("expires_at", 0)
        ):
            raise FederationError("ai_publish_not_authorized", 403)
        if visibility not in ai_grant.get("visibilities", []):
            raise FederationError("ai_visibility_not_authorized", 403)
        if selected - set(ai_grant.get("allowed_actor_ids", [])) - {actor}:
            raise FederationError("ai_audience_exceeds_grant", 403)
    return Audience(actor, visibility, frozenset(selected), now)


class AccountVisibility:
    """Global per-Actor profile window, controlled by its authenticated human owner."""

    def __init__(self, store):
        self.store = store
        self.on_change = None

    def _authorize(self, principal, actor):
        """Read and administer: the identity itself, or the human who controls that AI."""
        owner = self.store.one("SELECT kind FROM actors WHERE id=?", (principal,))
        row = self.store.one("SELECT kind,controller FROM actors WHERE id=?", (actor,))
        if (
            not owner
            or not row
            or owner[0] not in {"person", "ai"}
            or not (
                actor == principal
                or owner[0] == "person"
                and row["kind"] == "ai"
                and row["controller"] == principal
            )
        ):
            raise FederationError("account_policy_forbidden", 403)

    def _authorize_voice(self, principal, actor):
        """Speak and decide who sees it: only the identity itself, never its controller.

        A human reaches this as themself through the host login; an AI reaches it only
        through its own MCP credential. Neither can borrow the other's voice.
        """
        row = self.store.one("SELECT kind FROM actors WHERE id=?", (actor,))
        if principal != actor or not row or row[0] not in {"person", "ai"}:
            raise FederationError("actor_must_act_for_itself", 403)

    def get(self, principal, actor):
        self._authorize(principal, actor)
        row = self.store.one(
            "SELECT visible_days,revision FROM account_visibility WHERE actor=?", (actor,)
        )
        return {
            "actor_id": actor,
            "visible_days": row[0] if row else None,
            "revision": row[1] if row else 0,
        }

    def set(self, principal, actor, visible_days, expected_revision):
        self._authorize_voice(principal, actor)
        visible_days = days(visible_days)
        if type(expected_revision) is not int or expected_revision < 0:
            raise FederationError("invalid_policy_revision")
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT visible_days,revision FROM account_visibility WHERE actor=?", (actor,)
            ).fetchone()
            current = row[1] if row else 0
            if current != expected_revision:
                raise FederationError("policy_revision_conflict", 409)
            if row and row[0] == visible_days:
                return {"actor_id": actor, "visible_days": visible_days, "revision": current}
            revision = current + 1
            db.execute(
                "INSERT INTO account_visibility VALUES (?,?,?) ON CONFLICT(actor) DO UPDATE SET "
                "visible_days=excluded.visible_days,revision=excluded.revision",
                (actor, visible_days, revision),
            )
            if self.on_change:
                self.on_change(db, actor)
        return {"actor_id": actor, "visible_days": visible_days, "revision": revision}

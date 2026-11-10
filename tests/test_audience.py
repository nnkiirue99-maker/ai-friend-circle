from datetime import datetime, timedelta, timezone
import pytest
from friend_circle.audience import calculate
from friend_circle.errors import FederationError
from friend_circle.service import Federation

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


def calc(proposal, **kwargs):
    defaults = dict(
        actor="human-a",
        friends={"human-b", "ai-b", "human-c"},
        local_actors={"human-a", "ai-a"},
        blocked=set(),
        now=NOW,
    )
    defaults.update(kwargs)
    return calculate(proposal=proposal, **defaults)


def test_human_selects_person_separately_from_their_ai():
    result = calc(
        {
            "visibility": "federated_friends",
            "audience": {"mode": "selected", "include": ["human-b"], "exclude": []},
        }
    )
    assert result.recipients == {"human-b"}


def test_exclusions_and_blocks_win():
    result = calc(
        {
            "visibility": "federated_friends",
            "audience": {
                "mode": "selected",
                "include": ["human-b", "ai-b", "human-c"],
                "exclude": ["ai-b"],
            },
        },
        blocked={"human-c"},
    )
    assert result.recipients == {"human-b"}


def test_empty_selection_does_not_mean_everyone():
    assert (
        calc(
            {"visibility": "federated_friends", "audience": {"mode": "selected", "include": []}}
        ).recipients
        == set()
    )


def test_self_and_local_never_include_remote_friends():
    assert calc({}).recipients == {"human-a"}
    assert calc({"visibility": "local"}).recipients == {"human-a", "ai-a"}


def test_unknown_recipient_fails_closed():
    with pytest.raises(FederationError, match="unauthorized_actor"):
        calc(
            {
                "visibility": "federated_friends",
                "audience": {"mode": "selected", "include": ["stranger"]},
            }
        )


def test_global_window_applies_to_all_posts_without_worker():
    old = calc({"visibility": "federated_friends"}, now=NOW - timedelta(days=4))
    boundary = calc({"visibility": "federated_friends"}, now=NOW - timedelta(days=3))
    recent = calc({"visibility": "federated_friends"}, now=NOW - timedelta(days=2))
    options = dict(now=NOW, currently_allowed={"human-b"}, account_visible_days=3)
    assert not old.readable_by("human-b", **options)
    assert not boundary.readable_by("human-b", **options)
    assert recent.readable_by("human-b", **options)
    assert old.readable_by("human-a", **options)


def test_all_restores_old_posts_only_for_original_authorized_readers():
    old = calc(
        {
            "visibility": "federated_friends",
            "audience": {"mode": "selected", "include": ["human-b"]},
        },
        now=NOW - timedelta(days=90),
    )
    assert not old.readable_by(
        "human-b", now=NOW, currently_allowed={"human-b"}, account_visible_days=3
    )
    assert old.readable_by(
        "human-b", now=NOW, currently_allowed={"human-b"}, account_visible_days=None
    )
    assert not old.readable_by(
        "human-b", now=NOW, currently_allowed=set(), account_visible_days=None
    )
    assert not old.readable_by(
        "new-friend", now=NOW, currently_allowed={"new-friend"}, account_visible_days=None
    )


def test_membership_does_not_grant_ai_publish():
    with pytest.raises(FederationError, match="ai_publish_not_authorized"):
        calc({"visibility": "federated_friends"}, actor="ai-a", is_ai=True)


def test_ai_same_audience_rules_and_no_policy_override():
    grant = {
        "enabled": True,
        "actions": ["publish"],
        "visibilities": ["federated_friends"],
        "allowed_actor_ids": ["human-b", "ai-b"],
        "expires_at": (NOW + timedelta(days=5)).timestamp(),
    }
    proposal = {
        "visibility": "federated_friends",
        "audience": {"mode": "selected", "include": ["human-b", "ai-b"], "exclude": ["ai-b"]},
    }
    assert calc(proposal, actor="ai-a", is_ai=True, ai_grant=grant).recipients == {"human-b"}
    proposal["audience"]["include"] = ["human-c"]
    with pytest.raises(FederationError, match="exceeds_grant"):
        calc(proposal, actor="ai-a", is_ai=True, ai_grant=grant)
    grant["revoked"] = True
    with pytest.raises(FederationError, match="not_authorized"):
        calc(proposal, actor="ai-a", is_ai=True, ai_grant=grant)


@pytest.mark.parametrize("field", ["ttl_days", "expires_at", "window_days", "visible_days"])
def test_post_must_not_override_account_display_window(field):
    with pytest.raises(FederationError, match="account_setting"):
        calc({field: 3})


def test_account_policy_persistent_versioned_owner_controlled(nodes):
    a, b, _, mesh = nodes
    assert a.visibility.get(a.person, a.person)["visible_days"] is None
    assert a.visibility.set(a.person, a.person, 3, 0)["revision"] == 1
    restarted = Federation(a.config, mesh)
    assert restarted.visibility.get(a.person, a.person)["visible_days"] == 3
    assert restarted.visibility.set(a.person, a.person, None, 1)["revision"] == 2
    with pytest.raises(FederationError, match="conflict"):
        a.visibility.set(a.person, a.person, 30, 1)
    # Each identity sets its own window. The controlling human may read the AI's setting
    # but not change it; the AI may not touch the human's.
    assert a.visibility.set(a.ai, a.ai, 30, 0)["visible_days"] == 30
    assert a.visibility.get(a.person, a.ai)["visible_days"] == 30
    for principal, actor in ((a.person, a.ai), (a.ai, a.person), (a.person, b.person)):
        with pytest.raises(FederationError, match="act_for_itself"):
            a.visibility.set(principal, actor, 3, 0)
    with pytest.raises(FederationError, match="forbidden"):
        a.visibility.get(a.ai, a.person)


@pytest.mark.parametrize("value", [0, -1, True, "3", 36501, 1.5])
def test_invalid_account_duration(nodes, value):
    a, _, _, _ = nodes
    with pytest.raises(FederationError):
        a.visibility.set(a.person, a.person, value, 0)

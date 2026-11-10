"""Read-only capability smoke check for another implementation, no credentials."""

import argparse
import json
from friend_circle.config import PROFILE, origin_of
from friend_circle.network import SafeHTTP


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    args = parser.parse_args()
    if origin_of(args.origin) != args.origin:
        parser.error("canonical HTTPS origin required")
    caps = SafeHTTP().get_json(args.origin + "/federation/capabilities")
    expected = {"rfc9421-ed25519", "confirmed-friends", "invitations/1", "private-profiles/1"}
    missing = expected - set(caps.get("capabilities", []))
    passed = caps.get("profile") == PROFILE and not missing
    print(
        json.dumps(
            {
                "passed": passed,
                "missing": sorted(missing),
                "profile": caps.get("profile"),
                "scope": "discovery only; run full signed multi-node tests separately",
            },
            indent=2,
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

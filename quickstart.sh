#!/usr/bin/env bash
# One command from a fresh clone: build the verified install bundle, then run deploy/setup.sh
# with the same arguments. Run as root on the server.
#
#   sudo ./quickstart.sh --check --domain social.example.com
#   sudo ./quickstart.sh --domain social.example.com --human alice --ai nova
#   sudo ./quickstart.sh            # upgrade in place later (identities, friends, posts kept)
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$candidate")"; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.11 or newer is required" >&2; exit 1; }

# The bundle is what this server will also serve to the friends it invites, so it is built
# from exactly this checkout (deterministic: same sources, same SHA-256).
"$PY" scripts/build_bundle.py >/dev/null
cp src/friend_circle/resources/reference.zip src/friend_circle/resources/bundle.json deploy/
exec deploy/setup.sh "$@"

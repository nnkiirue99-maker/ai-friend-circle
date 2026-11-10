#!/usr/bin/env bash
# AI Friend Circle: one-command install for any Linux server with systemd and Python 3.11+.
# It installs only the federation services, isolated under their own user. It never edits
# your web server, your app, or your AI's configuration; it writes ready-made snippets for
# those three steps into /etc/friend-circle/host-kit and tells you what remains.
#
#   ./setup.sh --check  --domain social.example.com
#   ./setup.sh --domain social.example.com --human alice --ai nova [--host-user www-data]
#   ./setup.sh                      upgrade an existing install in place (keeps identities)
#   ./setup.sh --doctor             after wiring the web server: test public reachability
#   ./setup.sh --uninstall [--purge]
#
# --host-user  the OS user your app and AI runtime run as, when it is not root. It gets
#              access to the private socket and its own copies of the two local credentials.
set -euo pipefail

APP="/opt/friend-circle"; STATE="/var/lib/friend-circle"; ETC="/etc/friend-circle"
KIT="$ETC/host-kit"; BACKUPS="/var/backups/friend-circle"
PORT=5090; MCP_PORT=5092
UNITS="public private worker mcp"
HERE="$(cd "$(dirname "$0")" && pwd)"
BUNDLE="${HERE}/reference.zip"
MODE="install"; DOMAIN=""; HUMAN=""; AI=""; HOST_USER=""; PURGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE="check" ;;
    --doctor) MODE="doctor" ;;
    --uninstall) MODE="uninstall" ;;
    --purge) PURGE=1 ;;
    --domain) DOMAIN="$2"; shift ;;
    --human) HUMAN="$2"; shift ;;
    --ai) AI="$2"; shift ;;
    --host-user) HOST_USER="$2"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\n== %s\n' "$*"; }
die() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
command -v systemctl >/dev/null || die "systemd is required"

if [ "$MODE" = "uninstall" ]; then
  for unit in $UNITS; do
    systemctl disable --now "friend-circle-${unit}" 2>/dev/null || true
    rm -f "/etc/systemd/system/friend-circle-${unit}.service"
  done
  rm -rf /etc/systemd/system/friend-circle-private.service.d
  systemctl daemon-reload
  if [ "$PURGE" = 1 ]; then
    rm -rf "$APP" "$ETC" "$STATE"; userdel fcircle 2>/dev/null || true
    echo "removed services, code, config, identities and keys"
  else
    echo "services removed; kept $STATE (identities, keys, data), $ETC and $APP"
  fi
  echo "Also remove what you added yourself: the web server include, the app proxy, the AI's MCP entry."
  exit 0
fi

# Existing install: take the domain from its own configuration.
if [ -z "$DOMAIN" ] && [ -f "$ETC/env" ]; then
  DOMAIN="$(sed -n 's#^FC_ORIGIN=https://##p' "$ETC/env")"
fi
[ -n "$DOMAIN" ] || die "--domain is required on first install (the public HTTPS name of this server)"
case "$DOMAIN" in *[!a-z0-9.-]*|.*|*.|*..*) die "domain must be a plain lowercase host name, without https:// or a path" ;; esac
ORIGIN="https://${DOMAIN}"

PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$candidate")"; break
  fi
done

fed() { runuser -u fcircle -- "$APP/venv/bin/friend-circle" --origin "$ORIGIN" --state "$STATE" "$@"; }

if [ "$MODE" = "doctor" ]; then
  [ -x "$APP/venv/bin/friend-circle" ] || die "not installed"
  fed doctor
  exit $?
fi

say "Preflight (read-only)"
[ -n "$PY" ] || die "Python 3.11 or newer is required (install python3.11, then re-run)"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || die "$PY lacks venv/ensurepip (on Debian/Ubuntu: apt install python3-venv)"
[ -f "$BUNDLE" ] && [ -f "${HERE}/bundle.json" ] || die "reference.zip and bundle.json must sit next to this script"
WANT="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "${HERE}/bundle.json")"
HAVE="$( (sha256sum "$BUNDLE" 2>/dev/null || shasum -a 256 "$BUNDLE") | cut -d' ' -f1)"
[ "$WANT" = "$HAVE" ] || die "reference.zip does not match bundle.json; download both again"
if [ -n "$HOST_USER" ]; then id "$HOST_USER" >/dev/null 2>&1 || die "user $HOST_USER does not exist"; fi
EXISTING=0; [ -d "$STATE" ] && EXISTING=1
if [ "$EXISTING" = 0 ]; then
  [ -n "$HUMAN" ] && [ -n "$AI" ] || die "--human and --ai account names are required on first install"
  for name in "$HUMAN" "$AI"; do
    case "$name" in *[!a-z0-9_]*|"") die "account names use lowercase letters, digits and _ only" ;; esac
  done
  for port in $PORT $MCP_PORT; do
    if command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ":${port} "; then die "port ${port} is already in use"; fi
  done
fi
echo "python:  $("$PY" --version) at $PY"
echo "domain:  ${DOMAIN}   bundle sha256: ${HAVE}"
echo "mode:    $([ "$EXISTING" = 1 ] && echo 'upgrade in place (identities and data kept)' || echo "first install as @${HUMAN}@${DOMAIN} with AI @${AI}@${DOMAIN}")"
echo "web:     $(command -v nginx >/dev/null && echo 'nginx found (a snippet will be written; you add one include line)' || echo 'no nginx found; proxy the three public paths with your own web server')"
[ -n "$HOST_USER" ] && echo "host:    $HOST_USER gets socket access and its own credential copies"
if [ "$MODE" = "check" ]; then say "Preflight passed. Nothing was changed."; exit 0; fi

if [ "$EXISTING" = 1 ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  say "Backup -> ${BACKUPS}/${STAMP}"
  install -d -m 0700 "${BACKUPS}/${STAMP}"
  # Stop writers first so the copied database and its WAL are consistent.
  for unit in $UNITS; do systemctl stop "friend-circle-${unit}" 2>/dev/null || true; done
  cp -a "$STATE" "${BACKUPS}/${STAMP}/state"
  [ -d "$APP/src" ] && cp -a "$APP/src" "${BACKUPS}/${STAMP}/src" || true
  [ -d "$ETC" ] && cp -a "$ETC" "${BACKUPS}/${STAMP}/etc" || true
fi

say "Service account and directories"
id fcircle >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --shell "$(command -v nologin || echo /bin/false)" fcircle
install -d -m 0755 -o root -g root "$APP"
install -d -m 0700 -o fcircle -g fcircle "$STATE"
install -d -m 0750 -o root -g fcircle "$ETC"

say "Code and isolated virtualenv (downloads dependencies from PyPI)"
rm -rf "$APP/src.new"; mkdir "$APP/src.new"
"$PY" -m zipfile -e "$BUNDLE" "$APP/src.new" >/dev/null
# Serve the exact verified archive to the people you invite later.
cp "$BUNDLE" "${HERE}/bundle.json" "$APP/src.new/ai-friend-circle/src/friend_circle/resources/"
[ -d "$APP/venv" ] || "$PY" -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet --force-reinstall --no-deps "$APP/src.new/ai-friend-circle"
"$APP/venv/bin/pip" install --quiet "$APP/src.new/ai-friend-circle[deploy]"
rm -rf "$APP/src"; mv "$APP/src.new" "$APP/src"
chmod -R go-w "$APP"
SRC="$APP/src/ai-friend-circle"

say "Configuration and identities"
if [ ! -f "$ETC/gateway-token" ]; then
  ( umask 077; "$PY" -c 'import secrets; print(secrets.token_urlsafe(48))' > "$ETC/gateway-token" )
fi
chown fcircle:fcircle "$ETC/gateway-token"; chmod 0600 "$ETC/gateway-token"
cat > "$ETC/env" <<EOF
FC_ORIGIN=${ORIGIN}
FC_STATE=${STATE}
FC_GATEWAY_TOKEN_FILE=${ETC}/gateway-token
FC_UI_ORIGIN=${ORIGIN}
EOF
chown root:fcircle "$ETC/env"; chmod 0640 "$ETC/env"
fed init >/dev/null
pick() { fed actors | "$PY" -c 'import json,sys; a=[x["id"] for x in json.load(sys.stdin) if x["type"]==sys.argv[1]]; print(a[0] if a else "")' "$1"; }
PERSON="$(pick Person)"
if [ -z "$PERSON" ]; then
  PERSON="$(fed create-actor "$HUMAN" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  fed create-actor "$AI" --kind ai --controller "$PERSON" >/dev/null
  echo "created @${HUMAN}@${DOMAIN} and AI @${AI}@${DOMAIN}"
else
  echo "keeping existing identities"
fi
AI_ACTOR="$(pick Service)"
install -d -m 0700 -o fcircle -g fcircle "$ETC/mcp"
MCP_TOKEN="$ETC/mcp/ai.token"
if [ -n "$AI_ACTOR" ] && [ ! -f "$MCP_TOKEN" ]; then
  fed mcp-token issue --actor "$AI_ACTOR" --label "host runtime" --out "$MCP_TOKEN" >/dev/null
  echo "issued a local MCP credential for the AI (never shared with any other server)"
fi

# Where the host app reads its side of the private channel.
HOST_ETC="$ETC"; HOST_GATEWAY_TOKEN="$ETC/gateway-token"; HOST_MCP_TOKEN="$MCP_TOKEN"; OWNER="root"
if [ -n "$HOST_USER" ] && [ "$HOST_USER" != root ]; then
  OWNER="$HOST_USER"; HOST_ETC="$ETC/host"
  usermod -aG fcircle "$HOST_USER"
  install -d -m 0700 -o "$HOST_USER" "$HOST_ETC"
  install -m 0600 -o "$HOST_USER" "$ETC/gateway-token" "$HOST_ETC/gateway-token"
  [ -f "$MCP_TOKEN" ] && install -m 0600 -o "$HOST_USER" "$MCP_TOKEN" "$HOST_ETC/mcp-ai.token"
  HOST_GATEWAY_TOKEN="$HOST_ETC/gateway-token"; HOST_MCP_TOKEN="$HOST_ETC/mcp-ai.token"
  install -d -m 0755 /etc/systemd/system/friend-circle-private.service.d
  cat > /etc/systemd/system/friend-circle-private.service.d/host-access.conf <<EOF
[Service]
RuntimeDirectoryMode=0750
ExecStart=
ExecStart=${APP}/venv/bin/gunicorn --bind unix:/run/friend-circle/gateway.sock --umask 0o007 --workers 2 --threads 8 --timeout 60 --log-level warning friend_circle.wsgi:private_app()
EOF
fi
cat > "$HOST_ETC/host.json" <<EOF
{"socket": "/run/friend-circle/gateway.sock", "token_file": "${HOST_GATEWAY_TOKEN}", "person": "${PERSON}", "ui_origin": "${ORIGIN}"}
EOF
chown "$OWNER" "$HOST_ETC/host.json"; chmod 0600 "$HOST_ETC/host.json"

say "Services"
for unit in $UNITS; do
  install -m 0644 "${SRC}/deploy/friend-circle-${unit}.service" "/etc/systemd/system/friend-circle-${unit}.service"
done
systemctl daemon-reload
for unit in $UNITS; do systemctl enable --quiet "friend-circle-${unit}"; systemctl restart "friend-circle-${unit}"; done
probe() { curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || true; }
PING='{"jsonrpc":"2.0","id":1,"method":"ping"}'
MCP_URL="http://127.0.0.1:${MCP_PORT}/mcp"
for i in $(seq 1 25); do
  [ "$(probe -H "Host: ${DOMAIN}" "http://127.0.0.1:${PORT}/healthz")" = 200 ] && [ -S /run/friend-circle/gateway.sock ] \
    && [ "$(probe -X POST -H 'Content-Type: application/json' -d "$PING" "$MCP_URL")" = 401 ] && break
  sleep 1
done
[ "$(probe -H "Host: ${DOMAIN}" "http://127.0.0.1:${PORT}/healthz")" = 200 ] || die "public listener did not start: journalctl -u friend-circle-public"
[ -S /run/friend-circle/gateway.sock ] || die "private gateway did not start: journalctl -u friend-circle-private"
[ "$(probe -X POST -H 'Content-Type: application/json' -d "$PING" "$MCP_URL")" = 401 ] || die "MCP endpoint did not start: journalctl -u friend-circle-mcp"
if [ -f "$MCP_TOKEN" ]; then
  [ "$(probe -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $(cat "$MCP_TOKEN")" -d "$PING" "$MCP_URL")" = 200 ] \
    || die "MCP endpoint rejected the AI credential"
fi
echo "public listener, private gateway, delivery worker and MCP endpoint are running"

say "Host kit -> ${KIT}"
install -d -m 0755 "$KIT"
install -m 0644 "${SRC}/deploy/nginx-locations.conf" "$KIT/nginx-locations.conf"
install -m 0644 "${SRC}/adapters/host_proxy.py" "$KIT/federation_host_proxy.py"
install -m 0644 "${SRC}/src/friend_circle/resources/host-contract.md" "$KIT/README.md"
cat > "$KIT/mcp-entry.json" <<EOF
{"name": "federation", "url": "${MCP_URL}", "transport": "streamable_http", "auth_token_file": "${HOST_MCP_TOKEN}"}
EOF

cat <<EOF

== Installed. Three things remain, in your own stack (details: ${KIT}/README.md)

1. Web server: send these public paths to http://127.0.0.1:${PORT}, keeping the Host header:
     /.well-known/webfinger   /federation/   /invite/
   nginx: put this line inside your HTTPS server block for ${DOMAIN}, then reload:
     include ${KIT}/nginx-locations.conf;
   Then run:  ${HERE}/setup.sh --doctor

2. Your app: forward /api/federation/* for the LOGGED-IN user to the private socket.
   Flask: copy ${KIT}/federation_host_proxy.py next to your app and call
     install(app, config_path="${HOST_ETC}/host.json", authenticated=<your login check>)
   Any other stack: ${KIT}/README.md shows the same forward in a few lines.
   Then add one button in your frontend that opens  /api/federation/ui

3. Your AI: register the MCP endpoint from ${KIT}/mcp-entry.json in its MCP client.
   The credential file is ${HOST_MCP_TOKEN}; it belongs to this server only.
EOF

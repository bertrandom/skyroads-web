#!/usr/bin/env bash
# Build the web version and deploy it, with the ghost relay, to the server.
#
#   ./deploy.sh              build and deploy the game (and the relay if it changed)
#   DEPLOY_HOST=other ./deploy.sh
#
# The server keeps the game in /web/skyroads and the relay in
# /web/skyroads-relay (see server/README.md), both owned by the `web` user.
# The relay key (relay.key) is read from the server and handed to the build
# in the environment only, so it never lands in a file or a command line.
set -euo pipefail

HOST=${DEPLOY_HOST:-nuke}
SITE=${DEPLOY_SITE:-https://skyroads.smittn.com}
RELAY_URL=${GHOST_RELAY:-wss://${SITE#https://}/skyroads-relay}
GAME_DIR=/web/skyroads
RELAY_DIR=/web/skyroads-relay

cd "$(dirname "$0")"
step() { printf '\n== %s\n' "$*"; }

step "Reading the relay key from $HOST"
GHOST_KEY=$(ssh -o BatchMode=yes "$HOST" "sudo -n cat $RELAY_DIR/relay.key")
if ! [[ $GHOST_KEY =~ ^[0-9a-f]{64}$ ]]; then
    echo "error: $HOST:$RELAY_DIR/relay.key is not a 64-digit hex key" >&2
    exit 1
fi

step "Building web/dist for $RELAY_URL"
GHOST_KEY=$GHOST_KEY GHOST_RELAY=$RELAY_URL make -C web
unset GHOST_KEY
grep -q "\"$RELAY_URL\"" web/dist/relay.js || { echo "error: relay.js has the wrong URL" >&2; exit 1; }

step "Copying to $HOST"
STAGE=$(ssh -o BatchMode=yes "$HOST" 'mktemp -d /tmp/skyroads-deploy.XXXXXX')
trap 'ssh -o BatchMode=yes "$HOST" "rm -rf $STAGE" || true' EXIT
rsync -a --delete web/dist/ "$HOST:$STAGE/game/"
rsync -a server/relay.py "$HOST:$STAGE/relay.py"

step "Installing on $HOST"
ssh -o BatchMode=yes "$HOST" STAGE="$STAGE" GAME_DIR="$GAME_DIR" RELAY_DIR="$RELAY_DIR" bash -s <<'REMOTE'
set -euo pipefail
sudo -n rsync -a --delete --chown=web:web "$STAGE/game/" "$GAME_DIR/"
echo "game: $GAME_DIR updated"
if sudo -n cmp -s "$STAGE/relay.py" "$RELAY_DIR/relay.py"; then
    echo "relay: unchanged, left running"
else
    backup="$RELAY_DIR/stats.db.bak-$(date +%Y%m%d-%H%M%S)"
    sudo -n -u web cp "$RELAY_DIR/stats.db" "$backup"
    echo "relay: backed up the statistics to $backup"
    sudo -n install -o web -g web -m 644 "$STAGE/relay.py" "$RELAY_DIR/relay.py"
    sudo -n systemctl restart skyroads-relay
    sleep 1.5
    if ! systemctl is-active --quiet skyroads-relay; then
        echo "error: skyroads-relay did not come back up:" >&2
        sudo -n journalctl -u skyroads-relay -n 20 --no-pager >&2
        exit 1
    fi
    echo "relay: updated and restarted"
fi
REMOTE

step "Checking $SITE"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$SITE/")
remote_relay=$(curl -sS "$SITE/relay.js")
if [[ $code != 200 || $remote_relay != *"$RELAY_URL"* ]]; then
    echo "error: $SITE answered $code, relay.js: $remote_relay" >&2
    exit 1
fi
if ! cmp -s <(curl -sS "$SITE/index.wasm") web/dist/index.wasm; then
    echo "error: $SITE serves a different index.wasm than the one just built" >&2
    exit 1
fi
echo "ok: $SITE serves this build"

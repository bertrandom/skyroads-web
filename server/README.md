# SkyRoads live ghost relay

A small WebSocket relay that lets players of the web build race the live
ghosts of other people. While you race, your ship's position is streamed to
the relay. When you start a road, you get the most recent run of every other
player who is on that road right now and started it in the previous 5
minutes. When a player leaves the road or goes offline, their ghost is
removed. That includes what they
had done so far and, while they are still racing, their live updates. The
game replays each run at the same run time as yours: someone who started 20 s
before you shows up where they were 20 s into their run.

Each player gets a name of two harmless random words (`plucky-otter`). The
relay derives it from the browser's anonymous id, so it stays the same across
reloads. Players can choose their own name in the settings menu instead:
3-20 letters, digits and single dashes, starting and ending with a letter or
digit, and not already used by someone online (ignoring case). The relay
checks these rules itself; an empty name goes back to the random one. While you race, the players on your road are
listed over the top left of the game, each with the colour of their ghost.
Players stay listed for 15 s after a run (`GRACE`), long enough for the game
to restart the road after a crash. The settings menu also shows how many
players are connected to the relay in total (one per browser).

The relay also keeps all-time statistics in SQLite (`stats.db` next to
`relay.py`, or `--db PATH`; `--db ""` turns them off): every player id it has
seen, per player and road the number of races started, roads finished
and seconds played (by the game clock, capped at the real time), and per
road the fastest finish (game clock to the finish line) with its player,
plus each player's own fastest finish per road, each with the time it was
set. Every road record ever set is kept in `record_history`, so a record
that gets beaten is still on file. When a finish sets the road's all-time
record or beats the player's personal best (not on their first finish of a
road), the relay tells them and the page shows a Congratulations! modal.

A finished road only counts when the run checks out: the relay looks at the
run's own frames (starts at the start line, clock never goes backwards,
never faster than the game's top speed, reaches the end of the road where
the game says) and takes the finish and playing times from them. The end
message is also signed by the game (HMAC-SHA256 over a per-run nonce from
the relay, the road and the run clock) with a key shared by the relay and
the build: `relay.key` next to `relay.py` (hex; `--key-file` to move it,
not in git), and the same key passed to the web build as `GHOST_KEY`.
Without a key file the relay skips the signature check. This stops casual
cheating only: the key can be dug out of the WebAssembly, and a carefully
faked run can still pass. Finishing a
road is reported by the page when the game counts it in its save. The
settings menu's Statistics shows the totals for everyone and for you.

`relay.py` uses only the Python 3 standard library (Ubuntu 24.04 ships Python
3.12). It keeps everything in memory and forgets runs after 6 minutes. It
stores no identities: each browser sends a random id (kept in its
localStorage) only so that you are never shown your own runs, even after a
reload.

## Accounts (passkeys)

Players can create an account with a passkey and sign in with it in other
browsers (the settings menu's Account). The relay verifies the WebAuthn
registrations and sign-ins itself (discoverable credentials, attestation
"none", signatures checked with `python3-cryptography`: ES256, EdDSA,
RS256) and keeps accounts, passkeys and signed-in browsers in `stats.db`.
Signing in links the browser's anonymous id to the account; its statistics,
personal bests and records move to the account (added together, fastest
kept). While signed in the account keeps the name, controls, sound and road
completions (highest count per road) and pushes changes to every browser
signed in to it; the game re-reads them without a reload. Passkeys are bound
to the page's host name: only the origins given with `--origin` (default
`https://skyroads.org`, `https://skyroads.smittn.com` and
`http://localhost:8000`) may use them, so
moving the game to another host means creating new passkeys there. Without
`python3-cryptography` the relay runs with accounts turned off.

## Install (Ubuntu 24.04 + nginx)

Two servers, set up the same way:

| | host | site | nginx config | certificate |
|---|---|---|---|---|
| staging | nuke | https://skyroads.smittn.com/ | `nginx-skyroads.smittn.com.conf` | `*.smittn.com` |
| production | server | https://skyroads.org/ (www redirects) | `nginx-skyroads.org.conf` | `skyroads.org`, `*.skyroads.org` |

On each, the game is in `/web/skyroads` and the relay in `/web/skyroads-relay`,
both owned by and running as `web`, each relay with its own `relay.key` and
`stats.db`. The relay listens on the unix socket
`/web/skyroads-relay/skyroads-relay.sock`, which nginx proxies at
`/skyroads-relay`.

First install (shown for production; on nuke use the smittn.com config):

```sh
sudo install -d -o web -g web -m 755 /web/skyroads /web/skyroads-relay
sudo install -o web -g web -m 644 relay.py /web/skyroads-relay/
sudo install -m 644 skyroads-relay.service /etc/systemd/system/
sudo -u web sh -c 'umask 077; openssl rand -hex 32 > /web/skyroads-relay/relay.key'
sudo systemctl daemon-reload && sudo systemctl enable --now skyroads-relay
sudo certbot certonly --dns-dnsmadeeasy \
     --dns-dnsmadeeasy-credentials /root/dnsmadeeasy/dnsmadeeasy.ini \
     -d skyroads.org -d '*.skyroads.org'
sudo install -m 644 nginx-skyroads.org.conf /etc/nginx/sites-available/skyroads.org
sudo ln -s ../sites-available/skyroads.org /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Updates: run `./deploy.sh` (staging) or `./deploy.sh production` from the top
of the repo. It reads the relay key from the server over ssh (it only ever lives in the environment of `make`), builds
`web/dist` for that site's `wss://…/skyroads-relay`, copies it to
`/web/skyroads/` as `web`, and checks the live site serves the new
`index.wasm`. If `nginx-<site>.conf` changed it installs it in `/etc/nginx/sites-available/` and reloads nginx (putting the old one back if `nginx -t` fails). If `relay.py` changed it also backs up `stats.db` to
`stats.db.bak-DATE`, installs it and restarts `skyroads-relay` (live runs in
memory are lost); otherwise the relay is left running. `DEPLOY_HOST`,
`DEPLOY_SITE` and `GHOST_RELAY` override the host, site and relay URL.

`GHOST_RELAY` is written to `dist/relay.js`. A relay can also be tried
without rebuilding with `?relay=wss://…` on the page URL. Without a relay URL
the web game shows no ghosts.

## Local test

```sh
python3 relay.py                      # ws://127.0.0.1:8765 (or --unix PATH)
cd ../web && make serve               # http://localhost:8000/?relay=ws://127.0.0.1:8765
```

Open the page in two browsers. Start a road in one and race for a bit, then
start the same road in the other.

## Limits

| | |
|---|---|
| window | runs started up to 300 s before yours are shown (`WINDOW`) |
| ghosts | each other player's most recent run, up to 50 players (`MAX_RUNS_SENT`) |
| run length | 60 000 frames, about 25 minutes (`MAX_FRAMES`) |
| traffic | about 0.5 KB/s up per racing player (36 frames/s), the same down per ghost |

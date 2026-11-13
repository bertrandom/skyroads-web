#!/usr/bin/env python3
"""Live ghost relay for the SkyRoads web build.

Players stream their ship position while they race; a player who starts a
road receives the most recent run of each other player who is on the same road
right now (listed, see GRACE) and started it during the previous WINDOW
seconds: what has been recorded so far, then live updates.  When that player
leaves the road (or disconnects) the ghost is withdrawn.
The game replays each of them at the same run time as the local player, so
someone who started 20 s earlier is shown where they were 20 s into their run.

Python 3 standard library only.  Listens on 127.0.0.1:8765 by default (or a
unix socket with --unix) and expects nginx (or another proxy) to terminate
TLS and forward the WebSocket upgrade.

Protocol (one WebSocket per player):
  client -> server
    text   {"t":"start","road":N,"sub":S,"ago":MS,"player":P}
                                           a run on road N began MS ms ago (0
                                           normally, more when resent after a
                                           reconnect, followed by its frames so
                                           far); S is echoed back; P is an
                                           anonymous random id kept by the
                                           browser: runs with the same P are
                                           never sent back to it
    binary 0x01 + k * 12-byte frames       frames of the current run
    text   {"t":"end","ticks":T,"finish":F,"sig":G}
                                           the current run ended: T = run
                                           clock (36 ticks/s), F = run clock
                                           at the finish line (0 = not
                                           finished), G = HMAC-SHA256 (hex) of
                                           "nonce|road|T|F" with the shared
                                           key (relay.key)
    text   {"t":"hello","player":P,"name":N}
                                           sent on connecting: your anonymous
                                           id and (optional) chosen name
    text   {"t":"name","name":N}           change your name ("" = random one)
    text   {"t":"completed","road":N}      you finished road N (the run you
                                           started last)
    text   {"t":"stats"}                   ask for the statistics
    text   {"t":"passkey-register-begin"} / {"t":"passkey-login-begin"}
                                           start creating a passkey (a new
                                           account, or another passkey for
                                           yours) / signing in with one
    text   {"t":"passkey-register-finish", id, clientDataJSON,
            attestationObject}             the browser's answer (base64url)
    text   {"t":"passkey-login-finish", id, clientDataJSON,
            authenticatorData, signature}  the browser's answer (base64url)
    text   {"t":"signout"}                 unlink this browser from its account
    text   {"t":"sync","roads":[30],"controls":C,"sound":S,"initial":B}
                                           this browser's settings and road
                                           completions (signed in only);
                                           initial: right after signing in
  server -> client
    text   {"t":"nonce","n":N,"sub":S}     the number to sign this run's end with
    text   {"t":"account","signed_in":B,"passkeys":N}
                                           account state, on hello and when
                                           it changes
    text   {"t":"passkey-options","action":A,"options":O}
                                           WebAuthn options for the browser
    text   {"t":"passkey-result","action":A,"ok":B,"error":E}
    text   {"t":"sync","roads":[30],"controls":C,"sound":S}
                                           the account's merged settings and
                                           road completions, to apply
    text   {"t":"record","road":R,"seconds":S,"overall":O,"personal":P,
            "previous_overall":[seconds, name] or null,
            "previous_personal":seconds or null}
                                           your finish just set the road's
                                           all-time record (O) and/or beat
                                           your personal best (P; not on
                                           your first finish of a road)
    text   {"t":"stats","all":A,"you":Y}   statistics: A = {players, attempts,
                                           completions, seconds, roads:
                                           [[attempts, completions, seconds]
                                           x 30], best: [[seconds, name, at] or
                                           null x 30] (fastest finish)}, Y the
                                           same for you (without players;
                                           best: [[seconds, at] or null x 30]);
                                           at = Unix time the time was set
    text   {"t":"online","count":N}        players connected to the relay (one
                                           per browser), sent on hello and
                                           when it changes
    text   {"t":"you","name":N,"error":E}  your name now; E ("invalid" or
                                           "taken") if a requested name was
                                           refused
    text   {"t":"run","id":I,"sub":S,"color":C}
                                           run I (another player) follows; C
                                           (0..COLORS-1) is the ghost's tint:
                                           stable per player where possible,
                                           distinct among the ghosts you get
    binary u32 I + k * 12-byte frames      frames of run I
    text   {"t":"end","id":I}              run I ended
    text   {"t":"gone","id":I}             run I's player left the road: stop
                                           showing it
    text   {"t":"players","list":[{"name":N,"color":C,"you":B}, ...]}
                                           who is on your road right now (you
                                           first, colour null), sent when it
                                           changes; an empty list once you
                                           have left the road
Statistics only count a finished road when the run checks out: its frames
start at the start line, the clock never runs backwards, the ship never moves
faster than the game's top speed, it reaches the end of the road (half a row
before it, the game's finish rule) where the game says, and the end message
carries a valid signature from the game.  Finish and playing times come from
the frames, not from the page.  A determined player can still fake a run;
this only stops casual cheating.

Accounts: a player can create an account with a passkey (WebAuthn,
discoverable credential, verified with python3-cryptography) and sign in
with it in other browsers.  Signing in links the browser's anonymous id to
the account: its statistics, personal bests and records move to the account
(added together, fastest kept), and the account keeps the player's name,
settings and road completions (highest count per road) in sync across the
browsers signed in to it.

Every player gets a name of two harmless words ("plucky-otter") derived from
their anonymous id, so it is stable across reloads.  Players can choose their
own name instead: 3-20 letters, digits and single dashes, starting and ending
with a letter or digit, not already used by another player online (ignoring
case).
  frame = u16 t, u16 x, u16 y, i16 sprite, u32 z  (little endian; t = ticks
          since the start of the run, z = road position in 16.16 rows)
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import secrets
import json
import logging
import os
import re
import sqlite3
import struct
import time
from urllib.parse import urlsplit

try:                                # passkeys need python3-cryptography
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
    PASSKEYS = True
except ImportError:
    PASSKEYS = False

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FRAME = 12
WINDOW = 300            # seconds: runs started this long before you are shown
KEEP = WINDOW + 60      # seconds a run is kept after it started
MAX_FRAMES = 60000      # per run (~25 min of play)
MAX_RUNS_SENT = 50      # remote runs (one per other player) per player
MAX_MESSAGE = 65536     # bytes per incoming WebSocket message
SEND_TIMEOUT = 5        # seconds before a slow watcher is dropped
ROADS = 64
COLORS = 8              # ghost tints, see port/ghost.c
GRACE = 15              # seconds a player stays listed on a road after a run
                        # (the game restarts the road after a crash)

# Names are ADJECTIVE-NOUN, picked from the player's anonymous id.  Both lists
# are deliberately bland: no word here is offensive alone or in any pairing.
ADJECTIVES = [
    "amber", "brave", "breezy", "bright", "bubbly", "calm", "cheerful",
    "clever", "cosmic", "cozy", "crisp", "curious", "dapper", "daring",
    "dizzy", "eager", "fancy", "fearless", "fluffy", "frosty", "gentle",
    "giddy", "glowing", "golden", "happy", "hasty", "jolly", "jumpy", "keen",
    "lively", "lucky", "mellow", "merry", "mighty", "misty", "nimble", "peppy",
    "plucky", "polite", "proud", "quick", "quiet", "rapid", "rosy", "rusty",
    "shiny", "silly", "sleepy", "snappy", "sparkly", "speedy", "spry",
    "steady", "sunny", "swift", "tidy", "tiny", "witty", "zany", "zesty",
    "bold", "chirpy", "dreamy", "frisky", "gleeful", "humble",
]
NOUNS = [
    "otter", "falcon", "comet", "panda", "badger", "bison", "bunny", "camel",
    "cheetah", "condor", "coyote", "dingo", "dolphin", "eagle", "ferret",
    "finch", "gecko", "gopher", "heron", "hippo", "ibis", "jaguar", "koala",
    "lemur", "llama", "lynx", "marmot", "meerkat", "moose", "narwhal", "newt",
    "ocelot", "orca", "osprey", "panther", "parrot", "pelican", "penguin",
    "puffin", "quokka", "rabbit", "raccoon", "raven", "robin", "salmon",
    "seal", "sparrow", "squid", "starling", "stoat", "swan", "tapir", "tiger",
    "toucan", "turtle", "walrus", "wombat", "yak", "zebra", "rocket", "nebula",
    "pulsar", "quasar", "meteor", "planet", "galaxy",
]

log = logging.getLogger("relay")


class Run:
    _next = 1

    def __init__(self, owner, road, ago=0.0):
        self.id = Run._next
        Run._next += 1
        self.owner = owner
        self.key = owner.key            # the player, across connections
        self.road = road
        self.started = time.monotonic() - ago
        self.frames = bytearray()
        self.ended = False
        self.watchers = set()       # players this run was announced to


runs = {}                           # id -> Run
players = set()                     # connected Players


# chosen names: 3-20 characters, letters/digits in groups joined by single
# dashes (so no dash at either end, no double dashes)
NAME_RE = re.compile(r"(?=.{3,20}\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z")


def valid_name(name):
    return isinstance(name, str) and NAME_RE.match(name) is not None


def name_taken(name, me):
    low = name.lower()
    return any(p.key != me.key and p.name.lower() == low for p in players)


def name_for(key):
    h = hashlib.sha1(key.encode()).digest()
    return "%s-%s" % (ADJECTIVES[int.from_bytes(h[0:2], "big") % len(ADJECTIVES)],
                      NOUNS[int.from_bytes(h[2:4], "big") % len(NOUNS)])


def own_color(key):
    return hashlib.sha1(b"color:" + key.encode()).digest()[0] % COLORS


def roster(road, now):
    """key -> name of the players on `road` (one entry per player)."""
    return {p.key: p.name for p in players if p.on_road(road, now)}


# ---------------------------------------------------------------- passkeys
# Pages allowed to use passkeys with this relay (--origin); the passkeys are
# bound to the host name (the WebAuthn "relying party").
ORIGINS = ["https://skyroads.smittn.com", "http://localhost:8000"]
CHALLENGE_TTL = 300                 # seconds to answer a passkey prompt


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text):
    if not isinstance(text, str) or len(text) > 8192:
        raise ValueError("bad base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def cbor_item(data, i):
    """Decode one CBOR item at data[i:]; returns (value, next index).  Just
    what WebAuthn uses (no indefinite lengths, no floats)."""
    head = data[i]
    major, info = head >> 5, head & 31
    i += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        arg = int.from_bytes(data[i:i + size], "big")
        i += size
    else:
        raise ValueError("unsupported CBOR")
    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i
    if major in (2, 3):
        raw = bytes(data[i:i + arg])
        if len(raw) != arg:
            raise ValueError("short CBOR")
        return (raw if major == 2 else raw.decode()), i + arg
    if major == 4:
        out = []
        for _ in range(arg):
            value, i = cbor_item(data, i)
            out.append(value)
        return out, i
    if major == 5:
        out = {}
        for _ in range(arg):
            key, i = cbor_item(data, i)
            out[key], i = cbor_item(data, i)
        return out, i
    if major == 6:                  # tag: ignore it
        return cbor_item(data, i)
    if arg in (20, 21, 22):
        return {20: False, 21: True, 22: None}[arg], i
    raise ValueError("unsupported CBOR")


def parse_auth_data(ad):
    """WebAuthn authenticator data: (rpIdHash, flags, signCount,
    (credential id, COSE public key bytes) or None)."""
    if len(ad) < 37:
        raise ValueError("short authenticator data")
    flags = ad[32]
    count = int.from_bytes(ad[33:37], "big")
    cred = None
    if flags & 0x40:                # attested credential data
        n = int.from_bytes(ad[53:55], "big")
        cred_id = bytes(ad[55:55 + n])
        _, end = cbor_item(ad, 55 + n)
        cred = (cred_id, bytes(ad[55 + n:end]))
    return bytes(ad[:32]), flags, count, cred


def cose_verify(cose_bytes, signature, data):
    """Check a WebAuthn assertion signature with a COSE public key."""
    key, _ = cbor_item(cose_bytes, 0)
    kty, alg = key.get(1), key.get(3)
    if kty == 2 and alg == -7 and key.get(-1) == 1:         # ES256
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(key[-2], "big"),
                                            int.from_bytes(key[-3], "big"),
                                            ec.SECP256R1()).public_key()
        pub.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    elif kty == 3 and alg == -257:                          # RS256
        pub = rsa.RSAPublicNumbers(int.from_bytes(key[-2], "big"),
                                   int.from_bytes(key[-1], "big")).public_key()
        pub.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    elif kty == 1 and alg == -8 and key.get(-1) == 6:       # EdDSA (Ed25519)
        ed25519.Ed25519PublicKey.from_public_bytes(key[-2]).verify(signature, data)
    else:
        raise ValueError("unsupported key type")


def check_client_data(raw, kind, challenge, origin):
    cd = json.loads(raw)
    if cd.get("type") != kind:
        raise ValueError("wrong type")
    if unb64u(cd.get("challenge", "")) != challenge:
        raise ValueError("wrong challenge")
    if cd.get("origin") != origin:
        raise ValueError("wrong origin")


class Stats:
    """All-time statistics in SQLite: every player (anonymous browser id) ever
    seen, and per player and road how many runs were started and finished."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                id         TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen  INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS road_stats (
                player      TEXT    NOT NULL,
                road        INTEGER NOT NULL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                completions INTEGER NOT NULL DEFAULT 0,
                seconds     REAL    NOT NULL DEFAULT 0,
                best        REAL,
                best_at     INTEGER,
                PRIMARY KEY (player, road));
            CREATE TABLE IF NOT EXISTS best_times (
                road    INTEGER PRIMARY KEY,
                seconds REAL    NOT NULL,
                player  TEXT    NOT NULL,
                name    TEXT    NOT NULL,
                at      INTEGER NOT NULL);
            -- accounts (passkeys) and the browsers signed in to them
            CREATE TABLE IF NOT EXISTS accounts (
                id       TEXT PRIMARY KEY,
                created  INTEGER NOT NULL,
                name     TEXT,
                controls TEXT,
                sound    TEXT,
                roads    TEXT);
            CREATE TABLE IF NOT EXISTS credentials (
                id         TEXT PRIMARY KEY,
                account    TEXT    NOT NULL,
                public_key BLOB    NOT NULL,
                sign_count INTEGER NOT NULL,
                created    INTEGER NOT NULL,
                last_used  INTEGER);
            CREATE TABLE IF NOT EXISTS links (
                player  TEXT PRIMARY KEY,
                account TEXT NOT NULL);
            -- every road record ever set, the current one included
            CREATE TABLE IF NOT EXISTS record_history (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                road    INTEGER NOT NULL,
                seconds REAL    NOT NULL,
                player  TEXT    NOT NULL,
                name    TEXT    NOT NULL,
                at      INTEGER NOT NULL);
        """)
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(road_stats)")]
        if "best" not in columns:           # databases from before personal bests
            self.db.execute("ALTER TABLE road_stats ADD COLUMN best REAL")
        if "best_at" not in columns:
            self.db.execute("ALTER TABLE road_stats ADD COLUMN best_at INTEGER")
        if not self.db.execute("SELECT 1 FROM record_history LIMIT 1").fetchone():
            # records set before the history was kept
            self.db.execute("INSERT INTO record_history (road, seconds, player, name, at) "
                            "SELECT road, seconds, player, name, at FROM best_times ORDER BY at")
        self.db.commit()

    def seen(self, player):
        now = int(time.time())
        self.db.execute("INSERT INTO players (id, first_seen, last_seen) VALUES (?, ?, ?) "
                        "ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen",
                        (player, now, now))
        self.db.commit()

    def count(self, player, road, column):
        self.db.execute("INSERT INTO road_stats (player, road, %s) VALUES (?, ?, 1) "
                        "ON CONFLICT (player, road) DO UPDATE SET %s = %s + 1"
                        % (column, column, column), (player, road))
        self.db.commit()

    def played(self, player, road, seconds):
        self.db.execute("INSERT INTO road_stats (player, road, seconds) VALUES (?, ?, ?) "
                        "ON CONFLICT (player, road) DO UPDATE SET seconds = seconds + ?",
                        (player, road, seconds, seconds))
        self.db.commit()

    def finished(self, road, seconds, player, name):
        """Record a finish time: the player's best on the road, and the
        road's record if it is the fastest.  Returns a "record" message for
        the player if either was beaten, else None."""
        now = int(time.time())
        mine = self.db.execute("SELECT best FROM road_stats WHERE player = ? AND road = ?",
                               (player, road)).fetchone()
        mine = mine[0] if mine else None
        personal = mine is None or seconds < mine
        if personal:
            self.db.execute("UPDATE road_stats SET best = ?, best_at = ? WHERE player = ? "
                            "AND road = ?", (seconds, now, player, road))
        current = self.db.execute("SELECT seconds, name FROM best_times WHERE road = ?",
                                  (road,)).fetchone()
        overall = current is None or seconds < current[0]
        if overall:
            self.db.execute("INSERT INTO best_times (road, seconds, player, name, at) "
                            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (road) DO UPDATE SET "
                            "seconds = excluded.seconds, player = excluded.player, "
                            "name = excluded.name, at = excluded.at",
                            (road, seconds, player, name, now))
            self.db.execute("INSERT INTO record_history (road, seconds, player, name, at) "
                            "VALUES (?, ?, ?, ?, ?)", (road, seconds, player, name, now))
        self.db.commit()
        beaten = personal and mine is not None      # a first finish is no news
        if not (beaten or overall):
            return None
        return {"t": "record", "road": road, "seconds": round(seconds, 2),
                "overall": overall, "personal": beaten,
                "previous_overall": [round(current[0], 2), current[1]] if current else None,
                "previous_personal": round(mine, 2) if mine is not None else None}

    # -- accounts --
    def account_for(self, player):
        row = self.db.execute("SELECT account FROM links WHERE player = ?", (player,)).fetchone()
        return row[0] if row else None

    def new_account(self, account):
        self.db.execute("INSERT INTO accounts (id, created) VALUES (?, ?)",
                        (account, int(time.time())))
        self.db.commit()

    def add_credential(self, cred_id, account, public_key, count):
        self.db.execute("INSERT INTO credentials (id, account, public_key, sign_count, created) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (cred_id, account, public_key, count, int(time.time())))
        self.db.commit()

    def credential(self, cred_id):
        return self.db.execute("SELECT account, public_key, sign_count FROM credentials "
                               "WHERE id = ?", (cred_id,)).fetchone()

    def credentials_of(self, account):
        return [r[0] for r in self.db.execute(
            "SELECT id FROM credentials WHERE account = ?", (account,))]

    def used_credential(self, cred_id, count):
        self.db.execute("UPDATE credentials SET sign_count = ?, last_used = ? WHERE id = ?",
                        (count, int(time.time()), cred_id))
        self.db.commit()

    def link(self, player, account):
        """Sign a browser in: link its anonymous id to the account and move
        its statistics there."""
        self.db.execute("INSERT INTO links (player, account) VALUES (?, ?) "
                        "ON CONFLICT (player) DO UPDATE SET account = excluded.account",
                        (player, account))
        self.merge(player, account_key(account))

    def unlink(self, player):
        self.db.execute("DELETE FROM links WHERE player = ?", (player,))
        self.db.commit()

    def merge(self, src, dst):
        """Move player `src`'s statistics to `dst`: counts and time added,
        the faster best kept, records and their history rewritten."""
        self.db.execute(
            "INSERT INTO road_stats (player, road, attempts, completions, seconds, best, best_at) "
            "SELECT ?, road, attempts, completions, seconds, best, best_at FROM road_stats "
            "WHERE player = ? AND true ON CONFLICT (player, road) DO UPDATE SET "
            "attempts = attempts + excluded.attempts, "
            "completions = completions + excluded.completions, "
            "seconds = seconds + excluded.seconds, "
            "best_at = CASE WHEN excluded.best IS NOT NULL AND (best IS NULL OR excluded.best < best) "
            "          THEN excluded.best_at ELSE best_at END, "
            "best = CASE WHEN excluded.best IS NOT NULL AND (best IS NULL OR excluded.best < best) "
            "       THEN excluded.best ELSE best END", (dst, src))
        self.db.execute("DELETE FROM road_stats WHERE player = ?", (src,))
        self.db.execute("UPDATE best_times SET player = ? WHERE player = ?", (dst, src))
        self.db.execute("UPDATE record_history SET player = ? WHERE player = ?", (dst, src))
        self.db.execute(
            "INSERT INTO players (id, first_seen, last_seen) "
            "SELECT ?, first_seen, last_seen FROM players WHERE id = ? AND true "
            "ON CONFLICT (id) DO UPDATE SET first_seen = min(first_seen, excluded.first_seen), "
            "last_seen = max(last_seen, excluded.last_seen)", (dst, src))
        self.db.execute("DELETE FROM players WHERE id = ?", (src,))
        self.db.commit()

    def account_data(self, account):
        row = self.db.execute("SELECT name, controls, sound, roads FROM accounts WHERE id = ?",
                              (account,)).fetchone()
        if not row:
            return None
        roads = json.loads(row[3]) if row[3] else []
        return {"name": row[0], "controls": row[1], "sound": row[2],
                "roads": [int(roads[i]) if i < len(roads) else 0 for i in range(ROADS_IN_GAME)]}

    def save_account(self, account, **values):
        for column in ("name", "controls", "sound", "roads"):
            if column in values:
                value = values[column]
                if column == "roads":
                    value = json.dumps(value)
                self.db.execute("UPDATE accounts SET %s = ? WHERE id = ?" % column, (value, account))
        self.db.commit()

    def renamed(self, player, name):
        self.db.execute("UPDATE best_times SET name = ? WHERE player = ?", (name, player))
        self.db.commit()

    def summary(self, player):
        def block(rows):
            roads = [[0, 0, 0] for _ in range(ROADS_IN_GAME)]
            for road, a, c, s in rows:
                if 0 <= road < ROADS_IN_GAME:
                    roads[road] = [a, c, round(s)]
            return {"attempts": sum(r[0] for r in roads),
                    "completions": sum(r[1] for r in roads),
                    "seconds": sum(r[2] for r in roads), "roads": roads}
        everyone = block(self.db.execute(
            "SELECT road, SUM(attempts), SUM(completions), SUM(seconds) FROM road_stats "
            "GROUP BY road"))
        everyone["players"] = self.db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        best = [None] * ROADS_IN_GAME
        for road, seconds, name, at in self.db.execute(
                "SELECT road, seconds, name, at FROM best_times"):
            if 0 <= road < ROADS_IN_GAME:
                best[road] = [round(seconds, 2), name, at]
        everyone["best"] = best
        you = block(self.db.execute(
            "SELECT road, attempts, completions, seconds FROM road_stats WHERE player = ?",
            (player or "",)))
        mine = [None] * ROADS_IN_GAME
        for road, seconds, at in self.db.execute(
                "SELECT road, best, best_at FROM road_stats WHERE player = ? AND best IS NOT NULL",
                (player or "",)):
            if 0 <= road < ROADS_IN_GAME:
                mine[road] = [round(seconds, 2), at]
        you["best"] = mine
        return {"t": "stats", "all": everyone, "you": you}


stats = None                        # Stats, unless --db ""


def account_key(account):
    """A signed-in player's identity (key and statistics id)."""
    return "acct" + account
ROADS_IN_GAME = 30
RELAY_KEY = None                    # bytes from relay.key: end messages must be signed

# Road lengths in rows (ROADS.LZS, as loaded by 1000:5614): a road is finished
# once the ship is within half a row of its end.
ROAD_LENGTHS = [55, 115, 60, 94, 126, 48, 60, 106, 70, 81,
                199, 164, 112, 237, 201, 137, 153, 163, 147, 186,
                130, 160, 224, 83, 150, 191, 166, 234, 136, 200]
TICKS = 36                          # game clock ticks per second
TOP_SPEED = 0x2AAA                  # rows (16.16) per tick, the game's cap
START_Z = 3 << 16                   # every run starts 3 rows in


def check_run(run, ticks, finish, nonce, sig):
    """Check a run's frames (and the game's signature of its end).
    Returns (play seconds, finish seconds or None, problem or None)."""
    data = run.frames
    n = len(data) // FRAME
    if n == 0:
        return 0.0, None, "no frames"
    last_t = struct.unpack_from("<H", data, (n - 1) * FRAME)[0]
    play = last_t / TICKS
    if not finish:
        return play, None, None
    if RELAY_KEY is not None:
        msg = ("%d|%d|%d|%d" % (nonce or 0, run.road, ticks, finish)).encode()
        good = hmac.new(RELAY_KEY, msg, hashlib.sha256).hexdigest()
        if not (isinstance(sig, str) and hmac.compare_digest(good, sig)):
            return play, None, "bad signature"
    if n < TICKS:
        return play, None, "too short"
    line = ROAD_LENGTHS[run.road] * 65536 - 0x8000
    prev_t = prev_z = finish_t = None
    for i in range(n):
        t, _x, _y, _spr, z = struct.unpack_from("<HHHhI", data, i * FRAME)
        if i == 0:
            if t > TICKS or abs(z - START_Z) > 0x2000:
                return play, None, "does not start at the start"
        else:
            if t < prev_t:
                return play, None, "clock went backwards"
            if finish_t is None and z - prev_z > (t - prev_t) * TOP_SPEED + 0x400:
                return play, None, "faster than the top speed"
        if finish_t is None and z >= line:
            finish_t = t            # (the finish animation after it is not checked)
        prev_t, prev_z = t, z
    if finish_t is None:
        return play, None, "never reached the end of the road"
    if abs(finish_t - finish) > TICKS or abs(last_t - ticks) > TICKS:
        return play, None, "does not match the game's clock"
    return play, finish_t / TICKS, None


online_count = 0


async def broadcast_online(also=None):
    """Tell everyone how many players are connected when that changes (and
    `also`, a player who just said hello, in any case)."""
    global online_count
    n = len({p.key for p in players if p.greeted})  # one per browser, not tab
    msg = json.dumps({"t": "online", "count": n}).encode()
    if n != online_count:
        online_count = n
        for p in list(players):
            await p.notify(0x1, msg)
    elif also is not None:
        await also.notify(0x1, msg)


async def broadcast(road):
    """Send the current list of `road` to everyone on it (and an empty one to
    whoever just left it)."""
    for p in list(players):
        if p.road == road or p.listed == road:
            await p.prune_ghosts()
            await p.send_roster()


class Closed(Exception):
    pass


class Player:
    def __init__(self, reader, writer, origin=None):
        self.reader = reader
        self.writer = writer
        self.run = None
        self.sending = asyncio.Lock()
        self.anon = os.urandom(16).hex()    # until the page sends its id
        self.pid = None
        self.custom = None                  # chosen name, else name_for(key)
        self.account = None                 # signed-in account id
        self.origin = origin                # page origin (for passkeys)
        self.challenge = None               # pending passkey prompt
        self.greeted = False                # sent hello (counted as online)
        self.finishable = False             # last run may still be completed
        self.finish = None                  # (road, seconds) of the last run
        self.nonce = None                   # to check the end's signature
        self.road = None                    # road of the current/last run
        self.racing = False
        self.ended_at = float("-inf")
        self.listed = None                  # road whose list it last got
        self.colors = {}                    # key -> colour, for this viewer
        self.seen = {}                      # key -> order first seen
        self.ghosts = {}                    # key -> Run shown as a ghost

    @property
    def ghost_keys(self):
        return set(self.ghosts)

    def drop_ghosts(self):
        for r in self.ghosts.values():
            r.watchers.discard(self)
        self.ghosts = {}

    async def prune_ghosts(self):
        """Remove the ghosts of players who are no longer on the road."""
        listed = roster(self.road, time.monotonic())
        for key, r in list(self.ghosts.items()):
            if key not in listed:
                del self.ghosts[key]
                r.watchers.discard(self)
                await self.notify(0x1, json.dumps({"t": "gone", "id": r.id}).encode())

    @property
    def key(self):
        return account_key(self.account) if self.account else (self.pid or self.anon)

    @property
    def ident(self):
        """Statistics id: the account when signed in, else the browser."""
        return account_key(self.account) if self.account else self.pid

    @property
    def name(self):
        return self.custom or name_for(self.key)

    def on_road(self, road, now):
        return self.road == road and (self.racing or now - self.ended_at < GRACE)

    def assign(self, keys, pinned=()):
        """Colours for `keys`, all different (up to COLORS players): a
        player keeps the colour you saw them in before, else gets their own,
        else the next free one.  `pinned` keys never change (their ghosts are
        already in the game)."""
        used = set()
        order = sorted(pinned) + sorted(set(keys) - set(pinned),
                                        key=lambda k: self.seen.get(k, float("inf")))
        for k in order:
            self.seen.setdefault(k, len(self.seen))
            c = self.colors.get(k)
            if c is None or (c in used and k not in pinned):
                c = own_color(k)
                for i in range(COLORS):
                    if (c + i) % COLORS not in used:
                        c = (c + i) % COLORS
                        break
            self.colors[k] = c
            used.add(c)
        return {k: self.colors[k] for k in keys}

    async def set_name(self, name, quiet_errors=False):
        """Choose a name ("" = back to the random one) and tell the player
        what their name is now."""
        error = None
        if not isinstance(name, str):
            error = "invalid"
        elif name == "":
            self.custom = None
        elif not valid_name(name):
            error = "invalid"
        elif name_taken(name, self):
            error = "taken"
        else:
            self.custom = name
        if not error and stats and self.ident:
            stats.renamed(self.ident, self.name)  # fastest times show the new name
        if not error and stats and self.account:
            stats.save_account(self.account, name=self.custom)
            for p in list(players):             # the account's other browsers
                if p is not self and p.account == self.account:
                    p.custom = self.custom
                    await p.notify(0x1, json.dumps({"t": "you", "name": p.name,
                                                    "custom": bool(p.custom)}).encode())
        reply = {"t": "you", "name": self.name, "custom": bool(self.custom)}
        if error and not quiet_errors:
            reply["error"] = error
        await self.send_text(reply)
        if not error and self.road is not None:
            await broadcast(self.road)

    # -- accounts --
    async def send_account(self):
        n = len(stats.credentials_of(self.account)) if (stats and self.account) else 0
        await self.send_text({"t": "account", "signed_in": bool(self.account), "passkeys": n,
                              "available": bool(PASSKEYS and stats and self.rp_id())})

    def rp_id(self):
        """The passkeys' host name, if this page may use them."""
        if self.origin in ORIGINS:
            return urlsplit(self.origin).hostname
        return None

    async def signed_in(self, account):
        """Link this browser to `account` (moving its statistics) and adopt
        the account's name, or give it this browser's."""
        stats.link(self.pid, account)
        self.account = account
        data = stats.account_data(account) or {}
        name = data.get("name") or self.custom or ""
        await self.set_name(name, quiet_errors=True)   # the name first: the page
        await self.send_account()                       # shows it with the state
        await broadcast_online()
        if self.road is not None:
            await broadcast(self.road)

    async def passkey_begin(self, action):
        rp = self.rp_id()
        if not (PASSKEYS and stats and rp and self.pid):
            await self.send_text({"t": "passkey-result", "action": action, "ok": False,
                                  "error": "Passkeys are not available here."})
            return
        challenge = secrets.token_bytes(32)
        if action == "register":
            account = self.account or secrets.token_hex(16)
            options = {
                "challenge": b64u(challenge),
                "rp": {"id": rp, "name": "SkyRoads"},
                "user": {"id": b64u(bytes.fromhex(account)), "name": self.name,
                         "displayName": self.name},
                "pubKeyCredParams": [{"type": "public-key", "alg": -7},
                                     {"type": "public-key", "alg": -8},
                                     {"type": "public-key", "alg": -257}],
                "excludeCredentials": [{"type": "public-key", "id": c}
                                       for c in (stats.credentials_of(self.account)
                                                 if self.account else [])],
            }
        else:
            account = None
            options = {"challenge": b64u(challenge), "rpId": rp}
        self.challenge = (action, challenge, time.monotonic() + CHALLENGE_TTL, account)
        await self.send_text({"t": "passkey-options", "action": action, "options": options})

    async def passkey_finish(self, action, msg):
        pending, self.challenge = self.challenge, None
        try:
            if not (pending and pending[0] == action and time.monotonic() < pending[2]):
                raise ValueError("no passkey prompt is open")
            _, challenge, _, account = pending
            rp = self.rp_id()
            client_data = unb64u(msg.get("clientDataJSON"))
            if action == "register":
                check_client_data(client_data, "webauthn.create", challenge, self.origin)
                att, _ = cbor_item(unb64u(msg.get("attestationObject")), 0)
                rp_hash, flags, count, cred = parse_auth_data(att["authData"])
                if rp_hash != hashlib.sha256(rp.encode()).digest() or not flags & 1 or not cred:
                    raise ValueError("bad authenticator data")
                cred_id, public_key = cred    # attestation "none": trusted on first use
                if stats.credential(b64u(cred_id)):
                    raise ValueError("passkey already registered")
                if not self.account:
                    stats.new_account(account)
                stats.add_credential(b64u(cred_id), account, public_key, count)
                if self.account != account:
                    await self.signed_in(account)
                else:
                    await self.send_account()
            else:
                check_client_data(client_data, "webauthn.get", challenge, self.origin)
                cred_id = msg.get("id")
                row = stats.credential(cred_id) if isinstance(cred_id, str) else None
                if not row:
                    raise ValueError("unknown passkey")
                account, public_key, last_count = row
                auth_data = unb64u(msg.get("authenticatorData"))
                rp_hash, flags, count, _ = parse_auth_data(auth_data)
                if rp_hash != hashlib.sha256(rp.encode()).digest() or not flags & 1:
                    raise ValueError("bad authenticator data")
                try:
                    cose_verify(public_key, unb64u(msg.get("signature")),
                                auth_data + hashlib.sha256(client_data).digest())
                except InvalidSignature:
                    raise ValueError("bad signature")
                if count and last_count and count <= last_count:
                    raise ValueError("passkey counter went backwards (cloned?)")
                stats.used_credential(cred_id, count)
                if self.account != account:
                    await self.signed_in(account)
            await self.send_text({"t": "passkey-result", "action": action, "ok": True})
        except (ValueError, KeyError, TypeError, IndexError) as e:
            log.info("passkey %s refused: %s", action, e)
            await self.send_text({"t": "passkey-result", "action": action, "ok": False,
                                  "error": "The passkey could not be used."})

    async def signout(self):
        if stats and self.account and self.pid:
            stats.unlink(self.pid)
        self.account = None
        await self.send_account()

    async def sync(self, msg):
        """Merge this browser's settings and road completions into the
        account and send the result to every browser signed in to it."""
        if not (stats and self.account):
            return
        data = stats.account_data(self.account)
        roads = msg.get("roads")
        if isinstance(roads, list) and len(roads) == ROADS_IN_GAME:
            data["roads"] = [max(a, min(65535, int(b)) if isinstance(b, int) and b > 0 else 0)
                             for a, b in zip(data["roads"], roads)]
        initial = bool(msg.get("initial"))
        for field, allowed in (("controls", ("keyboard", "joystick", "mouse")),
                               ("sound", ("on", "off"))):
            value = msg.get(field)
            # signing in: the account's settings win; afterwards a change wins
            if value in allowed and (not initial or not data[field]):
                data[field] = value
        stats.save_account(self.account, controls=data["controls"], sound=data["sound"],
                           roads=data["roads"])
        out = json.dumps({"t": "sync", "roads": data["roads"], "controls": data["controls"],
                          "sound": data["sound"]}).encode()
        for p in list(players):
            if p.account == self.account:
                await p.notify(0x1, out)

    async def send_roster(self):
        now = time.monotonic()
        if not self.on_road(self.road, now):
            if self.listed is not None:
                self.listed = None
                await self.notify(0x1, b'{"t":"players","list":[]}')
            return
        others = roster(self.road, now)
        others.pop(self.key, None)
        colors = self.assign(set(others) | self.ghost_keys, pinned=self.ghost_keys)
        entries = [{"name": self.name, "color": None, "you": True}]
        entries += [{"name": n, "color": colors[k]}
                    for k, n in sorted(others.items(), key=lambda kv: kv[1])]
        self.listed = self.road
        await self.notify(0x1, json.dumps({"t": "players", "list": entries},
                                          separators=(",", ":")).encode())

    # -- WebSocket framing (RFC 6455, server side) --
    async def send(self, opcode, payload):
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        async with self.sending:
            self.writer.write(head + payload)
            await self.writer.drain()

    async def send_text(self, obj):
        await self.send(0x1, json.dumps(obj, separators=(",", ":")).encode())

    async def notify(self, opcode, payload):
        """Send to a watcher; False if it is gone or too slow."""
        try:
            await asyncio.wait_for(self.send(opcode, payload), SEND_TIMEOUT)
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return False

    async def recv(self):
        """Next complete message as (opcode, bytes); handles ping/close."""
        parts, opcode = [], None
        while True:
            b0, b1 = await self.reader.readexactly(2)
            op, fin, n = b0 & 0x0F, b0 & 0x80, b1 & 0x7F
            if not b1 & 0x80:
                raise Closed("unmasked client frame")
            if n == 126:
                n = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if n > MAX_MESSAGE:
                raise Closed("message too large")
            mask = await self.reader.readexactly(4)
            data = bytearray(await self.reader.readexactly(n))
            for i in range(n):
                data[i] ^= mask[i & 3]
            if op == 0x8:
                raise Closed("close")
            if op == 0x9:
                await self.send(0xA, bytes(data))
                continue
            if op == 0xA:
                continue
            if op != 0x0:
                opcode, parts = op, []
            parts.append(bytes(data))
            if sum(map(len, parts)) > MAX_MESSAGE:
                raise Closed("message too large")
            if fin and opcode is not None:
                return opcode, b"".join(parts)

    # -- game protocol --
    async def start(self, road, sub, ago, pid):
        if pid:
            self.pid = pid
        await self.end(quietly=True)
        previous, self.road, self.racing = self.road, road, True
        self.run = Run(self, road, min(max(ago, 0.0), WINDOW))
        self.run_began = time.monotonic() - max(ago, 0.0)   # for the time check
        self.nonce = None
        if ago < 1.0:                   # not a run resent after a reconnect
            self.finishable = True      # one completion may be reported
            self.nonce = secrets.randbits(32)
            await self.send_text({"t": "nonce", "n": self.nonce, "sub": sub})
            if stats and self.ident and road < ROADS_IN_GAME:
                stats.count(self.ident, road, "attempts")
        runs[self.run.id] = self.run
        now = self.run.started
        listed = roster(road, time.monotonic())
        listed.pop(self.key, None)
        others = [r for r in runs.values()     # players on the road right now
                  if r.road == road and r.owner is not self and r.key in listed
                  and now - WINDOW <= r.started <= now]
        others.sort(key=lambda r: r.started, reverse=True)
        latest, seen = [], set()
        for r in others:                # only each player's most recent run
            if r.key not in seen:
                seen.add(r.key)
                latest.append(r)
        latest = latest[:MAX_RUNS_SENT]
        self.drop_ghosts()
        colors = self.assign({r.key for r in latest} | set(listed))
        self.ghosts = {r.key: r for r in latest}
        for r in latest:
            r.watchers.add(self)
            await self.send_text({"t": "run", "id": r.id, "sub": sub, "color": colors[r.key]})
            head = struct.pack("<I", r.id)
            chunk = (MAX_MESSAGE // FRAME) * FRAME
            for i in range(0, len(r.frames), chunk):
                await self.send(0x2, head + bytes(r.frames[i:i + chunk]))
            if r.ended:
                await self.send_text({"t": "end", "id": r.id})
        await broadcast(road)
        if previous is not None and previous != road:
            await broadcast(previous)

    async def frames(self, data):
        r = self.run
        if r is None or r.ended or len(data) % FRAME:
            return
        room = MAX_FRAMES * FRAME - len(r.frames)
        data = data[:max(0, room)]
        if not data:
            return
        r.frames += data
        msg = struct.pack("<I", r.id) + data
        for w in list(r.watchers):
            if not await w.notify(0x2, msg):
                r.watchers.discard(w)

    async def end(self, quietly=False):
        if self.racing:
            self.racing = False
            self.ended_at = time.monotonic()
            if not quietly:             # drop off the list unless racing again
                road = self.road
                asyncio.get_running_loop().call_later(
                    GRACE + 0.5, lambda: asyncio.ensure_future(broadcast(road)))
        r, self.run = self.run, None
        if r is None or r.ended:
            return
        r.ended = True
        msg = json.dumps({"t": "end", "id": r.id}).encode()
        for w in list(r.watchers):
            if not await w.notify(0x1, msg):
                r.watchers.discard(w)

    async def serve(self):
        while True:
            op, data = await self.recv()
            if op == 0x2 and data[:1] == b"\x01":
                await self.frames(data[1:])
            elif op == 0x1:
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("t") == "start":
                    road, sub, ago = msg.get("road"), msg.get("sub", 0), msg.get("ago", 0)
                    player = msg.get("player")
                    if not (isinstance(player, str) and 0 < len(player) <= 64
                            and player.isascii() and player.isalnum()):
                        player = None
                    if (isinstance(road, int) and 0 <= road < ROADS and isinstance(sub, int)
                            and isinstance(ago, (int, float))):
                        await self.start(road, sub, ago / 1000.0, player)
                elif msg.get("t") == "end":
                    r, self.finish = self.run, None
                    ticks, finish = msg.get("ticks"), msg.get("finish")
                    if (stats and self.ident and r and not r.ended and r.road < ROADS_IN_GAME
                            and isinstance(ticks, int) and isinstance(finish, int)):
                        play, finished, problem = check_run(r, ticks, finish, self.nonce,
                                                            msg.get("sig"))
                        # the frames' clock, but no more than the real time
                        # since the run began (and an hour at most)
                        real = time.monotonic() - self.run_began + 5
                        stats.played(self.ident, r.road, min(play, real, 3600.0))
                        if finished is not None and finished <= real:
                            self.finish = (r.road, finished)
                        elif finish:
                            log.info("finish on road %d not counted: %s", r.road,
                                     problem or "faster than real time")
                    await self.end()
                elif msg.get("t") == "hello":
                    player = msg.get("player")
                    if (isinstance(player, str) and 0 < len(player) <= 64
                            and player.isascii() and player.isalnum()):
                        self.pid = player
                        if stats:
                            self.account = stats.account_for(player)
                            if not self.account:
                                stats.seen(player)
                    self.greeted = True
                    name = msg.get("name") or ""
                    if self.account:            # the account's name wins
                        data = stats.account_data(self.account) or {}
                        name = data.get("name") or name
                        if name and not data.get("name") and valid_name(name):
                            stats.save_account(self.account, name=name)
                    await self.set_name(name, quiet_errors=True)
                    await self.send_account()
                    await broadcast_online(also=self)
                elif msg.get("t") == "name":
                    await self.set_name(msg.get("name") or "")
                elif msg.get("t") == "completed":
                    road = msg.get("road")
                    # only a finish the run's frames (and signature) confirm
                    if (stats and self.ident and self.finishable and road == self.road
                            and self.finish and self.finish[0] == road):
                        self.finishable = False
                        stats.count(self.ident, road, "completions")
                        record = stats.finished(road, self.finish[1], self.ident, self.name)
                        self.finish = None
                        if record:
                            await self.send_text(record)
                elif msg.get("t") in ("passkey-register-begin", "passkey-login-begin"):
                    await self.passkey_begin(msg["t"].split("-")[1])
                elif msg.get("t") in ("passkey-register-finish", "passkey-login-finish"):
                    await self.passkey_finish(msg["t"].split("-")[1], msg)
                elif msg.get("t") == "signout":
                    await self.signout()
                elif msg.get("t") == "sync":
                    await self.sync(msg)
                elif msg.get("t") == "stats":
                    await self.send_text(stats.summary(self.ident) if stats
                                         else {"t": "stats", "error": "unavailable"})


async def handshake(reader, writer):
    request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    lines = request.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    key = headers.get("sec-websocket-key")
    if "websocket" not in headers.get("upgrade", "").lower() or not key:
        writer.write(b"HTTP/1.1 426 Upgrade Required\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        return False
    accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
    writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
    await writer.drain()
    return headers.get("origin", "")


async def client(reader, writer):
    player = None
    try:
        origin = await handshake(reader, writer)
        if origin is False:
            return
        player = Player(reader, writer, origin)
        players.add(player)
        await player.serve()
    except (Closed, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        pass
    finally:
        if player:
            players.discard(player)
            try:
                await broadcast_online()
            except (ConnectionError, OSError):
                pass
            try:
                await player.end(quietly=True)
            except (ConnectionError, OSError):
                pass
            for r in runs.values():
                r.watchers.discard(player)
            if player.road is not None:     # gone: take it off the list now
                await broadcast(player.road)
        writer.close()


async def expire():
    while True:
        await asyncio.sleep(30)
        now = time.monotonic()
        for rid in [i for i, r in runs.items() if now - r.started > KEEP]:
            r = runs.pop(rid)
            if r.owner.run is r:        # still racing after KEEP: stop relaying it
                r.owner.run = None


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--unix", metavar="PATH", help="listen on a unix socket instead")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--db", default=os.path.join(here, "stats.db"),
                    help='SQLite file for the statistics ("" to disable)')
    ap.add_argument("--origin", action="append",
                    help="page origin allowed to use passkeys (repeatable; default: %s)"
                         % ", ".join(ORIGINS))
    ap.add_argument("--key-file", default=os.path.join(here, "relay.key"),
                    help="hex key the game signs run ends with (same as the "
                         "build's GHOST_KEY); without it signatures are not checked")
    args = ap.parse_args()
    if args.origin:
        ORIGINS[:] = args.origin
    global RELAY_KEY
    if args.key_file and os.path.exists(args.key_file):
        with open(args.key_file) as f:
            RELAY_KEY = bytes.fromhex(f.read().strip())
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    global stats
    if args.db:
        stats = Stats(args.db)
        log.info("statistics in %s (%s)", args.db,
                 "signed run ends" if RELAY_KEY else "run ends NOT signed: no key file")
    if args.unix:
        if os.path.exists(args.unix):
            os.unlink(args.unix)            # stale socket from a previous run
        server = await asyncio.start_unix_server(client, args.unix)
        os.chmod(args.unix, 0o666)          # nginx connects as another user
        log.info("skyroads ghost relay on %s", args.unix)
    else:
        server = await asyncio.start_server(client, args.host, args.port)
        log.info("skyroads ghost relay on %s:%d", args.host, args.port)
    asyncio.create_task(expire())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

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
    text   {"t":"end","time":S}            the current run ended after S
                                           seconds of play (game clock)
    text   {"t":"hello","player":P,"name":N}
                                           sent on connecting: your anonymous
                                           id and (optional) chosen name
    text   {"t":"name","name":N}           change your name ("" = random one)
    text   {"t":"completed","road":N}      you finished road N (the run you
                                           started last)
    text   {"t":"stats"}                   ask for the statistics
  server -> client
    text   {"t":"stats","all":A,"you":Y}   statistics: A = {players, attempts,
                                           completions, seconds, roads:
                                           [[attempts, completions, seconds]
                                           x 30]}, Y the same for you
                                           (without players)
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
import json
import logging
import os
import re
import sqlite3
import struct
import time

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
                PRIMARY KEY (player, road));
        """)
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
        you = block(self.db.execute(
            "SELECT road, attempts, completions, seconds FROM road_stats WHERE player = ?",
            (player or "",)))
        return {"t": "stats", "all": everyone, "you": you}


stats = None                        # Stats, unless --db ""
ROADS_IN_GAME = 30


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
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.run = None
        self.sending = asyncio.Lock()
        self.anon = os.urandom(16).hex()    # until the page sends its id
        self.pid = None
        self.custom = None                  # chosen name, else name_for(key)
        self.greeted = False                # sent hello (counted as online)
        self.finishable = False             # last run may still be completed
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
        return self.pid or self.anon

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
        reply = {"t": "you", "name": self.name}
        if error and not quiet_errors:
            reply["error"] = error
        await self.send_text(reply)
        if not error and self.road is not None:
            await broadcast(self.road)

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
        if ago < 1.0:                   # not a run resent after a reconnect
            self.finishable = True      # one completion may be reported
            if stats and self.pid and road < ROADS_IN_GAME:
                stats.count(self.pid, road, "attempts")
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
                    seconds = msg.get("time")
                    if (stats and self.pid and self.run and not self.run.ended
                            and self.run.road < ROADS_IN_GAME
                            and isinstance(seconds, (int, float)) and seconds > 0):
                        # game clock, but no more than the real time since the
                        # run began (and an hour at most)
                        real = time.monotonic() - self.run_began + 5
                        stats.played(self.pid, self.run.road, min(seconds, real, 3600.0))
                    await self.end()
                elif msg.get("t") == "hello":
                    player = msg.get("player")
                    if (isinstance(player, str) and 0 < len(player) <= 64
                            and player.isascii() and player.isalnum()):
                        self.pid = player
                        if stats:
                            stats.seen(player)
                    self.greeted = True
                    await self.set_name(msg.get("name") or "", quiet_errors=True)
                    await broadcast_online(also=self)
                elif msg.get("t") == "name":
                    await self.set_name(msg.get("name") or "")
                elif msg.get("t") == "completed":
                    road = msg.get("road")
                    if (stats and self.pid and self.finishable and road == self.road
                            and isinstance(road, int) and 0 <= road < ROADS_IN_GAME):
                        self.finishable = False
                        stats.count(self.pid, road, "completions")
                elif msg.get("t") == "stats":
                    await self.send_text(stats.summary(self.pid) if stats
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
    return True


async def client(reader, writer):
    player = None
    try:
        if not await handshake(reader, writer):
            return
        player = Player(reader, writer)
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
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.db"),
                    help='SQLite file for the statistics ("" to disable)')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    global stats
    if args.db:
        stats = Stats(args.db)
        log.info("statistics in %s", args.db)
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

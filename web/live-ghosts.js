/* Live ghosts: streams your run to the ghost relay (server/relay.py) and
 * feeds the runs of other players on the same road into the game
 * (port/ghost.c), which replays them at the same run time as yours.
 *
 * Enabled when a relay URL is configured: `?relay=wss://host/path` on the
 * page URL, or window.SKY_GHOST_RELAY (written to relay.js by the Makefile).
 * Without one the game shows your own saved runs instead.
 *
 * A random anonymous player id is kept in localStorage so the relay does not
 * show you your own runs after a reload or reconnect; the relay also turns it
 * into your two-word name.  The players on your road are listed over the top
 * left of the game, each with the colour of their ghost.
 *
 * Multiplayer can be switched off in the settings menu (remembered in
 * localStorage, on by default): single player has no connection and no
 * ghosts.  Switching takes effect at once, also mid-run: the run is followed
 * in single player too and sent to the relay when multiplayer comes back. */
(function () {
  var url = new URLSearchParams(location.search).get('relay') || window.SKY_GHOST_RELAY;
  if (!url || typeof Module === 'undefined') return;

  var player = playerId();
  var multiplayer = true;
  try { multiplayer = localStorage.getItem('skyroads.multiplayer') !== '0'; } catch (e) {}
  var ws = null, backoff = 1000, retryTimer = null;
  var sub = 0, road = -1, racing = false, runStart = 0;
  var live = {};            // relay run ids announced for the current run
  var pending = [];         // frames not sent yet: t, x, y, spr, z, ...
  var sent = [];            // packed frames of the current run (for resending)
  var MAX_KEEP = 60000;
  /* ghost tints, the same as TINTS in port/ghost.c */
  var COLORS = ['#50e6ff', '#ff69c8', '#ffeb50', '#6eff78', '#ff9b32', '#af82ff', '#ff5050', '#f5f5f5'];
  var listEl = document.getElementById('players');

  /* the players on your road: [{name, color, you}], you first */
  function showPlayers(list) {
    if (!listEl) return;
    listEl.textContent = '';
    (list || []).forEach(function (p) {
      var li = document.createElement('li'), dot = document.createElement('span');
      dot.className = 'dot';
      if (p.you) {
        li.className = 'you';
        dot.style.background = 'transparent';
        dot.style.boxShadow = 'inset 0 0 0 2px #dde';
      } else {
        dot.style.background = COLORS[(p.color | 0) % COLORS.length];
      }
      li.appendChild(dot);
      li.appendChild(document.createTextNode(String(p.name) + (p.you ? ' (you)' : '')));
      listEl.appendChild(li);
    });
    shownList = list && list.length ? list : null;
    updateListVisibility();
  }
  /* only while racing: not on the road selection screen or other menus */
  var shownList = null, hideTimer = null;
  function updateListVisibility() {
    if (listEl) listEl.classList.toggle('show', !!(shownList && racing));
    if (window.skyPlacePlayers) window.skyPlacePlayers();  // beside or over the game
  }

  function playerId() {
    var id = null;
    try { id = localStorage.getItem('skyroads.player'); } catch (e) {}
    if (!id || !/^[0-9a-f]{32}$/.test(id)) {
      var b = new Uint8Array(16);
      crypto.getRandomValues(b);
      id = Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join('');
      try { localStorage.setItem('skyroads.player', id); } catch (e) {}  // else: this page only
    }
    return id;
  }

  function connect() {
    if (!multiplayer || ws) return;
    try { ws = new WebSocket(url); } catch (e) { retry(); return; }
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () {
      backoff = 1000;
      ws.send(JSON.stringify({ t: 'hello', player: player, name: storedName() }));
      if (racing) sendStart();          // reconnected mid-run: resend it
    };
    ws.onclose = function () {
      ws = null;
      showPlayers([]);
      setOnline(null);
      if (multiplayer) retry();
    };
    ws.onerror = function () {};
    ws.onmessage = receive;
  }
  function retry() {
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 30000);
  }
  function disconnect() {
    clearTimeout(retryTimer);
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    showPlayers([]);
    setOnline(null);
  }
  function open() { return ws && ws.readyState === 1; }

  function sendStart() {
    live = {};
    ws.send(JSON.stringify({ t: 'start', road: road, sub: sub, ago: Date.now() - runStart,
                             player: player }));
    for (var i = 0; i < sent.length; i++) ws.send(sent[i]);
  }

  function pack(frames) {
    var n = frames.length / 5, buf = new ArrayBuffer(1 + n * 12), v = new DataView(buf);
    v.setUint8(0, 1);
    for (var i = 0; i < n; i++) {
      var o = 1 + i * 12, f = i * 5;
      v.setUint16(o, frames[f], true);
      v.setUint16(o + 2, frames[f + 1], true);
      v.setUint16(o + 4, frames[f + 2], true);
      v.setInt16(o + 6, frames[f + 3], true);
      v.setUint32(o + 8, frames[f + 4] >>> 0, true);
    }
    return buf;
  }
  function flush() {
    if (!pending.length) return;
    var msg = pack(pending);
    pending = [];
    if (sent.length < MAX_KEEP / 4) sent.push(msg);
    if (open()) ws.send(msg);
  }
  setInterval(flush, 100);

  function receive(e) {
    if (typeof e.data === 'string') {
      var m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m.t === 'run' && m.sub === sub) {
        live[m.id] = true;
        Module._ghost_remote_color(m.id, m.color | 0);
      }
      else if (m.t === 'end' && live[m.id]) Module._ghost_remote_end(m.id);
      else if (m.t === 'gone' && live[m.id]) {
        delete live[m.id];
        Module._ghost_remote_remove(m.id);
      }
      else if (m.t === 'players') showPlayers(m.list);
      else if (m.t === 'online') setOnline(m.count | 0);
      else if (m.t === 'nonce' && m.sub === sub) Module._ghost_set_nonce(m.n >>> 0);
      else if (m.t === 'stats' && statsReply) { var sr = statsReply; statsReply = null; sr(m); }
      else if (m.t === 'you') {
        myName = String(m.name);
        if (nameReply) { var cb = nameReply; nameReply = null; cb(m.error || null); }
        if (window.skyNameChanged) window.skyNameChanged();
      }
      return;
    }
    if (e.data.byteLength < 16) return;
    var id = new DataView(e.data).getUint32(0, true);
    var n = ((e.data.byteLength - 4) / 12) | 0;
    if (!live[id] || !n) return;
    var p = Module._malloc(n * 12);
    if (!p) return;
    Module.HEAPU8.set(new Uint8Array(e.data, 4, n * 12), p);
    Module._ghost_remote_frames(id, p, n);
    Module._free(p);
  }

  /* settings menu (shell.html) */
  /* statistics: the relay counts your race starts; finished roads are
   * reported here (from the game's save, see cfg_store in port/platform.c) */
  var statsReply = null;
  Module.skyRoadCompleted = function (r) {
    if (open()) ws.send(JSON.stringify({ t: 'completed', road: r }));
  };
  /* cb(stats) with {all, you} as sent by the relay, or cb(null) */
  Module.skyGhostStats = function (cb) {
    if (!open()) { cb(null); return; }
    var timer = setTimeout(function () { statsReply = null; cb(null); }, 5000);
    statsReply = function (m) { clearTimeout(timer); cb(m.error ? null : m); };
    ws.send('{"t":"stats"}');
  };

  /* players connected to the relay (null while not connected) */
  var online = null;
  function setOnline(n) {
    online = n;
    if (window.skyOnlineChanged) window.skyOnlineChanged();
  }
  Module.skyGhostOnline = function () { return online; };

  /* your name: random two words from the relay unless you chose one
   * (kept in localStorage, checked again by the relay) */
  var myName = null, nameReply = null;
  var NAME_RE = /^(?=.{3,20}$)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$/;
  function storedName() {
    try { return localStorage.getItem('skyroads.name') || ''; } catch (e) { return ''; }
  }
  function storeName(n) {
    try { if (n) localStorage.setItem('skyroads.name', n); else localStorage.removeItem('skyroads.name'); } catch (e) {}
  }
  Module.skyGhostName = function () { return myName || storedName() || null; };
  Module.skyGhostChosenName = storedName;
  Module.skyGhostNameValid = function (n) { return n === '' || NAME_RE.test(n); };
  /* cb(error): null, 'invalid', 'taken' or 'offline' */
  Module.skyGhostSetName = function (n, cb) {
    if (!Module.skyGhostNameValid(n)) { cb('invalid'); return; }
    if (!open()) {                      // single player / offline: used when connecting
      storeName(n);
      if (n) myName = n; else myName = null;
      cb(null);
      return;
    }
    var timer = setTimeout(function () { nameReply = null; cb('offline'); }, 5000);
    nameReply = function (err) {
      clearTimeout(timer);
      if (!err) storeName(n);
      cb(err);
    };
    ws.send(JSON.stringify({ t: 'name', name: n }));
  };

  Module.skyGhostMultiplayer = function () { return multiplayer; };
  Module.skyGhostSetMultiplayer = function (on) {
    multiplayer = !!on;
    try { localStorage.setItem('skyroads.multiplayer', multiplayer ? '1' : '0'); } catch (e) {}
    Module._ghost_set_multiplayer(multiplayer ? 1 : 0);
    if (multiplayer) {
      backoff = 1000;
      connect();          // mid-run: resends the run so far once connected
    } else {
      disconnect();
      live = {};
      Module._ghost_remote_clear();   // drop the other players' ghosts now
    }
  };

  /* called by port/ghost.c (proxied to the main thread) */
  Module.skyGhostLive = function () { return multiplayer; };
  Module.skyGhostStart = function (r) {
    clearTimeout(hideTimer);
    sub++; road = r; racing = true; runStart = Date.now();
    updateListVisibility();
    pending = []; sent = [];
    if (open()) sendStart();
  };
  /* the run is followed in single player too, so that switching multiplayer
   * on mid-run can send it to the relay */
  Module.skyGhostFrame = function (t, x, y, spr, z) {
    if (racing) pending.push(t, x, y, spr, z);
  };
  /* from port/ghost.c: finishTicks = run clock when the road was finished
   * (0 = not finished), ticks = run clock at the end (36 a second), sig =
   * the game's signature of both with the relay's nonce for this run */
  Module.skyGhostEnd = function (finishTicks, ticks, sig) {
    flush();
    racing = false; sent = [];
    if (open())
      ws.send(JSON.stringify({ t: 'end', ticks: ticks >>> 0, finish: finishTicks >>> 0, sig: String(sig || '') }));
    // after a crash the game restarts the road right away (~1 ms): keep the
    // list up over that gap, hide it when the player goes back to the menus
    clearTimeout(hideTimer);
    hideTimer = setTimeout(updateListVisibility, 500);
  };

  connect();
})();

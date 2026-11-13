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
 * left of the game, each with the colour of their ghost. */
(function () {
  var url = new URLSearchParams(location.search).get('relay') || window.SKY_GHOST_RELAY;
  if (!url || typeof Module === 'undefined') return;

  var player = playerId();
  var ws = null, backoff = 1000;
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
    listEl.classList.toggle('show', !!(list && list.length));
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
    try { ws = new WebSocket(url); } catch (e) { retry(); return; }
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () {
      backoff = 1000;
      if (racing) sendStart();          // reconnected mid-run: resend it
    };
    ws.onclose = function () { ws = null; showPlayers([]); retry(); };
    ws.onerror = function () {};
    ws.onmessage = receive;
  }
  function retry() {
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 30000);
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

  /* called by port/ghost.c (proxied to the main thread) */
  Module.skyGhostStart = function (r) {
    sub++; road = r; racing = true; runStart = Date.now();
    pending = []; sent = [];
    if (open()) sendStart();
  };
  Module.skyGhostFrame = function (t, x, y, spr, z) {
    if (racing) pending.push(t, x, y, spr, z);
  };
  Module.skyGhostEnd = function () {
    flush();
    racing = false; sent = [];
    if (open()) ws.send('{"t":"end"}');
  };

  connect();
})();

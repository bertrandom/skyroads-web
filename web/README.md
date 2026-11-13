# SkyRoads – WebAssembly build

Browser version of the native port in `../port`, compiled with Emscripten. The
game code is shared with the native build; browser-specific code is behind
`#ifdef __EMSCRIPTEN__` in `../port`.

## Build & run

Requirements: Emscripten (`brew install emscripten`) and Python 3. The first
build downloads Emscripten's SDL2 port.

    cd web
    make            # -> dist/index.html, index.js, index.wasm, index.data
    make serve      # http://localhost:8000

`dist/` is a static site. It uses WebAssembly threads, so it needs
SharedArrayBuffer and therefore cross-origin isolation:

* `serve.py` (used by `make serve`) sends the required
  `Cross-Origin-Opener-Policy: same-origin` and
  `Cross-Origin-Embedder-Policy: require-corp` headers.
* On hosts that can't set headers (e.g. GitHub Pages), the bundled
  `coi-serviceworker.js` (MIT, Guido Zuidhof) adds them with a service worker;
  the page reloads itself once on the first visit.
* Opening `index.html` from `file://` will not work.

The game data (`../resources/Skyroads/*.LZS`, `*.DAT`, `*.SND`, `DEMO.REC`) is
packed into `index.data`. Check the licence of the data before publishing it.

## Browser specifics

| | |
|---|---|
| Threads | The game and the 180 Hz timer run in Web Workers (pthreads); the browser main thread runs the SDL event/render loop (`emscripten_set_main_loop`). |
| Start screen | Browsers only allow audio after a user gesture, so the game thread waits on a \"click or press a key\" screen; the intro then starts from the beginning with sound. The dismissing key is not passed to the game. |
| Audio | OPL register writes are queued with timer timestamps and replayed by the synthesizer on the main thread. |
| Saves | `skyroads.cfg` is kept in IndexedDB (`/save`, IDBFS), loaded before the game starts and flushed after every write. |
| Quit | Esc on the main menu ends the game and shows a "play again" screen. |
| Memory | Host data is placed above 64 KiB (`-sGLOBAL_BASE=128KB`) so `near_ptr()` can tell 16-bit DS offsets from wasm32 pointers. |

Debug hooks from the native port work as URL parameters, e.g.
`http://localhost:8000/?SKY_AUTOKEYS=0d,0d&SKY_WARP=12` (start a race and skip
to just before the end of the road).

# WebAssembly build of the SkyRoads port (Emscripten + SDL2 + pthreads).
#
#   make            build dist/ (index.html, index.js, index.wasm, index.data)
#   make serve      serve dist/ on http://localhost:8000 with the COOP/COEP
#                   headers required for SharedArrayBuffer (threads)
#   make clean
#
#   make GHOST_RELAY=wss://example.com/skyroads-relay GHOST_KEY=<hex>
#                   live ghosts of other players through the relay in
#                   ../server; GHOST_KEY (the relay's relay.key) lets the
#                   game sign the end of each run for the statistics
#
# The game code is shared with the native port in ../port.
EMCC  ?= emcc
PORT  := ../port
DATA  := ../resources/Skyroads
OUT   := dist
BUILD := build
GHOST_RELAY ?=
GHOST_KEY ?=

SRCS := main.c runtime.c video.c platform.c hw.c audio.c manual.c render.c game.c ghost.c sign.c
OBJS := $(addprefix $(BUILD)/,$(SRCS:.c=.o)) $(BUILD)/opl3.o

CFLAGS := -O2 -std=gnu11 -I$(PORT) -I$(BUILD) -pthread -sUSE_SDL=2 \
          -fno-strict-aliasing -fwrapv -w -Wno-int-conversion \
          -Wno-incompatible-pointer-types -Wno-incompatible-function-pointer-types

LDFLAGS := -O2 -pthread -sUSE_SDL=2 \
           -sPTHREAD_POOL_SIZE=4 \
           -sINITIAL_MEMORY=64MB \
           -sSTACK_SIZE=1MB -sDEFAULT_PTHREAD_STACK_SIZE=1MB \
           -sGLOBAL_BASE=128KB \
           -sENVIRONMENT=web,worker \
           -sEXPORTED_RUNTIME_METHODS=ENV,HEAPU8 \
           -sEXPORTED_FUNCTIONS=_main,_malloc,_free \
           --preload-file $(BUILD)/data@/data \
           --shell-file shell.html

# site icons (made from a frame of the intro by icons/gen_icons.py; committed) and the web app manifest
ICONS     := favicon.ico apple-touch-icon.png icon-192.png icon-512.png icon-maskable-512.png
ICONS_OUT := $(addprefix $(OUT)/,$(ICONS)) $(OUT)/manifest.webmanifest

all: $(OUT)/index.html $(OUT)/live-ghosts.js $(OUT)/relay.js $(OUT)/about.html $(ICONS_OUT)

$(OUT)/%: icons/%
	mkdir -p $(OUT)
	cp $< $@
$(OUT)/manifest.webmanifest: manifest.webmanifest
	mkdir -p $(OUT)
	cp $< $@

# generated sources come from the native port's pipeline
$(PORT)/game.c $(PORT)/protos.h $(PORT)/image_data.h:
	$(MAKE) -C $(PORT) game.c image_data.h

$(BUILD)/%.o: $(PORT)/%.c $(PORT)/compat.h $(PORT)/protos.h $(PORT)/image_data.h | $(BUILD)
	$(EMCC) $(CFLAGS) -c $< -o $@

$(BUILD):
	mkdir -p $(BUILD)

# Nuked-OPL3 (LGPL-2.1), the AdLib chip emulator
$(BUILD)/opl3.o: $(PORT)/thirdparty/nuked-opl3/opl3.c $(PORT)/thirdparty/nuked-opl3/opl3.h | $(BUILD)
	$(EMCC) $(CFLAGS) -c $< -o $@

# the relay key for sign.c (rewritten only when GHOST_KEY changes)
$(BUILD)/relay_key.h: FORCE | $(BUILD)
	@python3 gen_key_header.py $@ '$(GHOST_KEY)'
$(BUILD)/sign.o: $(BUILD)/relay_key.h

# only the files the game reads
$(BUILD)/data: $(wildcard $(DATA)/*.LZS $(DATA)/*.DAT $(DATA)/*.SND $(DATA)/*.REC)
	rm -rf $@ && mkdir -p $@
	cp $(DATA)/*.LZS $(DATA)/*.DAT $(DATA)/*.SND $(DATA)/DEMO.REC $@/

$(OUT)/index.html: $(OBJS) $(BUILD)/data shell.html
	mkdir -p $(OUT)
	$(EMCC) $(OBJS) $(LDFLAGS) -o $@
	cp coi-serviceworker.js $(OUT)/ 2>/dev/null || true

# the About dialog's text, written in about.md, with the git revision built
# (short hash, "-dirty" with uncommitted changes) in its lower right corner
REV := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)$(shell git diff --quiet HEAD -- .. 2>/dev/null || echo -dirty)
$(BUILD)/rev.txt: FORCE
	@mkdir -p $(BUILD)
	@echo '$(REV)' | cmp -s - $@ || echo '$(REV)' > $@
FORCE:

$(OUT)/about.html: about.md gen_about.py pyproject.toml $(BUILD)/rev.txt
	mkdir -p $(OUT)
	uv run --quiet gen_about.py about.md $@ "$$(cat $(BUILD)/rev.txt)"

$(OUT)/live-ghosts.js: live-ghosts.js
	mkdir -p $(OUT)
	cp $< $@

# relay URL for live ghosts (rewritten on every build so GHOST_RELAY applies)
$(OUT)/relay.js: FORCE
	mkdir -p $(OUT)
	printf 'window.SKY_GHOST_RELAY = "%s";\n' '$(GHOST_RELAY)' > $@

serve: all
	python3 serve.py $(OUT)

clean:
	rm -rf $(BUILD) $(OUT)

.PHONY: all serve clean FORCE
FORCE:

# WebAssembly build of the SkyRoads port (Emscripten + SDL2 + pthreads).
#
#   make            build dist/ (index.html, index.js, index.wasm, index.data)
#   make serve      serve dist/ on http://localhost:8000 with the COOP/COEP
#                   headers required for SharedArrayBuffer (threads)
#   make clean
#
#   make GHOST_RELAY=wss://example.com/skyroads-relay
#                   show live ghosts of other players through the relay in
#                   ../server (without it: your own saved runs)
#
# The game code is shared with the native port in ../port.
EMCC  ?= emcc
PORT  := ../port
DATA  := ../resources/Skyroads
OUT   := dist
BUILD := build
GHOST_RELAY ?=

SRCS := main.c runtime.c video.c platform.c hw.c audio.c manual.c render.c game.c ghost.c
OBJS := $(addprefix $(BUILD)/,$(SRCS:.c=.o))

CFLAGS := -O2 -std=gnu11 -I$(PORT) -pthread -sUSE_SDL=2 \
          -fno-strict-aliasing -fwrapv -w -Wno-int-conversion \
          -Wno-incompatible-pointer-types -Wno-incompatible-function-pointer-types

LDFLAGS := -O2 -pthread -sUSE_SDL=2 \
           -sPTHREAD_POOL_SIZE=4 \
           -sINITIAL_MEMORY=64MB \
           -sSTACK_SIZE=1MB -sDEFAULT_PTHREAD_STACK_SIZE=1MB \
           -sGLOBAL_BASE=128KB \
           -sENVIRONMENT=web,worker \
           -lidbfs.js -sEXPORTED_RUNTIME_METHODS=ENV,HEAPU8 \
           -sEXPORTED_FUNCTIONS=_main,_malloc,_free \
           --preload-file $(BUILD)/data@/data \
           --shell-file shell.html

all: $(OUT)/index.html $(OUT)/live-ghosts.js $(OUT)/relay.js

# generated sources come from the native port's pipeline
$(PORT)/game.c $(PORT)/protos.h $(PORT)/image_data.h:
	$(MAKE) -C $(PORT) game.c image_data.h

$(BUILD)/%.o: $(PORT)/%.c $(PORT)/compat.h $(PORT)/protos.h $(PORT)/image_data.h | $(BUILD)
	$(EMCC) $(CFLAGS) -c $< -o $@

$(BUILD):
	mkdir -p $(BUILD)

# only the files the game reads
$(BUILD)/data: $(wildcard $(DATA)/*.LZS $(DATA)/*.DAT $(DATA)/*.SND $(DATA)/*.REC)
	rm -rf $@ && mkdir -p $@
	cp $(DATA)/*.LZS $(DATA)/*.DAT $(DATA)/*.SND $(DATA)/DEMO.REC $@/

$(OUT)/index.html: $(OBJS) $(BUILD)/data shell.html
	mkdir -p $(OUT)
	$(EMCC) $(OBJS) $(LDFLAGS) -o $@
	cp coi-serviceworker.js $(OUT)/ 2>/dev/null || true

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

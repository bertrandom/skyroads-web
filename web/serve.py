#!/usr/bin/env python3
"""Static file server with the cross-origin isolation headers that
SharedArrayBuffer (and therefore WebAssembly threads) requires."""
import http.server
import os
import sys


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".wasm": "application/wasm", ".js": "text/javascript",
                      ".data": "application/octet-stream",
                      ".webmanifest": "application/manifest+json"}

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "dist"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    os.chdir(root)
    print("serving %s on http://localhost:%d" % (root, port))
    http.server.ThreadingHTTPServer(("", port), Handler).serve_forever()

#!/usr/bin/env python3
"""
JARVIS - tiny web server (Stage 1).

Serves ONLY the viewer/ folder on port 4700, and (from Stage 2) exposes the
POST /chat brain. Standard library only.

    python3 server.py

Then open http://localhost:4700
"""

from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.join(HERE, "viewer")
PORT = int(os.environ.get("JARVIS_PORT", "4700"))


class ViewerHandler(SimpleHTTPRequestHandler):
    """Static file server rooted at viewer/ - nothing outside it is reachable."""

    server_version = "JARVIS/1.0"

    def __init__(self, *args, **kwargs):
        kwargs["directory"] = VIEWER_DIR
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        # belt and braces: never let a request escape the viewer folder
        local = super().translate_path(path)
        root = os.path.realpath(VIEWER_DIR)
        local = os.path.realpath(local)
        if local != root and not local.startswith(root + os.sep):
            return os.path.join(root, "__forbidden__")
        return local

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("JARVIS %s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    if not os.path.isdir(VIEWER_DIR):
        print("JARVIS: viewer/ folder missing. Run: python3 build.py", file=sys.stderr)
        return 1
    if not os.path.exists(os.path.join(VIEWER_DIR, "graph-data.js")):
        print("JARVIS: viewer/graph-data.js missing - building it now.", file=sys.stderr)

    ThreadingHTTPServer.allow_reuse_address = True
    with ThreadingHTTPServer(("0.0.0.0", PORT), ViewerHandler) as httpd:
        print("JARVIS online -> http://localhost:%d  (serving %s)" % (PORT, VIEWER_DIR))
        print("Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nJARVIS: shutting down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

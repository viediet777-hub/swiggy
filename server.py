#!/usr/bin/env python3
"""
Swiggy Offer Finder - local dev server.

    python server.py            # http://127.0.0.1:8000
    python server.py --port 9000

    OFFER_KEY=SECRET python server.py     # gate the API with ?key=SECRET

Same logic as the Vercel deployment (see api/*.py + offer_core.py).
"""

import argparse
import os
import sys
from http.server import ThreadingHTTPServer

from offer_core import BaseHandler, log, required_key


class Handler(BaseHandler):
    endpoint = None            # local server dispatches on the request path


def main():
    ap = argparse.ArgumentParser(description="Swiggy Offer Finder server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ThreadingHTTPServer.allow_reuse_address = (os.name != "nt")
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        log("Port %s pe pehle se kuch chal raha hai (shayad purana server)." % args.port)
        log("Use band karo (Ctrl+C) ya:  python server.py --port 9000")
        sys.exit(1)
    srv.daemon_threads = True
    gate = required_key()
    log("=" * 60)
    log(" Swiggy Offer Finder running")
    log(" Open  http://%s:%s" % (args.host, args.port))
    log(" Access key: %s" % (gate if gate else "OFF (OFFER_KEY set karke enable karo)"))
    log(" Ctrl+C to stop")
    log("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("\nstopped")


if __name__ == "__main__":
    main()

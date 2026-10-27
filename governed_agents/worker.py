"""Worker process: claims runs from the shared store and executes them.

Run as many as you like; Postgres SKIP LOCKED keeps them from colliding.
It also serves /healthz on $PORT so platforms that expect an HTTP server
(Cloud Run, Kubernetes probes) can check it, and exits cleanly on SIGTERM
after the current run.
"""
from __future__ import annotations

import os
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from .agents import build
from .guards import Guards
from .model import from_env
from .runtime import Runtime
from .store import Store

stop = threading.Event()
last_loop = time.time()


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        healthy = time.time() - last_loop < 120
        self.send_response(200 if healthy else 503)
        self.end_headers()
        self.wfile.write(b"ok" if healthy else b"stalled")

    def log_message(self, *args):
        pass


def main() -> None:
    global last_loop
    store = Store(os.environ.get("DATABASE_URL", "sqlite:///agents.db"))
    registry, _, _ = build(store)
    rt = Runtime(store, registry, from_env(), Guards({"leads-private"}),
                 worker=f"{socket.gethostname()}-{os.getpid()}")
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8081"))), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        last_loop = time.time()
        if rt.work_once() is None:
            stop.wait(1.0)
    server.shutdown()


if __name__ == "__main__":
    main()

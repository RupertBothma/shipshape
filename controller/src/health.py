from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler serving liveness, readiness, and Prometheus metrics endpoints."""

    ready_event: threading.Event
    leader_event: threading.Event | None

    def _leader_ready(self) -> bool:
        return self.leader_event is None or self.leader_event.is_set()

    def _respond(
        self, status: int, body: bytes = b"", content_type: str | None = None
    ) -> None:
        """Send an HTTP response with optional body and content type."""
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, b"ok")
        elif self.path == "/leadz":
            if self._leader_ready():
                self._respond(200, b"ok")
            else:
                self._respond(503, b"not leader")
        elif self.path == "/readyz":
            ready = self.ready_event.is_set()
            leader_ready = self._leader_ready()
            if ready and leader_ready:
                self._respond(200, b"ready=true leader=true")
            else:
                ready_text = "true" if ready else "false"
                leader_text = "true" if leader_ready else "false"
                self._respond(503, f"ready={ready_text} leader={leader_text}".encode())
        elif self.path == "/metrics":
            try:
                from prometheus_client import generate_latest

                output = generate_latest()
                self._respond(200, output, "text/plain; version=0.0.4; charset=utf-8")
            except ImportError:
                self._respond(501)
        else:
            self._respond(404)

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.getLogger("controller.health").debug(fmt, *args)


def make_health_handler(
    ready: threading.Event, leader: threading.Event | None = None
) -> type[_HealthHandler]:
    """Return a handler class bound to the given readiness event.

    Uses class-level attribute binding so the stdlib HTTPServer can
    instantiate handlers without constructor arguments.
    """

    class _BoundHealthHandler(_HealthHandler):
        ready_event = ready
        leader_event = leader

    return _BoundHealthHandler


def start_health_server(
    ready: threading.Event, port: int, leader: threading.Event | None = None
) -> ThreadingHTTPServer:
    """Start the health/metrics HTTP server in a daemon thread and return it."""
    handler_class = make_health_handler(ready, leader=leader)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_class)  # noqa: S104
    server.daemon_threads = True
    server.block_on_close = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.getLogger(__name__).info("Health server listening on :%d", port)
    return server

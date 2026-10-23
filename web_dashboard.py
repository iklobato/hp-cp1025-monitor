"""Local real-time web dashboard: one reader thread polls the printer, browsers get Server-Sent Events.

Binds to 127.0.0.1 unless told otherwise (the Raspberry Pi serves the home LAN) and exposes
read-only GET endpoints:
  /             the page (dashboard.html)
  /api/status   latest reading as JSON
  /events       SSE stream, one JSON message per new reading
"""

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hpstatus import UsbError
from poller import MIN_INTERVAL_SECONDS, Poller
from viewmodel import build_payload

PAGE = Path(__file__).with_name("dashboard.html")
HOST = "127.0.0.1"
DEFAULT_PORT = 8025
CHANGE_FEED_SIZE = 30
SSE_KEEPALIVE_SECONDS = 15


class PrinterFeed:
    """Owns the USB handle loop; every other thread only reads the latest serialized payload."""

    def __init__(self, warn_percent, poller=None, min_interval=MIN_INTERVAL_SECONDS):
        self.warn_percent = warn_percent
        # A print server polls slower: while a reading holds the USB device, CUPS may not see it.
        self.min_interval = min_interval
        self.poller = poller or Poller(feed_size=CHANGE_FEED_SIZE)
        self.version = 0
        self.payload = json.dumps({"loading": True})
        self.error = ""
        self.updated = threading.Condition()

    def run_forever(self):
        while True:
            try:
                reading = self.poller.tick()
            except (UsbError, RuntimeError) as error:
                self.publish_error(error)
                time.sleep(self.min_interval)
                continue
            payload = build_payload(reading, self.warn_percent)
            self.publish(json.dumps(payload, ensure_ascii=False, default=str))
            time.sleep(max(reading.next_in, self.min_interval))

    def publish(self, payload):
        with self.updated:
            self.payload, self.error = payload, ""
            self.version += 1
            self.updated.notify_all()

    def publish_error(self, error):
        # Keep the last good reading; the page shows the failure next to it.
        with self.updated:
            self.error = f"{datetime.now():%H:%M:%S} leitura falhou: {error}"
            self.version += 1
            self.updated.notify_all()

    def snapshot(self):
        with self.updated:
            return self.version, self.payload, self.error

    def wait_newer(self, version, timeout):
        with self.updated:
            self.updated.wait_for(lambda: self.version != version, timeout=timeout)
            return self.version, self.payload, self.error


def message(payload, error):
    data = json.loads(payload)
    data["feed_error"] = error
    return json.dumps(data, ensure_ascii=False)


def make_handler(feed):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            routes = {
                "/": self.page,
                "/api/status": self.status,
                "/events": self.events,
            }
            route = routes.get(self.path.split("?")[0])
            if route is None:
                self.send_error(404)
                return
            route()

        def page(self):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def status(self):
            _, payload, error = feed.snapshot()
            body = message(payload, error).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            version, payload, error = feed.snapshot()
            try:
                self.wfile.write(f"data: {message(payload, error)}\n\n".encode())
                self.wfile.flush()
                while True:
                    newer, payload, error = feed.wait_newer(
                        version, SSE_KEEPALIVE_SECONDS
                    )
                    if newer == version:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        version = newer
                        self.wfile.write(
                            f"data: {message(payload, error)}\n\n".encode()
                        )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, *args):
            pass

    return Handler


def serve(
    warn_percent, port=DEFAULT_PORT, host=HOST, min_interval=MIN_INTERVAL_SECONDS
):
    feed = PrinterFeed(warn_percent, min_interval=min_interval)
    threading.Thread(target=feed.run_forever, name="printer-feed", daemon=True).start()
    server = ThreadingHTTPServer((host, port), make_handler(feed))
    server.daemon_threads = True
    print(f"Dashboard em http://{host}:{port}  (Ctrl+C para sair)", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()

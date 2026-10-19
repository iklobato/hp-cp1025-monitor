"""Local real-time web dashboard: one reader thread polls the printer, browsers get Server-Sent Events.

Binds to 127.0.0.1 only and exposes read-only GET endpoints:
  /             the page (dashboard.html)
  /api/status   latest reading as JSON
  /events       SSE stream, one JSON message per new reading
"""

import json
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import history
from dashboard import (
    SEVERITY_LOOK,
    action_items,
    controller_rows,
    endpoint_rows,
    humanize,
    humanize_queue,
    next_to_run_out,
    port_bits,
    short_date,
    translate_category,
    translate_state,
    usb_bus_rows,
)
from history import level_series
from hpstatus import UsbError
from printer_model import OK_STATES, collect, event_hint, years_since

PAGE = Path(__file__).with_name("dashboard.html")
HOST = "127.0.0.1"
DEFAULT_PORT = 8025
# Same cadence rule as --live: a full read measured ~275 ms, so 1 s keeps USB mostly idle.
MIN_INTERVAL_SECONDS = 1.0
DUTY_FACTOR = 4
CHANGE_FEED_SIZE = 30
TREND_POINTS = 30
SSE_KEEPALIVE_SECONDS = 15


def consumable_view(c, report, warn_percent, entries, prediction):
    empty = prediction.empty_date(c, report.read_at)
    return {
        "color": c.color,
        "name": c.name,
        "code": c.product_number,
        "state": c.state,
        "state_text": translate_state(c.state),
        "ok": c.state in OK_STATES,
        "low": c.state in OK_STATES and c.needs_attention(warn_percent),
        "percent": c.percent,
        "raw_percent": c.raw_percent,
        "pages_remaining": c.pages_remaining,
        "impressions": c.impressions,
        "rated_yield": c.rated_yield or c.capacity,
        "brand": c.brand,
        "serial": c.serial,
        "manufactured": short_date(c.manufactured, "%m/%Y"),
        "age_years": years_since(c.manufactured, report.read_at.date()),
        "installed": short_date(c.installed),
        "last_used": short_date(c.last_used),
        "chip": c.chip_tag,
        "chip_ok": c.chip_ok,
        "model": c.model_info,
        "advice": c.advice(warn_percent),
        "trend": level_series(entries, c.color)[-TREND_POINTS:],
        "empty_date": f"{empty:%d/%m/%Y}" if empty else None,
    }


def build_payload(report, warn_percent, entries, prediction, changes, reads, read_ms):
    severity = report.severity(warn_percent)
    counters = report.counters
    total = (counters.get("mono") or 0) + (counters.get("color") or 0)
    return {
        "read_at": report.read_at.isoformat(timespec="seconds"),
        "reads": reads,
        "read_ms": round(read_ms),
        "warn_percent": warn_percent,
        "severity": severity,
        "verdict": SEVERITY_LOOK[severity][0],
        "status": translate_category(report.status_category),
        "status_raw": report.status_category,
        "carousel": report.carousel,
        "alerts": [
            {
                "severity": sev,
                "id": alert_id,
                "text": translate_category(alert_id),
                "colors": colors,
            }
            for sev, alert_id, colors in report.alerts
        ],
        "actions": [
            {"subject": subject, "advice": advice, "color": color}
            for subject, advice, color in action_items(report, warn_percent)
        ],
        "next_out": next_to_run_out(report, prediction),
        "queue": {"paused": report.queue_paused, "text": humanize_queue(report.queue)},
        "port": {**report.port, "bits": port_bits(report.port.get("raw"))},
        "consumables": [
            consumable_view(c, report, warn_percent, entries, prediction)
            for c in report.consumables
        ],
        "counters": {
            **counters,
            "color_percent": (
                round(100 * (counters.get("color") or 0) / total) if total else None
            ),
        },
        "pages_per_day": prediction.pages_per_day,
        "events": [
            {"seq": seq, "code": code, "page": page, "hint": event_hint(code)}
            for seq, code, page in report.events
        ],
        "changes": [
            {"at": f"{at:%H:%M:%S}", "text": text} for at, text in reversed(changes)
        ],
        "usb_bus": usb_bus_rows(report),
        "controller": controller_rows(report),
        "endpoints": endpoint_rows(report),
        "identity": {k: humanize(v) for k, v in report.identity.items()},
        "settings": {k: humanize(v) for k, v in report.settings.items()},
        "latency_ms": report.latency_ms,
        "errors": report.errors,
    }


class PrinterFeed:
    """Owns the USB handle loop; every other thread only reads the latest serialized payload."""

    def __init__(self, warn_percent):
        self.warn_percent = warn_percent
        self.changes = deque(maxlen=CHANGE_FEED_SIZE)
        self.version = 0
        self.payload = json.dumps({"loading": True})
        self.error = ""
        self.updated = threading.Condition()

    def run_forever(self):
        previous, reads, interval = None, 0, MIN_INTERVAL_SECONDS
        while True:
            started = time.monotonic()
            try:
                report = collect()
            except (UsbError, RuntimeError) as error:
                self.publish_error(error)
                time.sleep(interval)
                continue
            read_seconds = time.monotonic() - started
            entries = history.record(report)
            reads += 1
            if previous is not None:
                self.changes.extend(
                    (report.read_at, change)
                    for change in report.changes_since(previous)
                )
            previous = report
            payload = build_payload(
                report,
                self.warn_percent,
                entries,
                history.forecast(entries),
                self.changes,
                reads,
                read_seconds * 1000,
            )
            self.publish(json.dumps(payload, ensure_ascii=False, default=str))
            interval = max(MIN_INTERVAL_SECONDS, read_seconds * DUTY_FACTOR)
            time.sleep(interval)

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


def serve(warn_percent, port=DEFAULT_PORT):
    feed = PrinterFeed(warn_percent)
    threading.Thread(target=feed.run_forever, name="printer-feed", daemon=True).start()
    server = ThreadingHTTPServer((HOST, port), make_handler(feed))
    server.daemon_threads = True
    print(f"Dashboard em http://{HOST}:{port}  (Ctrl+C para sair)")
    try:
        server.serve_forever()
    finally:
        server.server_close()

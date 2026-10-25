"""Printer and Pi page.

Printer, from the HP CP1025 monitor on this Pi (the same data printer_summary.py shows): the
verdict, port state, toner and when each runs out, pages per day, queue and last jobs, live
changes, the printer's event log, the USB link and its identity. The monitor's last full
reading is saved to disk, so the block is never empty while the printer is off.
Pi, straight from /proc and /sys, every second: each core, clock, memory breakdown, disk and
network rates, top processes, sockets; and every minute: power, services, journal errors,
failed units, USB devices and the system itself.
"""

import importlib.util
import os
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import net
import procfs
import system_data
import ui
from net import SourceError
from PIL import Image, ImageDraw
from system_data import Job, Network, PiStats, PrinterReading, PrinterStatus

MONITOR_DIR = Path.home() / "hp-cp1025-monitor"
HISTORY = MONITOR_DIR / "history.jsonl"
STATUS_URL = os.environ.get(
    "PI_DASHBOARD_PRINTER_URL", "http://127.0.0.1:8025/api/status"
)
STATUS_SAVED = net.CACHE_DIR / "printer-status.json"
# Each block runs its commands on its own thread, so each needs its own output file.
PRINTER_SCRATCH = net.CACHE_DIR / "command-printer.txt"
PI_SCRATCH = net.CACHE_DIR / "command-pi.txt"
SERVICES = (
    ("cups", "CUPS"),
    ("cp1025-dashboard", "monitor"),
    ("dashboard", "painel"),
)
# The monitor answers every second; the SD card is written at most once a minute.
SAVE_EVERY = timedelta(minutes=1)
SLOW_REFRESH = timedelta(seconds=30)  # history file and CUPS queue
HEALTH_REFRESH = timedelta(minutes=1)
UPDATES_REFRESH = timedelta(hours=1)
# Temperature and load charts: one point every 15 s, 480 points = the last 2 hours.
CHART_STEP = timedelta(seconds=15)
SAMPLES_KEPT = 480
RATE_SAMPLES = 120  # network chart: the last 2 minutes at one sample a second
# Pi 3 firmware lowers the clock from 60 °C (soft limit) and throttles hard at 80 °C.
WARM_C, HOT_C = 60, 80
PAGE_DAYS_SHOWN = 14
ROW = 23
SEVERITY_COLORS = {0: ui.GREEN, 1: ui.AMBER, 2: ui.RED}
CACHE_COLOR = ui.blend(ui.BLUE, ui.CARD, 0.45)
BUFFER_COLOR = ui.blend(ui.AMBER, ui.CARD, 0.7)

SUPPLIES = (
    ("Black", "Preto", (170, 170, 170)),
    ("Cyan", "Ciano", (0, 174, 239)),
    ("Magenta", "Magenta", (236, 0, 140)),
    ("Yellow", "Amarelo", (255, 221, 0)),
    ("OPC", "Tambor", (125, 133, 144)),
)
STATUS_NAMES = {
    "inPowerSave": "economia de energia",
    "idle": "pronta",
    "printing": "imprimindo",
    "warmup": "aquecendo",
    "ok": "ok",
}
ALERT_NAMES = {
    "incompatibleConsumable": "cartucho não original",
    "tonerLow": "toner baixo",
    "tonerEmpty": "toner acabou",
    "paperOut": "sem papel",
    "paperJam": "papel preso",
    "doorOpen": "tampa aberta",
}
# The monitor's errors are English text; known ones get a plain Portuguese line.
KNOWN_ERRORS = (("printer not found on USB", "impressora desligada ou fora do USB"),)
MISSING = (None, "", "–")


def load_monitor_history():
    """The printer monitor's own history module: its toner forecast is reused, not rewritten."""
    spec = importlib.util.spec_from_file_location(
        "cp1025_history", MONITOR_DIR / "history.py"
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, SyntaxError, ImportError):
        return None
    return module


@dataclass(frozen=True)
class Consumable:
    """What the monitor's Forecast.days_left expects: a color and the pages left."""

    color: str
    pages_remaining: int | None


def readable_error(error: str) -> str:
    for needle, text in KNOWN_ERRORS:
        if needle in error:
            return text
    return error


def thousands(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def temperature_color(celsius: float) -> tuple:
    if celsius >= HOT_C:
        return ui.RED
    return ui.AMBER if celsius >= WARM_C else ui.GREEN


def duration(delta: timedelta) -> str:
    days, rest = divmod(int(delta.total_seconds()), 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}d {hours}h" if days else f"{hours}h {rest // 60}min"


def speed(bytes_per_second: float) -> str:
    if bytes_per_second < 1024:
        return f"{bytes_per_second:.0f} B/s"
    if bytes_per_second < 1024 * 1024:
        return f"{bytes_per_second / 1024:.0f} KB/s"
    return ui.decimal(bytes_per_second / 1024 / 1024, 1) + " MB/s"


def megabytes(kilobytes: int) -> str:
    return f"{kilobytes / 1024:.0f} MB"


def draw_bar(draw, box: tuple, fraction: float, color: tuple) -> None:
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=(bottom - top) / 2, fill=ui.GRID)
    filled = left + (right - left) * max(0.0, min(fraction, 1.0))
    if filled - left >= bottom - top:
        draw.rounded_rectangle(
            (left, top, filled, bottom), radius=(bottom - top) / 2, fill=color
        )


def draw_rows(
    draw, box: tuple, rows: list[tuple[str, str, tuple]], first: int = 62
) -> None:
    """Label and value lines; values start after the widest label, rows past the card drop."""
    left, top, right, bottom = box
    label_face, value_face = ui.font(15), ui.font(15, bold=True)
    value_x = (
        left
        + 32
        + max((label_face.getlength(label) for label, _, _ in rows), default=0)
    )
    y = top + first
    for label, value, color in rows:
        if y > bottom - 8:
            break
        draw.text((left + 16, y), label, font=label_face, fill=ui.DIM, anchor="ls")
        draw.text(
            (value_x, y),
            ui.fit(value, value_face, right - value_x - 16),
            font=value_face,
            fill=color,
            anchor="ls",
        )
        y += ROW


def toner_end(forecast, key: str, supply: dict, now: datetime) -> str:
    if forecast is None:
        return ""
    when = forecast.empty_date(Consumable(key, supply.get("pages_remaining")), now)
    return f"acaba ~{when:%d/%m/%y}" if when else ""


def reading_time(payload: dict, now: datetime) -> str:
    try:
        return net.clock(datetime.fromisoformat(payload["read_at"]).astimezone(), now)
    except (KeyError, TypeError, ValueError):
        return "?"


def printer_state(status: PrinterStatus, now: datetime) -> tuple[str, tuple]:
    if status.feed_error:
        text = f"✘ {readable_error(status.feed_error)}"
        if status.payload:
            text += f" · dados de {reading_time(status.payload, now)}"
        return text, ui.RED
    if status.payload:
        reads = status.payload.get("reads") or 0
        return (
            f"● ao vivo · leitura nº {thousands(reads)} em {status.payload.get('read_ms', '?')} ms",
            ui.GREEN,
        )
    return "monitor iniciando…", ui.DIM


def draw_verdict(draw, box: tuple, status: PrinterStatus, now: datetime) -> None:
    left, top, right, bottom = box
    ui.card(draw, box)
    payload = status.payload
    state, state_color = printer_state(status, now)
    if not payload:
        draw.text(
            (left + 16, top + 44),
            "Impressora HP CP1025",
            font=ui.font(26, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        draw.text(
            (left + 16, top + 80),
            ui.fit(state, ui.font(17), right - left - 32),
            font=ui.font(17),
            fill=state_color,
            anchor="ls",
        )
        draw.text(
            (left + 16, top + 110),
            "ainda sem leitura completa: aparece quando a impressora ligar",
            font=ui.font(15),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    verdict_face = ui.font(32, bold=True)
    verdict = payload.get("verdict", "")
    draw.text(
        (left + 16, top + 48),
        verdict,
        font=verdict_face,
        fill=SEVERITY_COLORS.get(payload.get("severity"), ui.DIM),
        anchor="ls",
    )
    state_left = left + 40 + verdict_face.getlength(verdict)
    draw.text(
        (right - 16, top + 30),
        ui.fit(state, ui.font(15), right - state_left - 16),
        font=ui.font(15),
        fill=state_color,
        anchor="rs",
    )
    draw.text(
        (right - 16, top + 52),
        "HP LaserJet CP1025",
        font=ui.font(14),
        fill=ui.DIM,
        anchor="rs",
    )
    x, y = left + 16, top + 84
    face = ui.font(14, bold=True)
    for badge in payload.get("port_badges", []):
        ok = bool(badge.get("ok"))
        label = ("✔ " if ok else "✘ ") + str(badge.get("label", ""))
        width = face.getlength(label) + 20
        if x + width > right - 16:
            break
        color = ui.GREEN if ok else ui.RED
        draw.rounded_rectangle(
            (x, y - 13, x + width, y + 13), radius=13, outline=color, width=2
        )
        draw.text((x + 10, y), label, font=face, fill=color, anchor="lm")
        x += width + 8
    rows = payload.get("status_rows", {})
    line = " · ".join(
        f"{key}: {value}" for key, value in rows.items() if key != "Carrossel"
    )
    draw.text(
        (left + 16, bottom - 14),
        ui.fit(line, ui.font(15), right - left - 32),
        font=ui.font(15),
        fill=ui.DIM,
        anchor="ls",
    )


def draw_toner(
    draw, box: tuple, reading: PrinterReading | None, forecast, now: datetime
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Toner e tambor")
    if reading is None:
        draw.text(
            (left + 16, top + 80),
            "nenhuma leitura salva ainda",
            font=ui.font(20),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    state = STATUS_NAMES.get(reading.status, reading.status)
    draw.text(
        (right - 16, top + 30),
        f"lido {net.clock(reading.read_at, now)} · {state}",
        font=ui.font(15),
        fill=ui.DIM,
        anchor="rs",
    )
    step = (bottom - top - 130) / len(SUPPLIES)
    bar_left, bar_right = left + 110, right - 340
    for index, (key, label, color) in enumerate(SUPPLIES):
        supply = reading.supplies.get(key)
        y = top + 72 + index * step
        draw.text(
            (left + 16, y + 6),
            label,
            font=ui.font(17, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        if supply is None:
            draw.text(
                (bar_left, y + 6),
                "sem dado",
                font=ui.font(15),
                fill=ui.DIM,
                anchor="ls",
            )
            continue
        draw_bar(
            draw,
            (bar_left, y - 8, bar_right, y + 10),
            supply.get("percent", 0) / 100,
            color,
        )
        pages = supply.get("pages_remaining")
        detail = f"{supply.get('percent', 0):.0f}%" + (
            f" · ~{thousands(int(pages))} pág." if pages else ""
        )
        draw.text(
            (bar_right + 14, y + 6),
            detail,
            font=ui.font(16, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        end = toner_end(forecast, key, supply, now)
        if end:
            draw.text(
                (right - 16, y + 6), end, font=ui.font(14), fill=ui.DIM, anchor="rs"
            )
    share = reading.color_pages / reading.total_pages if reading.total_pages else 0
    totals = (
        f"{thousands(reading.total_pages)} páginas no total · {thousands(reading.color_pages)} "
        f"coloridas ({share:.0%})"
    )
    if forecast is not None and forecast.pages_per_day:
        totals += f" · ~{ui.decimal(forecast.pages_per_day, 1)} pág./dia"
    draw.text(
        (left + 16, bottom - 44),
        ui.fit(totals, ui.font(16), right - left - 32),
        font=ui.font(16),
        fill=ui.TEXT,
        anchor="ls",
    )
    if reading.alerts:
        alerts = "⚠ " + " · ".join(
            ALERT_NAMES.get(alert, alert) for alert in reading.alerts
        )
        draw.text(
            (left + 16, bottom - 16),
            ui.fit(alerts, ui.font(16, bold=True), right - left - 32),
            font=ui.font(16, bold=True),
            fill=ui.AMBER,
            anchor="ls",
        )


def draw_events(draw, box: tuple, payload: dict) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Mudanças e eventos")
    face, bold = ui.font(15), ui.font(15, bold=True)
    y = top + 60
    draw.text(
        (left + 16, y),
        "mudanças ao vivo",
        font=ui.font(13, bold=True),
        fill=ui.DIM,
        anchor="ls",
    )
    changes = payload.get("changes", [])[:4]
    if not changes:
        y += ROW
        draw.text(
            (left + 16, y),
            "nada mudou desde que o monitor ligou" if payload else "sem leitura",
            font=face,
            fill=ui.DIM,
            anchor="ls",
        )
    for change in changes:
        y += ROW
        draw.text(
            (left + 16, y),
            str(change.get("at", "")),
            font=face,
            fill=ui.DIM,
            anchor="ls",
        )
        draw.text(
            (left + 96, y),
            ui.fit(str(change.get("text", "")), face, right - left - 112),
            font=face,
            fill=ui.TEXT,
            anchor="ls",
        )
    y += ROW + 12
    draw.text(
        (left + 16, y),
        "registro da impressora (código · página)",
        font=ui.font(13, bold=True),
        fill=ui.DIM,
        anchor="ls",
    )
    events = sorted(
        payload.get("events", []), key=lambda event: event.get("seq") or 0, reverse=True
    )
    if not events:
        y += ROW
        draw.text((left + 16, y), "nenhum evento", font=face, fill=ui.DIM, anchor="ls")
    for event in events:
        y += ROW
        if y > bottom - 8:
            break
        code = f"{event.get('code', '?')} · pág. {event.get('page', '?')}"
        draw.text((left + 16, y), code, font=bold, fill=ui.TEXT, anchor="ls")
        hint_x = left + 32 + bold.getlength(code)
        draw.text(
            (hint_x, y),
            ui.fit(str(event.get("hint", "")), face, right - hint_x - 16),
            font=face,
            fill=ui.DIM,
            anchor="ls",
        )


def printer_details(payload: dict) -> list[tuple[str, str, tuple]]:
    """USB link, identity and the settings people ask about; fields the printer left empty drop."""
    identity, settings = payload.get("identity", {}), payload.get("settings", {})
    usb, controller = payload.get("usb_bus", {}), payload.get("controller", {})
    candidates = (
        ("Modelo", identity.get("Modelo")),
        ("Serial", identity.get("Serial")),
        ("Firmware", controller.get("Firmware")),
        ("Memória", identity.get("Memória livre")),
        ("Desliga após", settings.get("Desliga sozinha após")),
        ("USB", usb.get("Velocidade")),
        ("Porta", usb.get("Status 1284")),
        ("Latência", controller.get("Latência")),
    )
    rows = [
        (label, str(value), ui.TEXT)
        for label, value in candidates
        if value not in MISSING
    ]
    errors = payload.get("errors") or []
    rows.append(
        (
            "Erros",
            "; ".join(errors) if errors else "nenhum",
            ui.RED if errors else ui.GREEN,
        )
    )
    return rows


def draw_details(draw, box: tuple, payload: dict) -> None:
    ui.card(draw, box, "USB e identificação")
    if not payload:
        draw.text(
            (box[0] + 16, box[1] + 62),
            "sem leitura completa ainda",
            font=ui.font(15),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    draw_rows(draw, box, printer_details(payload))


def draw_pages_per_day(
    draw, box: tuple, printed: tuple[tuple[date, int], ...], today: date
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Páginas por dia")
    days = [
        today - timedelta(days=offset) for offset in reversed(range(PAGE_DAYS_SHOWN))
    ]
    counts = dict(printed)
    week = sum(counts.get(day, 0) for day in days[-7:])
    draw.text(
        (right - 16, top + 30),
        f"{week} nesta semana",
        font=ui.font(16, bold=True),
        fill=ui.TEXT,
        anchor="rs",
    )
    if not printed:
        draw.text(
            (left + 16, top + 64),
            "o histórico precisa de leituras em dias diferentes",
            font=ui.font(15),
            fill=ui.DIM,
            anchor="ls",
        )
    chart_top, chart_bottom = top + 74, bottom - 30
    slot = (right - left - 32) / len(days)
    high = max(max(counts.values(), default=0), 1)
    for index, day in enumerate(days):
        x0 = left + 16 + index * slot + slot * 0.2
        x1 = left + 16 + (index + 1) * slot - slot * 0.2
        if day in counts:
            height = (chart_bottom - chart_top - 16) * counts[day] / high
            draw.rectangle([x0, chart_bottom - height, x1, chart_bottom], fill=ui.BLUE)
            draw.text(
                ((x0 + x1) / 2, chart_bottom - height - 4),
                str(counts[day]),
                font=ui.font(12),
                fill=ui.TEXT,
                anchor="ms",
            )
        else:
            draw.line([(x0, chart_bottom), (x1, chart_bottom)], fill=ui.GRID, width=2)
        draw.text(
            ((x0 + x1) / 2, bottom - 10),
            f"{day:%d}",
            font=ui.font(12),
            fill=ui.DIM,
            anchor="ms",
        )


def draw_jobs(
    draw, box: tuple, queue: tuple[str, ...], jobs: tuple[Job, ...], now: datetime
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Fila e últimos trabalhos")
    y = top + 66
    waiting = f"{len(queue)} na fila" if queue else "fila vazia"
    draw.text(
        (left + 16, y),
        waiting,
        font=ui.font(18, bold=True),
        fill=ui.AMBER if queue else ui.GREEN,
        anchor="ls",
    )
    for job in jobs:
        y += 30
        if y > bottom - 12:
            break
        draw.text(
            (left + 16, y),
            f"{net.clock(job.finished, now)}",
            font=ui.font(16),
            fill=ui.DIM,
            anchor="ls",
        )
        draw.text(
            (left + 150, y),
            ui.fit(f"{job.name} · {job.user}", ui.font(16), right - left - 170),
            font=ui.font(16),
            fill=ui.TEXT,
            anchor="ls",
        )


@dataclass(frozen=True)
class Health:
    """The Pi's slower checks, read once a minute; None: that check has not answered."""

    throttle: int | None = None
    services: dict[str, str] | None = None
    network: Network | None = None
    updates: int | None = None
    journal_errors: int | None = None
    failed: tuple[str, ...] | None = None
    volts: float | None = None
    system: procfs.System | None = None


def draw_processor(
    draw,
    box: tuple,
    stats: PiStats | None,
    gauges: procfs.Gauges | None,
    live: procfs.Rates | None,
    health: Health,
    charts: tuple[tuple, tuple],
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Processador")
    if stats is None or gauges is None:
        ui.placeholder(draw, box, below=90)
        return
    draw.text(
        (right - 16, top + 30),
        f"ligada há {duration(stats.uptime)}",
        font=ui.font(15),
        fill=ui.DIM,
        anchor="rs",
    )
    draw.text(
        (left + 16, top + 104),
        f"{stats.temperature:.0f} °C",
        font=ui.font(48, bold=True),
        fill=temperature_color(stats.temperature),
        anchor="ls",
    )
    draw.text(
        (left + 16, top + 128),
        "temperatura",
        font=ui.font(15),
        fill=ui.DIM,
        anchor="ls",
    )
    if gauges.clock_mhz is not None:
        current, low, high = gauges.clock_mhz
        draw.text(
            (left + 16, top + 172),
            f"{current} MHz",
            font=ui.font(22, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        draw.text(
            (left + 16, top + 194),
            f"varia de {low} a {high}",
            font=ui.font(14),
            fill=ui.DIM,
            anchor="ls",
        )
    if health.volts is not None:
        draw.text(
            (left + 16, top + 216),
            f"núcleo a {ui.decimal(health.volts, 2)} V",
            font=ui.font(14),
            fill=ui.DIM,
            anchor="ls",
        )
    cores_left, cores_right = left + 220, left + 560
    if live is not None:
        for index, share in enumerate(live.cores):
            y = top + 68 + index * 30
            draw.text(
                (cores_left, y + 6),
                f"núcleo {index + 1}",
                font=ui.font(15),
                fill=ui.DIM,
                anchor="ls",
            )
            draw_bar(
                draw,
                (cores_left + 84, y - 8, cores_right - 56, y + 8),
                share,
                ui.RED if share > 0.9 else ui.BLUE,
            )
            draw.text(
                (cores_right, y + 6),
                f"{share:.0%}",
                font=ui.font(15, bold=True),
                fill=ui.TEXT,
                anchor="rs",
            )
    one, five, fifteen = gauges.load
    lines = [
        f"carga {ui.decimal(one, 2)} · {ui.decimal(five, 2)} · {ui.decimal(fifteen, 2)}  (1, 5, 15 min)"
    ]
    if live is not None:
        lines.append(
            f"{thousands(round(live.switches))} trocas/s · "
            f"{thousands(round(live.interrupts))} interrupções/s"
        )
    for index, line in enumerate(lines):
        draw.text(
            (cores_left, top + 200 + index * 24),
            ui.fit(line, ui.font(14), cores_right - cores_left),
            font=ui.font(14),
            fill=ui.TEXT if index == 0 else ui.DIM,
            anchor="ls",
        )
    temperatures, loads = charts
    chart_left = cores_right + 30
    middle = (top + 44 + bottom) / 2
    for (chart_top, chart_bottom), samples, color, caption in (
        (
            (top + 44, middle - 6),
            temperatures,
            temperature_color(stats.temperature),
            f"temperatura, 2 h · máx {max(temperatures, default=0):.0f} °C",
        ),
        (
            (middle + 6, bottom - 12),
            loads,
            ui.BLUE,
            f"carga por núcleo, 2 h · máx {max(loads, default=0):.0%}",
        ),
    ):
        draw.text(
            (chart_left, chart_top + 12),
            caption,
            font=ui.font(13),
            fill=ui.DIM,
            anchor="ls",
        )
        ui.line_chart(
            draw,
            (chart_left, chart_top + 18, right - 16, chart_bottom),
            samples,
            color,
            fill=0.18,
        )


def draw_resources(
    draw,
    box: tuple,
    gauges: procfs.Gauges | None,
    stats: PiStats | None,
    live: procfs.Rates | None,
    downloads: tuple[float, ...],
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Memória, disco e rede")
    if gauges is None or stats is None:
        return
    bar_left, bar_right = left + 100, left + round((right - left) * 0.46)
    text_x, face = bar_right + 16, ui.font(15, condensed=True)
    memory = gauges.memory
    swap_used = memory.swap_total - memory.swap_free
    disk_text = f"{stats.disk_used:.0%} cheio"
    network_text = "Wi-Fi " + (
        f"{gauges.wifi_dbm:.0f} dBm" if gauges.wifi_dbm is not None else "desligado"
    )
    if live is not None:
        disk_text += (
            f" · lê {speed(live.read_bytes)} · grava {speed(live.write_bytes)} · "
            f"{live.read_ops + live.write_ops:.0f} op/s"
        )
        network_text = (
            f"↓ {speed(live.down_bytes)}  ↑ {speed(live.up_bytes)} · {network_text}"
        )
    rows = (
        (
            "Memória",
            f"{megabytes(memory.used)} usada · {megabytes(memory.cached + memory.buffers)} cache · "
            f"{megabytes(memory.free)} livre de {megabytes(memory.total)}",
        ),
        (
            "Swap",
            (
                f"{megabytes(swap_used)} de {megabytes(memory.swap_total)}"
                if memory.swap_total
                else "sem swap"
            ),
        ),
        ("Disco", disk_text),
        ("Rede", network_text),
    )
    step = (bottom - top - 62) / len(rows)
    for index, (label, text) in enumerate(rows):
        y = top + 66 + index * step
        draw.text((left + 16, y + 6), label, font=ui.font(15), fill=ui.DIM, anchor="ls")
        bar = (bar_left, y - 8, bar_right, y + 8)
        if label == "Memória":
            draw.rounded_rectangle(bar, radius=8, fill=ui.GRID)
            x = bar_left
            for amount, color in (
                (memory.used, ui.BLUE),
                (memory.buffers, BUFFER_COLOR),
                (memory.cached, CACHE_COLOR),
            ):
                width = (bar_right - bar_left) * amount / memory.total
                draw.rectangle((x, y - 8, x + width, y + 8), fill=color)
                x += width
        elif label == "Swap":
            draw_bar(
                draw,
                bar,
                swap_used / memory.swap_total if memory.swap_total else 0,
                ui.AMBER,
            )
        elif label == "Disco":
            draw_bar(
                draw, bar, stats.disk_used, ui.RED if stats.disk_used > 0.9 else ui.BLUE
            )
        else:
            ui.line_chart(
                draw,
                (bar_left, y - 12, bar_right, y + 10),
                downloads,
                ui.GREEN,
                fill=0.25,
            )
        draw.text(
            (text_x, y + 6),
            ui.fit(text, face, right - text_x - 16),
            font=face,
            fill=ui.TEXT,
            anchor="ls",
        )


def draw_processes(draw, box: tuple, live: procfs.Rates | None) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Processos que mais gastam")
    columns = (("pid", right - 250), ("CPU", right - 140), ("memória", right - 16))
    draw.text(
        (left + 16, top + 58),
        "processo",
        font=ui.font(13, bold=True),
        fill=ui.DIM,
        anchor="ls",
    )
    for label, x in columns:
        draw.text(
            (x, top + 58), label, font=ui.font(13, bold=True), fill=ui.DIM, anchor="rs"
        )
    if live is None:
        draw.text(
            (left + 16, top + 84),
            "medindo…",
            font=ui.font(15),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    face, bold = ui.font(15), ui.font(15, bold=True)
    for index, process in enumerate(live.top):
        y = top + 84 + index * ROW
        if y > bottom - 6:
            break
        draw.text(
            (left + 16, y),
            ui.fit(process.name, face, right - left - 300),
            font=face,
            fill=ui.TEXT,
            anchor="ls",
        )
        for (_, x), text, font in zip(
            columns,
            (str(process.pid), f"{process.cpu:.0%}", megabytes(process.rss_kb)),
            (face, bold, face),
        ):
            draw.text(
                (x, y),
                text,
                font=font,
                fill=ui.TEXT if font is bold else ui.DIM,
                anchor="rs",
            )


def power_row(throttle: int | None) -> tuple[str, str, tuple]:
    if throttle is None:
        return "Energia e calor", "sem leitura", ui.DIM
    now_notes, past_notes = system_data.throttle_notes(throttle)
    if now_notes:
        return "Energia e calor", ", ".join(now_notes), ui.RED
    if past_notes:
        return (
            "Energia e calor",
            "ok agora; desde que ligou: " + ", ".join(past_notes),
            ui.AMBER,
        )
    return "Energia e calor", "fonte e temperatura ok", ui.GREEN


def health_rows(
    health: Health, gauges: procfs.Gauges | None
) -> list[tuple[str, str, tuple]]:
    rows = [power_row(health.throttle)]
    if health.services is None:
        rows.append(("Serviços", "sem leitura", ui.DIM))
    else:
        down = [
            f"{label} {health.services.get(name, '?')}"
            for name, label in SERVICES
            if health.services.get(name) != "active"
        ]
        rows.append(
            (
                "Serviços",
                (
                    "parado: " + ", ".join(down)
                    if down
                    else ", ".join(label for _, label in SERVICES) + " rodando"
                ),
                ui.RED if down else ui.GREEN,
            )
        )
    if health.network is None:
        rows.append(("Internet", "sem leitura", ui.DIM))
    elif health.network.ping_ms is None:
        rows.append(
            ("Internet", f"fora do ar · IP {health.network.address or '?'}", ui.RED)
        )
    else:
        rows.append(
            (
                "Internet",
                f"no ar, {health.network.ping_ms:.0f} ms · IP {health.network.address}",
                ui.GREEN,
            )
        )
    if health.updates is not None:
        rows.append(
            (
                "Atualizações",
                f"{health.updates} pendentes" if health.updates else "em dia",
                ui.AMBER if health.updates else ui.GREEN,
            )
        )
    if health.journal_errors is not None:
        rows.append(
            (
                "Erros na última hora",
                (
                    f"{health.journal_errors} no journal"
                    if health.journal_errors
                    else "nenhum"
                ),
                ui.AMBER if health.journal_errors else ui.GREEN,
            )
        )
    if health.failed is not None:
        rows.append(
            (
                "Serviços com falha",
                ", ".join(health.failed) or "nenhum",
                ui.RED if health.failed else ui.GREEN,
            )
        )
    if gauges is not None:
        rows.append(("Conexões abertas", str(gauges.established), ui.TEXT))
        rows.append(
            (
                "Portas TCP",
                " · ".join(map(procfs.port_label, gauges.listening)) or "nenhuma",
                ui.TEXT,
            )
        )
        rows.append(
            (
                "Portas UDP",
                " · ".join(map(procfs.port_label, gauges.udp_ports)) or "nenhuma",
                ui.TEXT,
            )
        )
    return rows


def system_rows(
    system: procfs.System | None, now: datetime
) -> list[tuple[str, str, tuple]]:
    if system is None:
        return [("Sistema", "sem leitura", ui.DIM)]
    rows = [
        ("Kernel", system.kernel, ui.TEXT),
        ("Sistema", system.distro, ui.TEXT),
        (
            "Ligada desde",
            f"{net.clock(system.booted, now)} (há {duration(now - system.booted)})",
            ui.TEXT,
        ),
    ]
    devices = system.usb or ("nenhum",)
    rows += [
        ("USB" if index == 0 else "", device, ui.TEXT)
        for index, device in enumerate(devices)
    ]
    return rows


class PrinterPanel(ui.Page):
    """The printer block: the monitor's live reading every second, the history file and the
    CUPS queue every 30 s."""

    name = "Impressora"
    live = True

    def __init__(self, history=load_monitor_history, fetch=net.fetch_bytes) -> None:
        self.monitor_history = history()
        self.fetch = fetch
        self.status = PrinterStatus({}, "", False)
        self.reading: PrinterReading | None = None
        self.queue: tuple[str, ...] = ()
        self.jobs: tuple[Job, ...] = ()
        self.printed: tuple[tuple[date, int], ...] = ()
        self.forecast = None
        self.saved_at: datetime | None = None
        self.slow_read_at: datetime | None = None
        self.status_errors: list[str] = []
        self.slow_errors: list[str] = []

    def refresh(self, now: datetime) -> None:
        try:
            self._read_status(now)
            self.status_errors = []
        except SourceError as err:
            self.status_errors = [f"Monitor da impressora: {err}"]
        if self.slow_read_at is not None and now - self.slow_read_at < SLOW_REFRESH:
            return
        errors = []
        for label, step in (
            ("Impressora", self._read_history),
            ("Fila", self._read_queue),
        ):
            try:
                step()
            except SourceError as err:
                errors.append(f"{label}: {err}")
        self.slow_errors, self.slow_read_at = errors, now

    def _read_status(self, now: datetime) -> None:
        save = self.saved_at is None or now - self.saved_at >= SAVE_EVERY
        self.status = system_data.printer_status(
            STATUS_URL, STATUS_SAVED, save, self.fetch
        )
        if save and self.status.payload and not self.status.from_disk:
            self.saved_at = now

    def _read_history(self) -> None:
        self.reading = system_data.last_printer_reading(HISTORY)
        if self.monitor_history is not None and HISTORY.exists():
            entries = self.monitor_history.load(HISTORY)
            self.printed = system_data.daily_pages(entries)
            self.forecast = self.monitor_history.forecast(entries)

    def _read_queue(self) -> None:
        self.queue = system_data.print_queue(PRINTER_SCRATCH)
        self.jobs = system_data.completed_jobs(PRINTER_SCRATCH)

    def problems(self) -> list[str]:
        return self.status_errors + self.slow_errors

    def live_key(self) -> object:
        status = self.status
        return (
            status.payload.get("reads"),
            status.feed_error,
            status.from_disk,
            self.reading,
            self.queue,
            self.jobs,
            self.printed,
        )

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        left, top, right, bottom = box
        height, middle = bottom - top, (left + right) // 2
        verdict = top + round(height * 0.14)
        toner = verdict + ui.GUTTER + round(height * 0.30)
        history = toner + ui.GUTTER + round(height * 0.21)
        draw_verdict(draw, (left, top, right, verdict), self.status, now)
        draw_toner(
            draw,
            (left, verdict + ui.GUTTER, right, toner),
            self.reading,
            self.forecast,
            now,
        )
        lower = toner + ui.GUTTER
        draw_pages_per_day(
            draw,
            (left, lower, middle - ui.GUTTER // 2, history),
            self.printed,
            now.astimezone().date(),
        )
        draw_jobs(
            draw,
            (middle + ui.GUTTER // 2, lower, right, history),
            self.queue,
            self.jobs,
            now,
        )
        lowest = history + ui.GUTTER
        draw_events(
            draw, (left, lowest, middle - ui.GUTTER // 2, bottom), self.status.payload
        )
        draw_details(
            draw, (middle + ui.GUTTER // 2, lowest, right, bottom), self.status.payload
        )


class PiPanel(ui.Page):
    """The Pi block, redrawn alone every second from /proc; the slower checks (power, services,
    internet, journal, updates, USB) run on their own clocks inside it."""

    name = "Raspberry Pi"
    live = True

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.stats: PiStats | None = None
        self.gauges: procfs.Gauges | None = None
        self.counters: procfs.Counters | None = None
        self.rates: procfs.Rates | None = None
        self.health = Health()
        self.temperatures: tuple[float, ...] = ()
        self.loads: tuple[float, ...] = ()
        self.downloads: tuple[float, ...] = ()
        self.chart_at: datetime | None = None
        self.errors: list[str] = []
        self.health_read_at: datetime | None = None
        self.updates_read_at: datetime | None = None

    def refresh(self, now: datetime) -> None:
        counters = procfs.sample(self.clock())
        if self.counters is not None:
            live = procfs.rates(self.counters, counters)
            self.rates = replace(live, top=procfs.named(live.top))
            self.downloads = (*self.downloads, live.down_bytes)[-RATE_SAMPLES:]
        self.counters = counters
        self.gauges = procfs.gauges()
        self.stats = system_data.pi_stats()
        if self.chart_at is None or now - self.chart_at >= CHART_STEP:
            self.temperatures = (*self.temperatures, self.stats.temperature)[
                -SAMPLES_KEPT:
            ]
            self.loads = (*self.loads, self.stats.load / self.stats.cpus)[
                -SAMPLES_KEPT:
            ]
            self.chart_at = now
        if self.health_read_at is None or now - self.health_read_at >= HEALTH_REFRESH:
            self._read_health(now)
            self.health_read_at = now

    def _read_health(self, now: datetime) -> None:
        """Each check on its own: one that fails keeps its last value and shows up as a problem."""
        errors, old = [], self.health

        def attempt(label: str, read, fallback):
            try:
                return read()
            except SourceError as err:
                errors.append(f"{label}: {err}")
                return fallback

        updates = old.updates
        if (
            self.updates_read_at is None
            or now - self.updates_read_at >= UPDATES_REFRESH
        ):
            updates = attempt(
                "Atualizações",
                lambda: system_data.pending_updates(PI_SCRATCH),
                old.updates,
            )
            self.updates_read_at = now
        self.health = Health(
            attempt(
                "Energia", lambda: system_data.throttle_bits(PI_SCRATCH), old.throttle
            ),
            attempt(
                "Serviços",
                lambda: system_data.services_state(
                    tuple(n for n, _ in SERVICES), PI_SCRATCH
                ),
                old.services,
            ),
            system_data.network(PI_SCRATCH),
            updates,
            attempt(
                "Journal",
                lambda: system_data.journal_errors(PI_SCRATCH),
                old.journal_errors,
            ),
            attempt(
                "Serviços com falha",
                lambda: system_data.failed_units(PI_SCRATCH),
                old.failed,
            ),
            attempt("Tensão", lambda: system_data.core_volts(PI_SCRATCH), old.volts),
            procfs.system(now),
        )
        self.errors = errors

    def problems(self) -> list[str]:
        return self.errors

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        left, top, right, bottom = box
        height, middle = bottom - top, left + round((right - left) * 0.58)
        processor = top + round(height * 0.26)
        resources = processor + ui.GUTTER + round(height * 0.19)
        processes = resources + ui.GUTTER + round(height * 0.26)
        draw_processor(
            draw,
            (left, top, right, processor),
            self.stats,
            self.gauges,
            self.rates,
            self.health,
            (self.temperatures, self.loads),
        )
        draw_resources(
            draw,
            (left, processor + ui.GUTTER, right, resources),
            self.gauges,
            self.stats,
            self.rates,
            self.downloads,
        )
        draw_processes(
            draw, (left, resources + ui.GUTTER, right, processes), self.rates
        )
        lowest = processes + ui.GUTTER
        health_box = (left, lowest, middle - ui.GUTTER // 2, bottom)
        ui.card(draw, health_box, "Saúde da Pi")
        draw_rows(draw, health_box, health_rows(self.health, self.gauges), first=58)
        system_box = (middle + ui.GUTTER // 2, lowest, right, bottom)
        ui.card(draw, system_box, "Sistema")
        draw_rows(draw, system_box, system_rows(self.health.system, now), first=58)


def sample_payload() -> dict:
    """The keys of the monitor's viewmodel.build_payload this page reads, with real-looking values."""
    return {
        "read_at": "2026-10-23T11:59:58",
        "reads": 4210,
        "read_ms": 41,
        "severity": 1,
        "verdict": "▲ ATENÇÃO",
        "status_rows": {
            "Estado": "economia de energia",
            "Carrossel": "parado no cartucho 2",
            "Fila CUPS": "ativa, ociosa",
        },
        "port_badges": [
            {"label": "USB online", "ok": True},
            {"label": "sem erro na porta", "ok": True},
            {"label": "papel ok", "ok": True},
            {"label": "fila ativa", "ok": True},
        ],
        "events": [
            {
                "seq": 30,
                "code": "10.1001",
                "page": "1310",
                "hint": "falha no chip do cartucho Ciano (provável)",
            },
            {
                "seq": 31,
                "code": "59.F0",
                "page": "1328",
                "hint": "sem descrição conhecida",
            },
        ],
        "changes": [
            {"at": "11:59:40", "text": "Estado: pronta → economia de energia"},
            {"at": "11:41:02", "text": "Páginas: 1327 → 1328"},
        ],
        "usb_bus": {
            "Velocidade": "high (480 Mb/s) · USB 2.00",
            "Energia": "–",
            "Status 1284": "0x18 = 0001 1000 → online, sem erro, com papel",
        },
        "controller": {
            "Firmware": "08/11/2012 · FWVER 20121108",
            "Latência": "média 40 ms · pior 60 ms",
        },
        "identity": {
            "Modelo": "HP LaserJet Professional CP1025nw",
            "Serial": "TESTSERIAL01",
            "Memória livre": "6 de 8 MB",
        },
        "settings": {"Desliga sozinha após": "15 min"},
        "errors": [],
    }


def selftest() -> Image.Image:
    now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    module = load_monitor_history()
    page = PrinterPanel(lambda: module)
    pi = PiPanel()
    page.reading = PrinterReading(
        now - timedelta(hours=16),
        1328,
        455,
        "inPowerSave",
        ("incompatibleConsumable",),
        {
            "Black": {"percent": 100, "pages_remaining": 1250},
            "Cyan": {"percent": 50},
            "Magenta": {"percent": 40, "pages_remaining": 300},
            "OPC": {"percent": 100},
        },
    )
    page.status = PrinterStatus(
        sample_payload(), "09:48:56 leitura falhou: printer not found on USB", True
    )
    pi.stats = PiStats(49.4, 0.42, 4, 0.31, 904, 0.12, timedelta(hours=1, minutes=5))
    pi.gauges = procfs.Gauges(
        procfs.Memory(926764, 349844, 5788, 265596, 926716, 920400),
        (0.09, 0.17, 0.21),
        (800, 600, 1200),
        -26.0,
        1,
        (22, 631, 8025),
        (5353,),
    )
    pi.rates = procfs.Rates(
        (0.05, 0.62, 0.0, 0.97),
        1234,
        567,
        0,
        12_800,
        0,
        3,
        2048,
        900,
        (
            procfs.Busy(812, "python3 screen.py", 0.21, 197_412),
            procfs.Busy(640, "python3 web_dashboard.py", 0.07, 29_664),
        ),
    )
    pi.temperatures = tuple(48 + i % 5 for i in range(100))
    pi.loads = tuple((i % 10) / 40 for i in range(100))
    pi.downloads = tuple(float(i % 7) * 900 for i in range(120))
    page.printed = ((date(2026, 10, 20), 12), (date(2026, 10, 22), 8))
    page.jobs = (Job("HP_CP1025-16", "iklo", now - timedelta(hours=16)),)
    pi.health = Health(
        0x50000,
        {"cups": "active", "cp1025-dashboard": "failed", "dashboard": "active"},
        Network("192.168.0.10", 31.2),
        9,
        2,
        ("cp1025-dashboard.service",),
        1.2,
        procfs.System(
            "6.18.50+rpt-rpi-v8",
            "Debian GNU/Linux 13 (trixie)",
            now - timedelta(hours=6),
            ("hub USB interno", "placa de rede Ethernet"),
        ),
    )
    assert (
        readable_error("09:48:56 leitura falhou: printer not found on USB")
        == "impressora desligada ou fora do USB"
    )
    assert readable_error("outro erro") == "outro erro"
    assert (
        thousands(1328) == "1.328" and duration(timedelta(days=2, hours=3)) == "2d 3h"
    )
    assert (
        speed(900) == "900 B/s"
        and speed(12_800) == "12 KB/s"
        and speed(3_500_000) == "3,3 MB/s"
    )
    assert temperature_color(59.9) == ui.GREEN and temperature_color(60) == ui.AMBER
    assert temperature_color(80) == ui.RED
    details = dict(
        (label, value) for label, value, _ in printer_details(sample_payload())
    )
    assert "Energia" not in details and details["Erros"] == "nenhum", (
        "fields the printer left empty drop"
    )
    assert details["Modelo"] == "HP LaserJet Professional CP1025nw"
    state, color = printer_state(page.status, now)
    assert color == ui.RED and "desligada" in state and "dados de" in state, state
    state, color = printer_state(PrinterStatus(sample_payload(), "", False), now)
    assert color == ui.GREEN and "4.210" in state
    assert printer_state(PrinterStatus({}, "", False), now)[1] == ui.DIM
    services = dict(
        (label, (value, color))
        for label, value, color in health_rows(pi.health, pi.gauges)
    )
    assert services["Serviços"] == ("parado: monitor failed", ui.RED)
    assert (
        services["Serviços com falha"][1] == ui.RED
        and services["Portas TCP"][0] == "22 ssh · 631 cups · 8025 monitor"
    )
    calm = dict(
        (label, value)
        for label, value, _ in health_rows(
            Health(
                services={n: "active" for n, _ in SERVICES}, failed=(), journal_errors=0
            ),
            None,
        )
    )
    assert (
        calm["Serviços"] == "CUPS, monitor, painel rodando"
        and calm["Serviços com falha"] == "nenhum"
    )
    assert [label for label, _, _ in system_rows(pi.health.system, now)][-2:] == [
        "USB",
        "",
    ]
    if (
        module is not None
    ):  # on the Pi, where the monitor lives: its forecast drives the toner dates
        entries = [
            {"read_at": "2026-10-01T10:00:00", "total_pages": 1000, "color_pages": 300},
            {"read_at": "2026-10-11T10:00:00", "total_pages": 1100, "color_pages": 340},
        ]
        page.forecast = module.forecast(entries)
        assert (
            toner_end(page.forecast, "Black", {"pages_remaining": 110}, now)
            == "acaba ~03/11/26"
        )
        assert (
            toner_end(page.forecast, "Cyan", {"pages_remaining": 40}, now)
            == "acaba ~02/11/26"
        ), "color toner wears only on color pages"
        assert toner_end(page.forecast, "Cyan", {}, now) == ""
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b'{"loading": true, "feed_error": "x"}'

    fresh = PrinterPanel(lambda: None, fetch)
    fresh.slow_read_at = (
        now  # skip the history file and lpstat: only the monitor read is under test
    )
    fresh.refresh(now)
    assert (
        calls == [STATUS_URL]
        and fresh.status.feed_error == "x"
        and fresh.problems() == []
    )
    image = Image.new("RGB", (1896, 1012), ui.BACKGROUND)
    page.draw(ImageDraw.Draw(image), (0, 0, 942, 1012), now)
    pi.draw(ImageDraw.Draw(image), (954, 0, 1896, 1012), now)
    PrinterPanel(lambda: None).draw(
        ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 942, 992), now
    )
    PiPanel().draw(ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 942, 992), now)
    assert PiPanel().live is True and pi.live is True, (
        "the live flag is not overwritten by the samples"
    )
    print("page_system selftest ok")
    return image

"""Snapshot history of printer readings and toner-replacement forecast."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

HISTORY_FILE = Path(__file__).with_name("history.jsonl")
SPARK_LEVELS = "▁▂▃▄▅▆▇█"
# Forecasting needs a real usage window; fewer pages or days than this is noise.
MIN_FORECAST_DAYS = 1
MIN_FORECAST_PAGES = 1
COLOR_TONERS = {"Cyan", "Magenta", "Yellow"}


def snapshot(report):
    return {
        "read_at": report.read_at.isoformat(timespec="seconds"),
        "total_pages": report.counters.get("total"),
        "color_pages": report.counters.get("color"),
        "status": report.status_category,
        "alerts": [a[1] for a in report.alerts],
        "supplies": {
            c.color: {
                "percent": c.percent,
                "pages_remaining": c.pages_remaining,
                "state": c.state,
            }
            for c in report.consumables
        },
    }


def comparable(entry):
    return {k: v for k, v in entry.items() if k != "read_at"}


def load(path=HISTORY_FILE):
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def record(report, path=HISTORY_FILE):
    """Append a snapshot only when something measurable changed; return the full history."""
    entries = load(path)
    current = snapshot(report)
    if not entries or comparable(entries[-1]) != comparable(current):
        with path.open("a") as handle:
            handle.write(json.dumps(current) + "\n")
        entries.append(current)
    return entries


def sparkline(values):
    known = [v for v in values if v is not None]
    if not known:
        return ""
    return "".join(
        (
            "·"
            if v is None
            else SPARK_LEVELS[min(len(SPARK_LEVELS) - 1, v * len(SPARK_LEVELS) // 101)]
        )
        for v in values
    )


def level_series(entries, color):
    return [e["supplies"].get(color, {}).get("percent") for e in entries]


@dataclass
class Forecast:
    pages_per_day: float | None
    color_pages_per_day: float | None
    window_days: float
    window_pages: int

    def rate_for(self, consumable):
        """Black toner and the drum wear on every page; C/M/Y only on color pages."""
        if consumable.color in COLOR_TONERS:
            return self.color_pages_per_day
        return self.pages_per_day

    def days_left(self, consumable):
        rate = self.rate_for(consumable)
        if not rate or not consumable.pages_remaining:
            return None
        return consumable.pages_remaining / rate

    def empty_date(self, consumable, now):
        days = self.days_left(consumable)
        return None if days is None else (now + timedelta(days=days)).date()


def forecast(entries):
    usable = [e for e in entries if e.get("total_pages") is not None]
    if len(usable) < 2:
        return Forecast(None, None, 0, 0)
    first, last = usable[0], usable[-1]
    days = (
        datetime.fromisoformat(last["read_at"])
        - datetime.fromisoformat(first["read_at"])
    ).total_seconds() / 86400
    pages = last["total_pages"] - first["total_pages"]
    if days < MIN_FORECAST_DAYS or pages < MIN_FORECAST_PAGES:
        return Forecast(None, None, days, pages)
    color_pages = (last.get("color_pages") or 0) - (first.get("color_pages") or 0)
    return Forecast(pages / days, color_pages / days, days, pages)

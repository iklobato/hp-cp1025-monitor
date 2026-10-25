"""Data for the Pi dashboard: quotes (Yahoo).

No tokens. Yahoo's chart endpoint is public but unofficial (no key, no SLA).
"""

import json
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from net import CACHE_DIR, SourceError, br_percent, fetch_bytes


CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}"


@dataclass(frozen=True)
class Period:
    label: str
    range: str
    interval: str
    max_age: timedelta


PERIODS = (
    Period("1d", "1d", "5m", timedelta(minutes=1)),
    Period("1w", "5d", "30m", timedelta(minutes=15)),
    Period("1m", "1mo", "1h", timedelta(hours=1)),
)
# Interest rates also get a year (asked for on 2026-10-23); market quotes keep the three above.
YEAR = Period("1y", "1y", "1d", timedelta(hours=6))
RATE_PERIODS = (*PERIODS, YEAR)


@dataclass(frozen=True)
class Series:
    """One Yahoo chart response: the price now, the close before the range, the path."""

    price: float
    previous_close: float
    closes: tuple[float, ...]
    traded_at: datetime
    stale: bool = False
    session: tuple[datetime, datetime] | None = None

    @property
    def change(self) -> float:
        return self.price / self.previous_close - 1

    def state(self, now: datetime) -> str:
        """Regular session status: "aberto", "abre HH:MM" (later today) or "fechado"."""
        if self.session is None:
            return "sem horario"
        start, end = self.session
        if start <= now < end:
            return "aberto"
        if now < start:
            return f"abre {start.astimezone():%H:%M}"
        return "fechado"

    @property
    def path(self) -> tuple[float, ...]:
        """Starts at the previous close so the chart shows the whole move of the period."""
        return (self.previous_close, *self.closes)


def series_from_chart(chart: dict, stale: bool = False) -> Series:
    try:
        result = chart["chart"]["result"][0]
        meta = result["meta"]
        # A stock with no trade yet today comes back with an empty quote ({}), not an error.
        candles = result["indicators"]["quote"][0].get("close") or []
        closes = tuple(c for c in candles if c is not None)
        regular = meta.get("currentTradingPeriod", {}).get("regular")
        session = (
            (
                datetime.fromtimestamp(regular["start"], timezone.utc),
                datetime.fromtimestamp(regular["end"], timezone.utc),
            )
            if regular
            else None
        )
        series = Series(
            float(meta["regularMarketPrice"]),
            float(meta["chartPreviousClose"]),
            closes,
            datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc),
            stale,
            session,
        )
    except (KeyError, IndexError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err
    if series.previous_close == 0:
        raise SourceError("formato dos dados mudou")
    return series


def fetch_series(
    symbol: str,
    period: Period,
    now: datetime,
    cache_dir: Path = CACHE_DIR,
    fetch: Callable[[str], bytes] = fetch_bytes,
) -> Series:
    """Cached per symbol and period; a failed refresh falls back to the old copy, marked stale."""
    path = cache_dir / f"{urllib.parse.quote(symbol, safe='')}_{period.range}.json"
    cached_at = (
        datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if path.exists()
        else None
    )
    if cached_at and now - cached_at < period.max_age:
        return series_from_chart(json.loads(path.read_text()))
    url = CHART_URL.format(
        symbol=urllib.parse.quote(symbol), range=period.range, interval=period.interval
    )
    try:
        body = fetch(url)
        series = series_from_chart(json.loads(body))
    except (SourceError, ValueError):
        if cached_at is None:
            raise SourceError("sem dados") from None
        return series_from_chart(json.loads(path.read_text()), stale=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=cache_dir, delete=False) as handle:
        handle.write(body)
    Path(handle.name).replace(path)
    return series


def selftest() -> None:
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)

    chart = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 204120.12,
                        "chartPreviousClose": 206495.23,
                        "regularMarketTime": int(
                            datetime(
                                2026, 10, 22, 20, 16, tzinfo=timezone.utc
                            ).timestamp()
                        ),
                    },
                    "indicators": {
                        "quote": [{"close": [206000.0, None, 205000.0, 204120.12]}]
                    },
                }
            ]
        }
    }
    series = series_from_chart(chart)
    assert series.closes == (206000.0, 205000.0, 204120.12)
    quiet = {
        "chart": {
            "result": [{**chart["chart"]["result"][0], "indicators": {"quote": [{}]}}]
        }
    }
    assert series_from_chart(quiet).closes == () and series_from_chart(quiet).path == (
        206495.23,
    )
    assert series.path[0] == 206495.23
    assert br_percent(series.change) == "-1,15%"
    assert series.session is None and series.state(now) == "sem horario"
    opened = {
        **chart["chart"]["result"][0]["meta"],
        "currentTradingPeriod": {
            "regular": {
                "start": int(
                    datetime(2026, 10, 23, 13, 0, tzinfo=timezone.utc).timestamp()
                ),
                "end": int(
                    datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc).timestamp()
                ),
            }
        },
    }
    b3 = series_from_chart(
        {"chart": {"result": [{**chart["chart"]["result"][0], "meta": opened}]}}
    )
    assert (
        b3.state(now).startswith("abre ")
        and b3.state(now) == f"abre {b3.session[0].astimezone():%H:%M}"
    )
    assert b3.state(now + timedelta(hours=2)) == "aberto"
    assert b3.state(now + timedelta(hours=9)) == "fechado"
    try:
        series_from_chart({"chart": {"result": None}})
    except SourceError:
        pass
    else:
        raise AssertionError("bad payload must raise SourceError")

    with tempfile.TemporaryDirectory() as tmp:
        cache, calls = Path(tmp), []
        body = json.dumps(chart).encode()

        def ok(url: str) -> bytes:
            calls.append(url)
            return body

        def down(url: str) -> bytes:
            raise SourceError("sem conexao")

        live = datetime.now(timezone.utc)
        fetch_series("^BVSP", PERIODS[1], live, cache, ok)
        fetch_series("^BVSP", PERIODS[1], live, cache, ok)
        assert len(calls) == 1 and "range=5d" in calls[0], (
            "second read comes from cache"
        )
        later = live + PERIODS[1].max_age * 2
        assert fetch_series("^BVSP", PERIODS[1], later, cache, down).stale
        try:
            fetch_series("BRL=X", PERIODS[0], later, cache, down)
        except SourceError:
            pass
        else:
            raise AssertionError("no cache and no network must fail")
    print("market_data selftest ok")


if __name__ == "__main__":
    selftest()

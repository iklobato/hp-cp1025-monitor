"""Data for the Pi dashboard: news (RSS/Atom), quotes (Yahoo), weather (Open-Meteo),
Brazilian rates (Banco Central SGS), and the printer and Pi status on this machine.

No tokens. Yahoo's chart endpoint is public but unofficial (no key, no SLA).
"""

import csv
import gzip
import hashlib
import io
import html
import json
import math
import re
import os
import shlex
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError

HTTP_TIMEOUT_SECONDS = 15
# Banco Central's SGS answered one of three identical requests in 12.1 s (measured 2026-10-23).
SGS_TIMEOUT_SECONDS = 40
USER_AGENT = "Mozilla/5.0 (pi-dashboard)"
GZIP_MAGIC = b"\x1f\x8b"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}"
CACHE_DIR = Path.home() / ".cache/pi-dashboard"
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=America%2FSao_Paulo"
    "&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m,"
    "wind_gusts_10m,wind_direction_10m,pressure_msl,cloud_cover,visibility,dew_point_2m"
    "&hourly=temperature_2m,precipitation_probability,pressure_msl&past_days=7&forecast_days=16"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
    "precipitation_sum,sunrise,sunset,uv_index_max"
)
AIR_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}"
    "&timezone=America%2FSao_Paulo&current=us_aqi,pm2_5,pm10,ozone&hourly=us_aqi&forecast_days=2"
)
INMET_URL = "https://apiprevmet3.inmet.gov.br/avisos/ativos"
RADAR_INDEX_URL = "https://api.rainviewer.com/public/weather-maps.json"
# RainViewer's free tiles stop at zoom 7: zoom 8 returns a "Zoom Level Not Supported" picture (checked 2026-10-23).
RADAR_TILE_URL = "{host}{path}/256/{z}/{x}/{y}/2/1_1.png"
# Esri dark canvas (no key; CARTO started stamping "API KEY REQUIRED" on its tiles, seen 2026-10-23).
# Note the {y}/{x} order in Esri's tile paths.
BASEMAP_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
LABELS_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}"
SIDRA_URL = "https://apisidra.ibge.gov.br/values/{path}"
TESOURO_URL = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/"
    "resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv"
)
# The Tesouro file holds every day since 2002 (14.6 MB on 2026-10-23); it comes over slowly.
TESOURO_TIMEOUT_SECONDS = 120
LOTTERY_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/{game}"
ANP_PAGE_URL = (
    "https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/precos/"
    "levantamento-de-precos-de-combustiveis-ultimas-semanas-pesquisadas"
)
ANP_TIMEOUT_SECONDS = 90
# Big files kept on disk so a restart does not download them again: every start fetched the
# 14.6 MB Tesouro file, and the first try after a restart sometimes failed to connect.
DOWNLOAD_DIR = CACHE_DIR / "downloads"
TESOURO_KEEP = timedelta(hours=12)  # one new price row per business day
ANP_KEEP = timedelta(days=7)  # a published weekly survey file never changes
DOWNLOAD_PRUNE = timedelta(days=30)
SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados?formato=json&dataInicial={start}&dataFinal={end}"


class SourceError(Exception):
    """A feed or quote could not be read; the screen shows it instead of crashing."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# Every fetch here is a read (GET), so a second try is safe; only failures that can pass are retried.
FETCH_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 3


def decoded(body: bytes) -> bytes:
    """g1 sometimes answers gzip-compressed without Content-Encoding."""
    return gzip.decompress(body) if body.startswith(GZIP_MAGIC) else body


def fetch_once(url: str, timeout: float, opener=urllib.request.urlopen) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise SourceError(
            f"endereço inválido: {url[:40]!r}"
        )  # g1 pages declare og:image "None"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout) as response:
            return decoded(response.read())
    except urllib.error.HTTPError as err:
        if err.code == 429:
            raise SourceError("HTTP 429 (muitos pedidos)", retryable=True) from err
        if err.code >= 500:
            raise SourceError(
                f"HTTP {err.code} (erro no servidor deles)", retryable=True
            ) from err
        raise SourceError(f"HTTP {err.code}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        timed_out = isinstance(err, TimeoutError) or isinstance(
            getattr(err, "reason", None), TimeoutError
        )
        raise SourceError(
            "demorou demais" if timed_out else "sem conexao", retryable=True
        ) from err


def fetch_bytes(
    url: str,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    opener=urllib.request.urlopen,
    sleep=time.sleep,
) -> bytes:
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return fetch_once(url, timeout, opener)
        except SourceError as err:
            if not err.retryable or attempt == FETCH_ATTEMPTS:
                raise
            sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable")


def parse_json(body: bytes):
    """JSON, or a SourceError that says what came instead (servers under load send HTML error pages)."""
    try:
        return json.loads(body)
    except ValueError as err:
        if not body.strip():
            raise SourceError("resposta vazia", retryable=True) from err
        if body.lstrip()[:1] == b"<":
            raise SourceError(
                "veio página de erro em vez de dados", retryable=True
            ) from err
        raise SourceError("dados em formato inesperado") from err


def short_age(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    hours = minutes // 60
    if hours >= 24:
        return f"{hours // 24}d"
    return f"{hours}h" if hours else f"{minutes}m"


def clock(moment: datetime, now: datetime) -> str:
    """Local time of day, with the date in front when it is not today."""
    local = moment.astimezone()
    if local.date() == now.astimezone().date():
        return f"{local:%H:%M}"
    return f"{local:%d/%m %H:%M}"


def br_number(value: float) -> str:
    """Brazilian notation (204.120 / 5,0533), with fewer decimals as the number grows."""
    decimals = 0 if value >= 10_000 else 2 if value >= 10 else 4
    us = f"{value:,.{decimals}f}"
    return us.replace(",", "_").replace(".", ",").replace("_", ".")


def br_percent(change: float) -> str:
    return f"{change * 100:+.2f}%".replace(".", ",")


@dataclass(frozen=True)
class Feed:
    """One news source: a letter for its badge, a label, a URL."""

    letter: str
    label: str
    url: str


@dataclass(frozen=True)
class Story:
    published: datetime
    title: str
    feed: Feed
    summary: str = ""
    link: str = ""
    image: str = ""  # picture URL the feed itself carries; empty when it has none

    def mentions(self, words: tuple[str, ...]) -> bool:
        """Whole-word match, ignoring case and accents, in the title or the summary."""
        text = f" {title_key(self.title)} {title_key(self.summary)} "
        return any(f" {title_key(word)} " in text for word in words)


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, *names: str) -> str:
    for child in element:
        if _local_name(child) in names and child.text:
            return child.text.strip()
    return ""


def parse_published(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        published = parsedate_to_datetime(stamp)
    except (TypeError, ValueError):
        pass
    else:
        # RFC 2822 "-0000" (used by g1) parses as naive but means UTC.
        return published if published.tzinfo else published.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


TAG = re.compile(r"<[^>]+>")


def plain_text(fragment: str) -> str:
    """Feed summaries carry HTML (images, paragraphs); keep the words only."""
    return " ".join(html.unescape(TAG.sub(" ", html.unescape(fragment))).split())


IMG_SRC = re.compile(r"""<img[^>]+src=["']([^"']+)""")


def story_link(element: ET.Element) -> str:
    """RSS <link>text</link>, or Atom <link href=... rel="alternate">."""
    for child in element:
        if _local_name(child) != "link":
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        if child.get("href") and child.get("rel", "alternate") == "alternate":
            return child.get("href")
    return ""


def story_image(element: ET.Element) -> str:
    """media:content / media:thumbnail / an image enclosure, else the first <img> in the body."""
    for child in element:
        name = _local_name(child)
        if (
            name in ("content", "thumbnail")
            and child.get("url")
            and "image" in child.get("medium", "image")
        ):
            return child.get("url")
        if (
            name == "enclosure"
            and child.get("url")
            and child.get("type", "image/").startswith("image/")
        ):
            return child.get("url")
    for child in element:
        if (
            _local_name(child) in ("description", "encoded", "summary", "content")
            and child.text
        ):
            match = IMG_SRC.search(html.unescape(child.text))
            if match:
                return match.group(1)
    return ""


OG_IMAGE = (
    re.compile(
        r"""<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)["'][^>]+content=["']([^"']+)"""
    ),
    re.compile(
        r"""<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["'](?:og:image|twitter:image)["']"""
    ),
)


def og_image(page: str) -> str:
    """The picture a site declares for sharing (og:image or twitter:image), if any."""
    for pattern in OG_IMAGE:
        match = pattern.search(page)
        if match and match.group(1).startswith(("http://", "https://")):
            return html.unescape(match.group(1))
    return ""


THUMB_DIR = CACHE_DIR / "thumbs"
THUMB_KEEP = timedelta(days=3)


def thumbnail(
    url: str,
    size: tuple[int, int],
    fetch: Callable[[str], bytes] = fetch_bytes,
    folder: Path = THUMB_DIR,
):
    """The picture cut to fill `size` (center crop), cached on disk by URL so each loads once."""
    path = folder / f"{hashlib.sha1(f'{url}|{size}'.encode()).hexdigest()}.jpg"
    if path.exists():
        return Image.open(path).convert("RGB")
    try:
        picture = Image.open(io.BytesIO(fetch(url))).convert("RGB")
    except (UnidentifiedImageError, OSError) as err:
        raise SourceError("imagem ilegível") from err
    picture = ImageOps.fit(picture, size, Image.Resampling.LANCZOS)
    folder.mkdir(parents=True, exist_ok=True)
    picture.save(path, "JPEG", quality=85)
    return picture


def prune_thumbnails(now: datetime, folder: Path = THUMB_DIR) -> int:
    """Delete cached pictures older than THUMB_KEEP; returns how many went."""
    if not folder.exists():
        return 0
    cutoff = (now - THUMB_KEEP).timestamp()
    old = [path for path in folder.glob("*.jpg") if path.stat().st_mtime < cutoff]
    for path in old:
        path.unlink(missing_ok=True)
    return len(old)


def stories_from_feed(xml: bytes, feed: Feed, limit: int) -> list[Story]:
    """RSS <item> and Atom <entry> both carry a title and a publish time."""
    stories = []
    for element in ET.fromstring(xml).iter():
        if _local_name(element) not in ("item", "entry"):
            continue
        published = parse_published(
            _child_text(element, "pubDate", "published", "updated")
        )
        title = " ".join(html.unescape(_child_text(element, "title")).split())
        if title and published:
            stories.append(
                Story(
                    published,
                    title,
                    feed,
                    plain_text(_child_text(element, "description", "summary")),
                    story_link(element),
                    story_image(element),
                )
            )
    stories.sort(key=lambda story: story.published, reverse=True)
    return stories[:limit]


def fetch_stories(feed: Feed, limit: int) -> list[Story]:
    try:
        return stories_from_feed(fetch_bytes(feed.url), feed, limit)
    except (ET.ParseError, EOFError, gzip.BadGzipFile) as err:
        raise SourceError("feed invalido") from err


def title_key(title: str) -> str:
    """Accents, case and punctuation removed: g1 and g1 Economia publish the same headline."""
    plain = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    )
    return " ".join("".join(c if c.isalnum() else " " for c in plain).split())


def unique_stories(groups: list[list[Story]]) -> list[list[Story]]:
    """Each headline once, in the first group (in the given order) that has it."""
    seen: set[str] = set()
    result = []
    for group in groups:
        kept = []
        for story in sorted(group, key=lambda s: s.published, reverse=True):
            key = title_key(story.title)
            if key not in seen:
                seen.add(key)
                kept.append(story)
        result.append(kept)
    return result


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


@dataclass(frozen=True)
class Hour:
    time: datetime
    temperature: float
    rain_chance: float


@dataclass(frozen=True)
class Day:
    day: date
    code: int
    high: float
    low: float
    rain_chance: float
    rain_mm: float
    sunrise: datetime
    sunset: datetime
    uv: float

    @property
    def length(self) -> timedelta:
        return self.sunset - self.sunrise


@dataclass(frozen=True)
class Weather:
    read_at: datetime
    temperature: float
    feels_like: float
    humidity: float
    rain_now: float
    code: int
    wind: float
    gusts: float
    wind_from: float  # degrees, where the wind comes from
    pressure: float  # hPa at sea level
    pressure_3h_change: float
    clouds: float  # %
    visibility_km: float
    dew_point: float
    hours: tuple[Hour, ...]
    past: tuple[Day, ...]  # the days before today, oldest first
    days: tuple[Day, ...]  # today and the days ahead


def weather_from_json(payload: dict) -> Weather:
    try:
        zone = ZoneInfo(payload["timezone"])

        def local(stamp: str) -> datetime:
            return datetime.fromisoformat(stamp).replace(tzinfo=zone)

        current, hourly, daily = payload["current"], payload["hourly"], payload["daily"]
        read_at = local(current["time"])
        hours = tuple(
            Hour(local(t), float(temp), float(chance or 0))
            for t, temp, chance in zip(
                hourly["time"],
                hourly["temperature_2m"],
                hourly["precipitation_probability"],
            )
            if temp is not None
        )
        pressures = [
            (local(t), float(value))
            for t, value in zip(hourly["time"], hourly["pressure_msl"])
            if value is not None
        ]
        earlier = [
            value for time, value in pressures if time <= read_at - timedelta(hours=3)
        ]
        all_days = tuple(
            Day(
                date.fromisoformat(d),
                int(code),
                float(high),
                float(low),
                float(chance or 0),
                float(mm or 0),
                local(rise),
                local(sset),
                float(uv or 0),
            )
            for d, code, high, low, chance, mm, rise, sset, uv in zip(
                daily["time"],
                daily["weather_code"],
                daily["temperature_2m_max"],
                daily["temperature_2m_min"],
                daily["precipitation_probability_max"],
                daily["precipitation_sum"],
                daily["sunrise"],
                daily["sunset"],
                daily["uv_index_max"],
            )
            if code is not None and high is not None
        )
        today = read_at.date()
        pressure = float(current["pressure_msl"])
        return Weather(
            read_at,
            float(current["temperature_2m"]),
            float(current["apparent_temperature"]),
            float(current["relative_humidity_2m"]),
            float(current["precipitation"]),
            int(current["weather_code"]),
            float(current["wind_speed_10m"]),
            float(current["wind_gusts_10m"]),
            float(current["wind_direction_10m"]),
            pressure,
            pressure - earlier[-1] if earlier else 0.0,
            float(current["cloud_cover"]),
            float(current["visibility"]) / 1000,
            float(current["dew_point_2m"]),
            hours,
            tuple(day for day in all_days if day.day < today),
            tuple(day for day in all_days if day.day >= today),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err


def fetch_json(url: str, fetch: Callable[[str], bytes]):
    return parse_json(fetch(url))


def fetch_weather(
    latitude: float, longitude: float, fetch: Callable[[str], bytes] = fetch_bytes
) -> Weather:
    return weather_from_json(
        fetch_json(WEATHER_URL.format(lat=latitude, lon=longitude), fetch)
    )


@dataclass(frozen=True)
class Air:
    aqi: float  # US AQI
    pm25: float
    pm10: float
    ozone: float
    hours: tuple[tuple[datetime, float], ...]


def air_from_json(payload: dict) -> Air:
    try:
        zone = ZoneInfo(payload["timezone"])
        current, hourly = payload["current"], payload["hourly"]
        hours = tuple(
            (datetime.fromisoformat(t).replace(tzinfo=zone), float(v))
            for t, v in zip(hourly["time"], hourly["us_aqi"])
            if v is not None
        )
        return Air(
            float(current["us_aqi"]),
            float(current["pm2_5"]),
            float(current["pm10"]),
            float(current["ozone"]),
            hours,
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err


def fetch_air(
    latitude: float, longitude: float, fetch: Callable[[str], bytes] = fetch_bytes
) -> Air:
    return air_from_json(fetch_json(AIR_URL.format(lat=latitude, lon=longitude), fetch))


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: str
    start: datetime
    end: datetime


def alerts_from_json(payload: dict, geocode: str, zone: ZoneInfo) -> tuple[Alert, ...]:
    """INMET alerts whose city list holds `geocode` (the IBGE code), soonest first, each once."""
    try:
        found = {}
        for group in payload.values():
            for row in group:
                if geocode not in str(row.get("geocodes", "")).split(","):
                    continue
                alert = Alert(
                    row["descricao"],
                    row["severidade"],
                    datetime.strptime(row["inicio"], "%Y-%m-%d %H:%M").replace(
                        tzinfo=zone
                    ),
                    datetime.strptime(row["fim"], "%Y-%m-%d %H:%M").replace(
                        tzinfo=zone
                    ),
                )
                found[(alert.kind, alert.severity, alert.start)] = alert
    except (AttributeError, KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err
    return tuple(sorted(found.values(), key=lambda alert: (alert.start, alert.kind)))


def fetch_alerts(
    geocode: str, fetch: Callable[[str], bytes] = fetch_bytes
) -> tuple[Alert, ...]:
    return alerts_from_json(
        fetch_json(INMET_URL, fetch), geocode, ZoneInfo("America/Sao_Paulo")
    )


def tile_xy(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    """Web Mercator tile coordinates (fractional) of a point."""
    n = 2**zoom
    lat = math.radians(latitude)
    return (longitude + 180) / 360 * n, (
        1 - math.log(math.tan(lat) + 1 / math.cos(lat)) / math.pi
    ) / 2 * n


@dataclass(frozen=True)
class RadarFrame:
    time: datetime
    host: str
    path: str


def radar_frame_from_json(payload: dict) -> RadarFrame:
    try:
        latest = payload["radar"]["past"][-1]
        return RadarFrame(
            datetime.fromtimestamp(latest["time"], timezone.utc),
            payload["host"],
            latest["path"],
        )
    except (KeyError, IndexError, TypeError) as err:
        raise SourceError("formato dos dados mudou") from err


def fetch_radar_frame(fetch: Callable[[str], bytes] = fetch_bytes) -> RadarFrame:
    return radar_frame_from_json(fetch_json(RADAR_INDEX_URL, fetch))


def cached_tile(
    url: str, path: Path, fetch: Callable[[str], bytes] = fetch_bytes
) -> bytes:
    """Map tiles never change: download once, keep on disk."""
    if path.exists():
        return path.read_bytes()
    body = fetch(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(body)
    Path(handle.name).replace(path)
    return body


def sgs_from_json(payload: list, today: date) -> tuple[tuple[date, float], ...]:
    """Banco Central series as (day, value); the Selic target is published ahead, so future days are dropped."""
    try:
        points = tuple(
            (datetime.strptime(row["data"], "%d/%m/%Y").date(), float(row["valor"]))
            for row in payload
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err
    kept = tuple(point for point in points if point[0] <= today)
    if not kept:
        raise SourceError("sem dados")
    return kept


def fetch_sgs(
    code: int,
    days: int,
    today: date,
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=SGS_TIMEOUT_SECONDS),
):
    start = today - timedelta(days=days)
    url = SGS_URL.format(code=code, start=f"{start:%d/%m/%Y}", end=f"{today:%d/%m/%Y}")
    payload = parse_json(fetch(url))
    return sgs_from_json(payload, today)


def sidra_from_json(payload: list) -> tuple[tuple[str, float], ...]:
    """IBGE SIDRA rows as (period name, value); the first row is the column legend."""
    try:
        rows = tuple(
            (row["D3N"], float(row["V"]))
            for row in payload[1:]
            if row["V"] not in ("...", "-", "X")
        )
    except (KeyError, TypeError, ValueError, IndexError) as err:
        raise SourceError("formato dos dados mudou") from err
    if not rows:
        raise SourceError("sem dados")
    return rows


def fetch_sidra(
    path: str,
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=SGS_TIMEOUT_SECONDS),
):
    payload = parse_json(fetch(SIDRA_URL.format(path=path)))
    return sidra_from_json(payload)


@dataclass(frozen=True)
class Bond:
    kind: str
    maturity: date
    rate: float  # % a.a. for buying; on top of IPCA or Selic for those titles
    price: float


@dataclass(frozen=True)
class Tesouro:
    day: date  # newest prices in the file (it runs about a week behind)
    on_sale: tuple[Bond, ...]
    history: dict  # (kind, maturity) -> ((day, buying rate), ...) since `since`, oldest first


def tesouro_from_csv(text: str, since: date) -> Tesouro:
    """Titles on sale on the newest day, and each title's buying rate from `since` on.

    The file holds every day since 2002; older rows are skipped while reading to keep memory low.
    """

    def number(value: str) -> float:
        return float(value.replace(".", "").replace(",", ".")) if value.strip() else 0.0

    by_day: dict[date, list[Bond]] = {}
    try:
        for row in csv.DictReader(io.StringIO(text), delimiter=";"):
            day = datetime.strptime(row["Data Base"], "%d/%m/%Y").date()
            if day < since:
                continue
            bond = Bond(
                row["Tipo Titulo"],
                datetime.strptime(row["Data Vencimento"], "%d/%m/%Y").date(),
                number(row["Taxa Compra Manha"]),
                number(row["PU Compra Manha"]),
            )
            by_day.setdefault(day, []).append(bond)
    except (KeyError, ValueError) as err:
        raise SourceError("planilha do Tesouro mudou") from err
    if not by_day:
        raise SourceError("sem dados")
    newest = max(by_day)
    on_sale = tuple(
        sorted(
            (b for b in by_day[newest] if b.price > 0),
            key=lambda b: (b.kind, b.maturity),
        )
    )
    history: dict[tuple[str, date], list[tuple[date, float]]] = {}
    for day in sorted(by_day):
        for bond in by_day[day]:
            if bond.rate:
                history.setdefault((bond.kind, bond.maturity), []).append(
                    (day, bond.rate)
                )
    return Tesouro(
        newest, on_sale, {key: tuple(points) for key, points in history.items()}
    )


def kept_download(url: str, keep: timedelta, folder: Path, now: float) -> bytes | None:
    """The copy of `url` saved by store_download, while younger than `keep`."""
    path = folder / hashlib.sha1(url.encode()).hexdigest()
    try:
        if now - path.stat().st_mtime < keep.total_seconds():
            return path.read_bytes()
    except OSError:
        pass
    return None


def store_download(url: str, body: bytes, folder: Path, now: float) -> None:
    """Called only after the body was read successfully, so an error page is never kept."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / hashlib.sha1(url.encode()).hexdigest()
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(body)
    temporary.replace(path)
    for old in folder.iterdir():
        if now - old.stat().st_mtime > DOWNLOAD_PRUNE.total_seconds():
            old.unlink(missing_ok=True)


def fetch_tesouro(
    since: date,
    fetch: Callable[[str], bytes] = partial(
        fetch_bytes, timeout=TESOURO_TIMEOUT_SECONDS
    ),
    folder: Path = DOWNLOAD_DIR,
    now: Callable[[], float] = time.time,
):
    body = kept_download(TESOURO_URL, TESOURO_KEEP, folder, now())
    downloaded = body is None
    if downloaded:
        body = fetch(TESOURO_URL)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    result = tesouro_from_csv(text, since)
    if downloaded:
        store_download(TESOURO_URL, body, folder, now())
    return result


FOCUS_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais"
    "?$format=json&$top=20000&$select=Indicador,Data,DataReferencia,Mediana&$filter={filter}"
)


def focus_from_json(
    payload: dict,
) -> dict[tuple[str, str], tuple[tuple[date, float], ...]]:
    """Focus survey medians per (indicator, year), oldest first; one survey a week."""
    try:
        found: dict[tuple[str, str], dict[date, float]] = {}
        for row in payload["value"]:
            key = (row["Indicador"], str(row["DataReferencia"]))
            found.setdefault(key, {})[date.fromisoformat(row["Data"])] = float(
                row["Mediana"]
            )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err
    if not found:
        raise SourceError("sem dados")
    return {key: tuple(sorted(points.items())) for key, points in found.items()}


def fetch_focus(
    indicators: tuple[str, ...],
    since: date,
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=SGS_TIMEOUT_SECONDS),
):
    names = " or ".join(f"Indicador eq '{name}'" for name in indicators)
    odata = f"({names}) and baseCalculo eq 0 and Data ge '{since:%Y-%m-%d}'"
    return focus_from_json(
        fetch_json(FOCUS_URL.format(filter=urllib.parse.quote(odata)), fetch)
    )


CREDIT_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/taxaJuros/versao/v2/odata/TaxasJurosDiariaPorInicioPeriodo"
    "?$format=json&$top=20000&{query}"
)
CREDIT_SEGMENT = "PESSOA FÍSICA"


def credit_medians(rows: list[dict]) -> dict[str, float]:
    """Median yearly rate across banks for each loan type."""
    by_type: dict[str, list[float]] = {}
    for row in rows:
        if (
            row.get("Segmento") == CREDIT_SEGMENT
            and row.get("TaxaJurosAoAno") is not None
        ):
            by_type.setdefault(row["Modalidade"], []).append(
                float(row["TaxaJurosAoAno"])
            )
    return {
        name: sorted(values)[len(values) // 2]
        if len(values) % 2
        else (sorted(values)[len(values) // 2 - 1] + sorted(values)[len(values) // 2])
        / 2
        for name, values in by_type.items()
    }


@dataclass(frozen=True)
class Credit:
    week: date
    now: dict[str, float]
    month_ago: dict[str, float]
    year_ago: dict[str, float]


def fetch_credit(
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=SGS_TIMEOUT_SECONDS),
) -> Credit:
    """Banco Central's weekly survey of what banks charge individuals, now, ~1 month and ~1 year ago."""
    segment = urllib.parse.quote(
        f"Segmento eq '{CREDIT_SEGMENT}' and Modalidade eq 'Cheque especial - Prefixado'"
    )
    weeks_query = (
        f"$select=InicioPeriodo&$orderby=InicioPeriodo%20desc&$filter={segment}"
    )
    weeks = sorted(
        {
            date.fromisoformat(row["InicioPeriodo"])
            for row in fetch_json(CREDIT_URL.format(query=weeks_query), fetch).get(
                "value", []
            )
        },
        reverse=True,
    )
    if not weeks:
        raise SourceError("sem dados")

    def at(target: date) -> tuple[date, dict[str, float]]:
        week = next((w for w in weeks if w <= target), weeks[-1])
        odata = urllib.parse.quote(f"InicioPeriodo eq '{week:%Y-%m-%d}'")
        rows = fetch_json(
            CREDIT_URL.format(
                query=f"$select=Segmento,Modalidade,TaxaJurosAoAno&$filter={odata}"
            ),
            fetch,
        )
        return week, credit_medians(rows.get("value", []))

    newest, now = at(weeks[0])
    return Credit(
        newest,
        now,
        at(newest - timedelta(days=28))[1],
        at(newest - timedelta(days=364))[1],
    )


@dataclass(frozen=True)
class Draw:
    number: int
    day: date
    numbers: tuple[str, ...]
    second_numbers: tuple[str, ...]  # Dupla Sena's second draw
    clovers: tuple[str, ...]  # +Milionária's trevos
    extra: str  # Timemania's team or Dia de Sorte's month
    top_winners: int
    top_prize: float
    accumulated: bool
    next_day: date | None
    next_estimate: float


def draw_from_json(payload: dict) -> Draw:
    def day(text: str | None) -> date | None:
        return datetime.strptime(text, "%d/%m/%Y").date() if text else None

    try:
        top = (payload.get("listaRateioPremio") or [{}])[0]
        return Draw(
            int(payload["numero"]),
            day(payload["dataApuracao"]),
            tuple(payload["listaDezenas"]),
            tuple(payload.get("listaDezenasSegundoSorteio") or ()),
            tuple(payload.get("trevosSorteados") or ()),
            " ".join(
                "".join(
                    c
                    for c in payload.get("nomeTimeCoracaoMesSorte") or ""
                    if c.isprintable()
                ).split()
            ),
            int(top.get("numeroDeGanhadores") or 0),
            float(top.get("valorPremio") or 0),
            bool(payload.get("acumulado")),
            day(payload.get("dataProximoConcurso")),
            float(payload.get("valorEstimadoProximoConcurso") or 0),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err


def fetch_draw(game: str, fetch: Callable[[str], bytes] = fetch_bytes) -> Draw:
    return draw_from_json(fetch_json(LOTTERY_URL.format(game=game), fetch))


ANP_DATES = re.compile(
    r"(\d{4})[-_](\d{2})[-_](\d{2})[-_](\d{4})[-_](\d{2})[-_](\d{2})"
)


def anp_station_files(page: str) -> list[tuple[date, str]]:
    """Per-station weekly price sheets linked on the ANP page, newest first (file names vary in - and _)."""
    found = {}
    for url in re.findall(r'https?://[^"\s]+revendas_lpc[^"\s]+\.xlsx', page):
        match = ANP_DATES.search(url.rsplit("/", 1)[-1])
        if match:
            end = date(int(match[4]), int(match[5]), int(match[6]))
            found[end] = url
    return sorted(found.items(), reverse=True)


XLSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def xlsx_rows(data: bytes):
    """Rows of the first sheet as lists of strings, read with the standard library (no openpyxl)."""
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        shared = [
            "".join(t.text or "" for t in item.iter(XLSX + "t"))
            for item in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(
                XLSX + "si"
            )
        ]
        with book.open("xl/worksheets/sheet1.xml") as sheet:
            for _, element in ET.iterparse(sheet):
                if element.tag != XLSX + "row":
                    continue
                values = []
                for cell in element.iter(XLSX + "c"):
                    value = cell.find(XLSX + "v")
                    text = "" if value is None or value.text is None else value.text
                    values.append(
                        shared[int(text)] if cell.get("t") == "s" and text else text
                    )
                yield values
                element.clear()


@dataclass(frozen=True)
class StationPrice:
    city: str
    station: str
    address: str
    brand: str
    product: str
    price: float


def station_prices(rows, cities: tuple[str, ...]) -> tuple[StationPrice, ...]:
    """Rows after the ANP header line (CNPJ, RAZÃO, FANTASIA, ...) for the given cities in São Paulo state."""
    wanted = {title_key(city) for city in cities}
    columns: dict[str, int] | None = None
    prices = []
    for row in rows:
        if columns is None:
            if row and row[0] == "CNPJ":
                columns = {name: index for index, name in enumerate(row)}
            continue
        try:
            city = row[columns["MUNICÍPIO"]]
            if title_key(city) not in wanted or row[columns["ESTADO"]] != "SAO PAULO":
                continue
            address = f"{row[columns['ENDEREÇO']]}, {row[columns['NÚMERO']]} - {row[columns['BAIRRO']]}"
            prices.append(
                StationPrice(
                    city,
                    row[columns["FANTASIA"]] or row[columns["RAZÃO"]],
                    address,
                    row[columns["BANDEIRA"]],
                    row[columns["PRODUTO"]],
                    float(row[columns["PREÇO DE REVENDA"]].replace(",", ".")),
                )
            )
        except (IndexError, KeyError, ValueError):
            continue
    if columns is None:
        raise SourceError("planilha da ANP mudou")
    return tuple(prices)


@dataclass(frozen=True)
class FuelWeek:
    end: date
    prices: tuple[StationPrice, ...]


def fetch_fuel_weeks(
    cities: tuple[str, ...],
    weeks: int = 2,
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=ANP_TIMEOUT_SECONDS),
    folder: Path = DOWNLOAD_DIR,
    now: Callable[[], float] = time.time,
):
    """The newest `weeks` station surveys for these cities, newest first."""
    files = anp_station_files(fetch(ANP_PAGE_URL).decode("utf-8", errors="replace"))
    if not files:
        raise SourceError("ANP sem planilhas")
    result = []
    for end, url in files[:weeks]:
        body = kept_download(url, ANP_KEEP, folder, now())
        downloaded = body is None
        if downloaded:
            body = fetch(url)
        try:
            prices = station_prices(xlsx_rows(body), cities)
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as err:
            raise SourceError("planilha da ANP ilegível") from err
        if downloaded:
            store_download(url, body, folder, now())
        result.append(FuelWeek(end, prices))
    return tuple(result)


@dataclass(frozen=True)
class PrinterReading:
    read_at: datetime
    total_pages: int
    color_pages: int
    status: str
    alerts: tuple[str, ...]
    supplies: dict


def last_printer_reading(history: Path) -> PrinterReading | None:
    """Newest line of the printer monitor's history; it is written only when the printer answers."""
    if not history.exists():
        return None
    lines = [line for line in history.read_text().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        row = json.loads(lines[-1])
        return PrinterReading(
            datetime.fromisoformat(row["read_at"]).astimezone(),
            int(row["total_pages"]),
            int(row["color_pages"]),
            str(row["status"]),
            tuple(row.get("alerts", [])),
            dict(row["supplies"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("historico invalido") from err


@dataclass(frozen=True)
class PrinterStatus:
    payload: dict  # the monitor's full reading (viewmodel.build_payload); {} before the first
    feed_error: str  # empty while the monitor reads the printer fine
    from_disk: bool  # the monitor has nothing yet (printer off since it started): last saved copy


def printer_status(
    url: str, saved: Path, save: bool, fetch: Callable[[str], bytes] = fetch_bytes
) -> PrinterStatus:
    """The monitor's /api/status. A full reading is copied to `saved` when `save` is set, so the
    page still shows the printer after a reboot with the printer switched off."""
    data = parse_json(fetch(url))
    feed_error = str(data.get("feed_error", ""))
    if "verdict" in data:
        if save:
            saved.parent.mkdir(parents=True, exist_ok=True)
            temporary = saved.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False))
            temporary.replace(saved)
        return PrinterStatus(data, feed_error, False)
    try:
        return PrinterStatus(json.loads(saved.read_text()), feed_error, True)
    except (OSError, ValueError):
        return PrinterStatus({}, feed_error, False)


def run_command(command: str, scratch: Path) -> tuple[int, str]:
    """Run a fixed shell command, output captured through a file (no subprocess, by house rule)."""
    scratch.parent.mkdir(parents=True, exist_ok=True)
    status = os.system(f"{command} > {shlex.quote(str(scratch))} 2>&1")
    return status, scratch.read_text(errors="replace") if scratch.exists() else ""


def print_queue(scratch: Path) -> tuple[str, ...]:
    """Pending jobs, one `lpstat -o` line each."""
    status, output = run_command("lpstat -o", scratch)
    if status != 0 and output.strip():
        raise SourceError(output.strip().splitlines()[0])
    return tuple(line for line in output.splitlines() if line.strip())


@dataclass(frozen=True)
class Job:
    name: str
    user: str
    finished: datetime


def jobs_from_lpstat(output: str) -> tuple[Job, ...]:
    """`LC_ALL=C lpstat -W completed -o` lines: job, user, bytes, then `Thu Oct 22 22:35:01 2026` (local time)."""
    jobs = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            finished = datetime.strptime(
                " ".join(parts[4:8]), "%b %d %H:%M:%S %Y"
            ).astimezone()
        except ValueError:
            continue
        jobs.append(Job(parts[0], parts[1], finished))
    return tuple(sorted(jobs, key=lambda job: job.finished, reverse=True))


def completed_jobs(scratch: Path) -> tuple[Job, ...]:
    status, output = run_command("LC_ALL=C lpstat -W completed -o", scratch)
    if status != 0 and output.strip():
        raise SourceError(output.strip().splitlines()[0])
    return jobs_from_lpstat(output)


def daily_pages(entries: list[dict]) -> tuple[tuple[date, int], ...]:
    """Pages printed per day, from the printer counter at each reading (first day has no before)."""
    last_total: dict[date, int] = {}
    for entry in entries:
        if entry.get("total_pages") is None:
            continue
        day = datetime.fromisoformat(entry["read_at"]).date()
        last_total[day] = max(last_total.get(day, 0), int(entry["total_pages"]))
    days = sorted(last_total)
    return tuple(
        (day, last_total[day] - last_total[before])
        for before, day in zip(days, days[1:])
    )


# vcgencmd get_throttled bits: 0-3 happening now, 16-19 happened since boot.
THROTTLE_NOW = {
    0x1: "fonte fraca agora",
    0x2: "processador limitado",
    0x4: "freando por calor",
    0x8: "no limite de calor",
}
THROTTLE_SINCE_BOOT = {
    0x10000: "fonte fraca",
    0x20000: "processador limitado",
    0x40000: "freou por calor",
    0x80000: "chegou no limite de calor",
}


def throttle_notes(bits: int) -> tuple[list[str], list[str]]:
    return (
        [text for bit, text in THROTTLE_NOW.items() if bits & bit],
        [text for bit, text in THROTTLE_SINCE_BOOT.items() if bits & bit],
    )


def throttle_bits(scratch: Path) -> int:
    status, output = run_command("vcgencmd get_throttled", scratch)
    try:
        return int(output.strip().split("=", 1)[1], 16)
    except (IndexError, ValueError) as err:
        raise SourceError("vcgencmd sem resposta") from err


def services_state(names: tuple[str, ...], scratch: Path) -> dict[str, str]:
    """`systemctl is-active` prints one state per unit, in order; it exits non-zero when any is down."""
    _, output = run_command(
        "systemctl is-active " + " ".join(shlex.quote(name) for name in names), scratch
    )
    states = output.split()
    if len(states) != len(names):
        raise SourceError("systemctl sem resposta")
    return dict(zip(names, states))


@dataclass(frozen=True)
class Network:
    address: str
    ping_ms: float | None  # None: no answer from the internet


def network_from_output(addresses: str, ping: str) -> Network:
    ipv4 = next((a for a in addresses.split() if a.count(".") == 3), "")
    match = re.search(r"= [\d.]+/([\d.]+)/", ping)
    return Network(ipv4, float(match.group(1)) if match else None)


def network(scratch: Path) -> Network:
    _, addresses = run_command("hostname -I", scratch)
    _, ping = run_command("ping -c 1 -W 2 1.1.1.1", scratch)
    return network_from_output(addresses, ping)


def journal_errors(scratch: Path) -> int:
    """Lines at priority err or worse in the last hour (the user is in group adm, so it sees all)."""
    status, output = run_command(
        "journalctl -p err --since -1h -q --no-pager -o cat", scratch
    )
    if status != 0:
        raise SourceError("journalctl sem resposta")
    return sum(1 for line in output.splitlines() if line.strip())


def failed_units(scratch: Path) -> tuple[str, ...]:
    status, output = run_command("systemctl --failed --no-legend --plain", scratch)
    if status != 0:
        raise SourceError("systemctl sem resposta")
    return tuple(line.split()[0] for line in output.splitlines() if line.strip())


def core_volts(scratch: Path) -> float:
    _, output = run_command("vcgencmd measure_volts core", scratch)
    match = re.search(r"volt=([\d.]+)V", output)
    if match is None:
        raise SourceError("vcgencmd sem resposta")
    return float(match.group(1))


def pending_updates(scratch: Path) -> int:
    status, output = run_command("apt list --upgradable", scratch)
    if status != 0:
        raise SourceError("apt sem resposta")
    return sum(1 for line in output.splitlines() if "[upgradable from:" in line)


@dataclass(frozen=True)
class PiStats:
    temperature: float
    load: float
    cpus: int
    memory_used: float
    memory_total_mb: int
    disk_used: float
    uptime: timedelta


def pi_stats(
    proc: Path = Path("/proc"),
    thermal: Path = Path("/sys/class/thermal/thermal_zone0/temp"),
    root: str = "/",
) -> PiStats:
    meminfo = dict(
        (key, int(value.split()[0]))
        for key, value in (
            line.split(":", 1) for line in (proc / "meminfo").read_text().splitlines()
        )
    )
    disk = os.statvfs(root)
    return PiStats(
        temperature=int(thermal.read_text()) / 1000,
        load=float((proc / "loadavg").read_text().split()[0]),
        cpus=os.cpu_count() or 1,
        memory_used=1 - meminfo["MemAvailable"] / meminfo["MemTotal"],
        memory_total_mb=meminfo["MemTotal"] // 1024,
        disk_used=1 - disk.f_bavail / disk.f_blocks,
        uptime=timedelta(seconds=float((proc / "uptime").read_text().split()[0])),
    )


def selftest() -> None:
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    feed = Feed("S", "AWS", "u")
    rss = b"""<rss><channel><title>x</title>
<item><title>Old  one</title><pubDate>Thu, 22 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>New one</title><pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>No date</title></item>
<item><title>Meta&amp;#8217;s AI</title><pubDate>Fri, 23 Oct 2026 11:30:00 -0000</pubDate></item>
</channel></rss>"""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>y</title>
<entry><title>Atom story</title><updated>2026-10-23T11:00:00Z</updated></entry></feed>"""
    stories = stories_from_feed(rss, feed, 5) + stories_from_feed(
        atom, Feed("V", "Verge", "u"), 5
    )
    assert [s.title for s in stories] == [
        "Meta’s AI",
        "New one",
        "Old one",
        "Atom story",
    ]
    assert stories[0].published == now - timedelta(minutes=30), "-0000 must mean UTC"
    assert len(stories_from_feed(rss, feed, 1)) == 1
    summarized = stories_from_feed(
        b"""<rss><channel><item><title>PagBank contrata</title>
<description>&lt;img src="x.jpg"&gt;&lt;p&gt;Vagas em Sorocaba &amp;amp; Campinas&lt;/p&gt;</description>
<pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>""",
        feed,
        5,
    )[0]
    assert summarized.summary == "Vagas em Sorocaba & Campinas", summarized.summary
    assert summarized.image == "x.jpg", "image from the <img> in the description"
    media = stories_from_feed(
        b"""<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item><title>A</title>
<link>https://site/a</link><media:content url="https://site/a.jpg" medium="image"/>
<pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>""",
        feed,
        5,
    )[0]
    assert media.link == "https://site/a" and media.image == "https://site/a.jpg"
    atom_story = stories_from_feed(
        b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title>
<link rel="alternate" href="https://site/b"/><updated>2026-10-23T11:00:00Z</updated></entry></feed>""",
        feed,
        5,
    )[0]
    assert atom_story.link == "https://site/b" and atom_story.image == ""
    assert (
        og_image('<meta property="og:image" content="https://s/i.jpg?a=1&amp;b=2">')
        == "https://s/i.jpg?a=1&b=2"
    )
    assert (
        og_image('<meta content="https://s/j.png" name="twitter:image">')
        == "https://s/j.png"
    )
    assert og_image("<html></html>") == ""
    assert og_image('<meta property="og:image" content="None">') == "", (
        "g1 writes the word None"
    )
    try:
        fetch_bytes("None")
    except SourceError as err:
        assert not err.retryable
    else:
        raise AssertionError(
            "a non-web address must fail as a SourceError, not a ValueError"
        )
    with tempfile.TemporaryDirectory() as tmp:
        png = io.BytesIO()
        Image.new("RGB", (400, 100), (200, 10, 10)).save(png, "PNG")
        fetched = []

        def fake(url):
            fetched.append(url)
            return png.getvalue() if url.endswith(".png") else b"not a picture"

        first = thumbnail("https://s/p.png", (120, 68), fake, Path(tmp))
        again = thumbnail("https://s/p.png", (120, 68), fake, Path(tmp))
        assert first.size == again.size == (120, 68) and fetched == [
            "https://s/p.png"
        ], "second read from disk"
        try:
            thumbnail("https://s/broken", (120, 68), fake, Path(tmp))
        except SourceError as err:
            assert str(err) == "imagem ilegível"
        else:
            raise AssertionError("a non-image must fail")
        later = datetime.now(timezone.utc) + THUMB_KEEP + timedelta(hours=1)
        assert prune_thumbnails(later, Path(tmp)) == 1 and not list(
            Path(tmp).glob("*.jpg")
        )
    assert summarized.mentions(("sorocaba",)) and summarized.mentions(("Pagbank",))
    assert not summarized.mentions(("Itati",)), "whole words only"
    assert decoded(gzip.compress(rss)) == rss and decoded(rss) == rss
    assert (
        short_age(timedelta(minutes=30)) == "30m"
        and short_age(timedelta(hours=26)) == "1d"
    )
    assert clock(stories[2].published, now).startswith("22/10")

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
    assert (
        br_number(5.0533) == "5,0533"
        and br_number(57.83) == "57,83"
        and br_number(204120.12) == "204.120"
    )
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

    g1 = Story(now, "Preço dos alimentos: o que ficou mais caro", Feed("G", "g1", "u"))
    economia = Story(
        now,
        "Preco dos alimentos - o que ficou MAIS caro",
        Feed("E", "g1 Economia", "u"),
    )
    other = Story(now, "Outra notícia", Feed("G", "g1", "u"))
    market, general = unique_stories([[economia], [g1, other]])
    assert market == [economia] and general == [other]
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
    weather_payload = {
        "timezone": "America/Sao_Paulo",
        "current": {
            "time": "2026-10-23T09:45",
            "temperature_2m": 19.9,
            "apparent_temperature": 21.7,
            "relative_humidity_2m": 88,
            "precipitation": 0.6,
            "weather_code": 80,
            "wind_speed_10m": 6.2,
            "wind_gusts_10m": 32.8,
            "wind_direction_10m": 52,
            "pressure_msl": 1018.2,
            "cloud_cover": 99,
            "visibility": 3680.0,
            "dew_point_2m": 17.9,
        },
        "hourly": {
            "time": ["2026-10-23T06:00", "2026-10-23T09:00", "2026-10-23T10:00"],
            "temperature_2m": [18.0, 19.0, None],
            "precipitation_probability": [40, 50, 60],
            "pressure_msl": [1016.0, 1018.0, 1018.5],
        },
        "daily": {
            "time": ["2026-10-22", "2026-10-23", "2026-10-24"],
            "weather_code": [61, 95, None],
            "temperature_2m_max": [24.0, 25.0, None],
            "temperature_2m_min": [16.0, 17.5, None],
            "precipitation_probability_max": [80, 97, None],
            "precipitation_sum": [12.4, 22.3, None],
            "sunrise": ["2026-10-22T05:29", "2026-10-23T05:28", "2026-10-24T05:27"],
            "sunset": ["2026-10-22T18:14", "2026-10-23T18:15", "2026-10-24T18:15"],
            "uv_index_max": [3.0, 4.75, None],
        },
    }
    weather = weather_from_json(weather_payload)
    assert (
        weather.temperature == 19.9 and weather.code == 80 and len(weather.hours) == 2
    )
    assert (
        weather.read_at.utcoffset() == timedelta(hours=-3)
        and weather.days[0].high == 25.0
    )
    assert [d.day.day for d in weather.past] == [22] and [
        d.day.day for d in weather.days
    ] == [23], "yesterday goes to past; a day with no forecast yet is dropped"
    assert round(weather.pressure_3h_change, 1) == 2.2 and weather.visibility_km == 3.68
    assert weather.days[0].length == timedelta(hours=12, minutes=47)
    try:
        weather_from_json({"timezone": "America/Sao_Paulo"})
    except SourceError:
        pass
    else:
        raise AssertionError("weather without fields must fail")

    air = air_from_json(
        {
            "timezone": "America/Sao_Paulo",
            "current": {"us_aqi": 42, "pm2_5": 9.1, "pm10": 15.0, "ozone": 60.0},
            "hourly": {
                "time": ["2026-10-23T10:00", "2026-10-23T11:00"],
                "us_aqi": [40, None],
            },
        }
    )
    assert air.aqi == 42 and len(air.hours) == 1
    zone = ZoneInfo("America/Sao_Paulo")
    inmet = {
        "hoje": [
            {
                "descricao": "Tempestade",
                "severidade": "Perigo",
                "inicio": "2026-10-23 00:01",
                "fim": "2026-10-23 23:59",
                "geocodes": "3509502,3552205",
            },
            {
                "descricao": "Tempestade",
                "severidade": "Perigo",
                "inicio": "2026-10-23 00:01",
                "fim": "2026-10-23 23:59",
                "geocodes": "3552205",
            },
            {
                "descricao": "Baixa Umidade",
                "severidade": "Perigo Potencial",
                "inicio": "2026-10-23 12:00",
                "fim": "2026-10-23 20:00",
                "geocodes": "35522050,2300101",
            },
        ],
        "futuro": [
            {
                "descricao": "Tempestade",
                "severidade": "Perigo Potencial",
                "inicio": "2026-10-24 00:01",
                "fim": "2026-10-24 23:59",
                "geocodes": "3552205",
            }
        ],
    }
    alerts = alerts_from_json(inmet, "3552205", zone)
    assert [(a.kind, a.start.day) for a in alerts] == [
        ("Tempestade", 23),
        ("Tempestade", 24),
    ], (
        "same alert listed twice counts once; a longer code that only starts with ours does not match"
    )
    frame = radar_frame_from_json(
        {
            "host": "https://tilecache.rainviewer.com",
            "radar": {
                "past": [{"time": 1, "path": "/a"}, {"time": 1792762800, "path": "/b"}]
            },
        }
    )
    assert frame.path == "/b" and frame.time.year == 2026
    x, y = tile_xy(-23.5475, -46.63611, 7)
    assert (int(x), int(y)) == (47, 72), (
        "matches the tile checked by hand on 2026-10-23"
    )

    selic = sgs_from_json(
        [
            {"data": "23/10/2026", "valor": "13.75"},
            {"data": "04/11/2026", "valor": "13.75"},
        ],
        date(2026, 10, 23),
    )
    assert selic == ((date(2026, 10, 23), 13.75),), "future Selic days are dropped"
    try:
        sgs_from_json([{"data": "x"}], date(2026, 10, 23))
    except SourceError:
        pass
    else:
        raise AssertionError("bad SGS row must fail")

    draw = draw_from_json(
        {
            "numero": 3073,
            "dataApuracao": "22/10/2026",
            "listaDezenas": ["04", "13"],
            "listaDezenasSegundoSorteio": None,
            "trevosSorteados": ["2", "3"],
            "nomeTimeCoracaoMesSorte": "TOMBENSE         /MG",
            "acumulado": True,
            "listaRateioPremio": [{"numeroDeGanhadores": 0, "valorPremio": 0.0}],
            "dataProximoConcurso": "24/10/2026",
            "valorEstimadoProximoConcurso": 230000000.0,
        }
    )
    assert (
        draw.number == 3073
        and draw.clovers == ("2", "3")
        and draw.extra == "TOMBENSE /MG"
        and draw.accumulated
    )
    assert draw.next_day == date(2026, 10, 24) and draw.second_numbers == ()
    blank = draw_from_json(
        {
            "numero": 1,
            "dataApuracao": "22/10/2026",
            "listaDezenas": ["01"],
            "nomeTimeCoracaoMesSorte": "\x00" * 17,
        }
    )
    assert blank.extra == "", (
        "Caixa pads the team field with NUL characters in games without one"
    )
    page = (
        '<a href="https://www.gov.br/x/arquivos-lpc/2026/revendas_lpc_2026-10-04_2026-10-10.xlsx">'
        '<a href="https://www.gov.br/x/arquivos-lpc/2026/revendas_lpc_2026_10_11-2026_10_17.xlsx">'
        '<a href="https://www.gov.br/x/arquivos-lpc/2026/resumo_semanal_lpc_2026-10-11_2026-10-17.xlsx">'
    )
    files = anp_station_files(page)
    assert [end for end, _ in files] == [date(2026, 10, 17), date(2026, 10, 10)], (
        "mixed - and _ names, newest first"
    )
    header = [
        "CNPJ",
        "RAZÃO",
        "FANTASIA",
        "ENDEREÇO",
        "NÚMERO",
        "COMPLEMENTO",
        "BAIRRO",
        "CEP",
        "MUNICÍPIO",
        "ESTADO",
        "BANDEIRA",
        "PRODUTO",
        "UNIDADE DE MEDIDA",
        "PREÇO DE REVENDA",
    ]
    rows = [
        ["PERÍODO: 11/10/2026 A 17/10/2026"],
        header,
        [
            "1",
            "POSTO A LTDA",
            "POSTO A",
            "AV X",
            "10",
            "",
            "CENTRO",
            "1",
            "SOROCABA",
            "SAO PAULO",
            "SHELL",
            "GASOLINA COMUM",
            "R$ / litro",
            "6.19",
        ],
        [
            "2",
            "POSTO B LTDA",
            "",
            "RUA Y",
            "5",
            "",
            "VILA",
            "1",
            "SOROCABA",
            "MINAS GERAIS",
            "BRANCA",
            "ETANOL",
            "R$ / litro",
            "4.09",
        ],
        [
            "3",
            "POSTO C LTDA",
            "",
            "RUA Z",
            "7",
            "",
            "CENTRO",
            "1",
            "JUNDIAI",
            "SAO PAULO",
            "BRANCA",
            "ETANOL",
            "R$ / litro",
            "4,19",
        ],
    ]
    found = station_prices(rows, ("Sorocaba", "Jundiaí"))
    assert [(p.station, p.city, p.price) for p in found] == [
        ("POSTO A", "SOROCABA", 6.19),
        ("POSTO C LTDA", "JUNDIAI", 4.19),
    ], (
        "same-name city in another state is skipped; empty trade name falls back to the company"
    )
    try:
        station_prices([["outra planilha"]], ("Sorocaba",))
    except SourceError:
        pass
    else:
        raise AssertionError("a sheet without the header must fail")
    calls, waits = [], []

    def opener_factory(outcomes):
        def opener(request, timeout):
            calls.append(request.full_url)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return io.BytesIO(outcome)

        return opener

    gateway = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
    assert (
        fetch_bytes("http://x", 1, opener_factory([gateway, b"ok"]), waits.append)
        == b"ok"
    )
    assert len(calls) == 2 and waits == [RETRY_BACKOFF_SECONDS], (
        "a 502 is tried once more after a pause"
    )
    calls.clear()
    missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    try:
        fetch_bytes("http://x", 1, opener_factory([missing, b"ok"]), waits.append)
    except SourceError as err:
        assert str(err) == "HTTP 404" and len(calls) == 1, "a 404 is not retried"
    else:
        raise AssertionError("404 must fail")
    calls.clear()
    try:
        fetch_bytes("http://x", 1, opener_factory([gateway, gateway]), waits.append)
    except SourceError as err:
        assert "erro no servidor deles" in str(err) and len(calls) == FETCH_ATTEMPTS
    else:
        raise AssertionError("two 502s must fail")
    for body, message in (
        (b"<html>erro</html>", "veio página de erro em vez de dados"),
        (b"  ", "resposta vazia"),
        (b"x{", "dados em formato inesperado"),
    ):
        try:
            parse_json(body)
        except SourceError as err:
            assert str(err) == message, err
        else:
            raise AssertionError(body)
    focus = focus_from_json(
        {
            "value": [
                {
                    "Indicador": "Selic",
                    "Data": "2026-10-16",
                    "DataReferencia": "2026",
                    "Mediana": 13.25,
                },
                {
                    "Indicador": "Selic",
                    "Data": "2026-10-09",
                    "DataReferencia": "2026",
                    "Mediana": 13.5,
                },
                {
                    "Indicador": "IPCA",
                    "Data": "2026-10-16",
                    "DataReferencia": 2027,
                    "Mediana": 4.32,
                },
            ]
        }
    )
    assert focus[("Selic", "2026")] == (
        (date(2026, 10, 9), 13.5),
        (date(2026, 10, 16), 13.25),
    )
    assert ("IPCA", "2027") in focus, (
        "a year that comes as a number is a key like the others"
    )
    medians = credit_medians(
        [
            {"Segmento": CREDIT_SEGMENT, "Modalidade": "Cheque", "TaxaJurosAoAno": v}
            for v in (100.0, 150.0, 200.0, 400.0)
        ]
        + [
            {
                "Segmento": "PESSOA JURÍDICA",
                "Modalidade": "Cheque",
                "TaxaJurosAoAno": 1.0,
            },
            {
                "Segmento": CREDIT_SEGMENT,
                "Modalidade": "Rotativo",
                "TaxaJurosAoAno": 476.33,
            },
        ]
    )
    assert medians == {"Cheque": 175.0, "Rotativo": 476.33}, medians
    pages = {
        "weeks": {
            "value": [
                {"InicioPeriodo": d} for d in ("2026-10-02", "2026-09-04", "2025-10-03")
            ]
        }
    }

    def fake_credit(url):
        if "orderby" in url:
            return json.dumps(pages["weeks"]).encode()
        week = urllib.parse.unquote(url).split("InicioPeriodo eq '")[1][:10]
        return json.dumps(
            {
                "value": [
                    {
                        "Segmento": CREDIT_SEGMENT,
                        "Modalidade": "Cheque",
                        "TaxaJurosAoAno": {
                            "2026-10-02": 155.0,
                            "2026-09-04": 150.0,
                            "2025-10-03": 140.0,
                        }[week],
                    }
                ]
            }
        ).encode()

    credit = fetch_credit(fake_credit)
    assert credit.week == date(2026, 10, 2) and credit.now == {"Cheque": 155.0}
    assert credit.month_ago == {"Cheque": 150.0} and credit.year_ago == {
        "Cheque": 140.0
    }
    jobless = sidra_from_json(
        [
            {"V": "Valor", "D3N": "Trimestre"},
            {"V": "5.4", "D3N": "abr-mai-jun 2026"},
            {"V": "...", "D3N": "mai-jun-jul 2026"},
        ]
    )
    assert jobless == (("abr-mai-jun 2026", 5.4),), (
        "IBGE placeholders like ... are skipped"
    )
    tesouro = tesouro_from_csv(
        "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        "Tesouro Selic;01/03/2027;15/10/2026;0,01;0,02;20097,80;20086,80;20086,80\n"
        "Tesouro IPCA+;15/05/2035;22/10/2026;7,45;7,57;2.345,67;2.300,00;2.300,00\n"
        "Tesouro Selic;01/03/2027;22/10/2026;0,02;0,03;20101,10;20090,00;20090,00\n"
        "Tesouro Prefixado;01/01/2027;22/10/2026;;13,50;0,00;980,00;980,00\n"
        "Tesouro Selic;01/03/2027;01/01/2020;0,10;0,20;10000,00;10000,00;10000,00\n",
        date(2025, 10, 1),
    )
    newest, bonds = tesouro.day, tesouro.on_sale
    assert tesouro.history[("Tesouro Selic", date(2027, 3, 1))] == (
        (date(2026, 10, 15), 0.01),
        (date(2026, 10, 22), 0.02),
    ), "history oldest first, rows before `since` skipped"
    assert newest == date(2026, 10, 22) and [b.kind for b in bonds] == [
        "Tesouro IPCA+",
        "Tesouro Selic",
    ]
    assert bonds[0].price == 2345.67 and bonds[0].rate == 7.45, (
        "Brazilian thousands and decimal marks"
    )
    try:
        tesouro_from_csv("outra;coisa\n1;2\n", date(2025, 1, 1))
    except SourceError:
        pass
    else:
        raise AssertionError("a changed spreadsheet must fail loudly")
    csv = (
        "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        "Tesouro Selic;01/03/2027;22/10/2026;0,02;0,03;20101,10;20090,00;20090,00\n"
    ).encode()
    with tempfile.TemporaryDirectory() as tmp:
        downloads, moment = (
            [],
            [time.time()],
        )  # file times are real, so the clock starts now

        def counted(url: str) -> bytes:
            downloads.append(url)
            return csv

        def broken(url: str) -> bytes:
            downloads.append(url)
            return b"<html>pagina de erro</html>"

        folder = Path(tmp)
        try:
            fetch_tesouro(date(2026, 1, 1), broken, folder, lambda: moment[0])
        except SourceError:
            pass
        else:
            raise AssertionError("an error page must fail")
        assert not list(folder.iterdir()), "an error page is never kept"
        first = fetch_tesouro(date(2026, 1, 1), counted, folder, lambda: moment[0])
        moment[0] += 3600
        again = fetch_tesouro(date(2026, 1, 1), counted, folder, lambda: moment[0])
        assert first == again and len(downloads) == 2, (
            "a restart within 12 h reads the disk copy"
        )
        stamp = moment[0] + TESOURO_KEEP.total_seconds()
        for kept in folder.iterdir():
            os.utime(kept, (stamp - 13 * 3600, stamp - 13 * 3600))
        fetch_tesouro(date(2026, 1, 1), counted, folder, lambda: stamp)
        assert len(downloads) == 3, "an old copy is downloaded again"
        old = folder / "sobra"
        old.write_bytes(b"x")
        os.utime(old, (0, 0))
        store_download("https://x", b"y", folder, stamp)
        assert not old.exists(), "copies older than 30 days are deleted"

    with tempfile.TemporaryDirectory() as tmp:
        history = Path(tmp) / "history.jsonl"
        assert last_printer_reading(history) is None
        history.write_text(
            '{"read_at": "2026-10-22T10:00:00", "total_pages": 1, "color_pages": 0, "status": "x", "supplies": {}}\n'
            '{"read_at": "2026-10-22T22:50:06", "total_pages": 1328, "color_pages": 455, "status": "inPowerSave", '
            '"alerts": ["incompatibleConsumable"], "supplies": {"Cyan": {"percent": 50, "pages_remaining": 350}}}\n'
        )
        reading = last_printer_reading(history)
        assert reading.total_pages == 1328 and reading.supplies["Cyan"]["percent"] == 50
        assert reading.alerts == ("incompatibleConsumable",)
        saved = Path(tmp) / "printer.json"
        empty = printer_status(
            "u",
            saved,
            True,
            lambda url: b'{"loading": true, "feed_error": "printer not found"}',
        )
        assert empty == PrinterStatus({}, "printer not found", False), (
            "nothing saved yet: no reading at all"
        )
        printer_status("u", saved, False, lambda url: b'{"verdict": "ok", "reads": 1}')
        assert not saved.exists(), "the caller decides how often the SD card is written"
        live = printer_status(
            "u", saved, True, lambda url: b'{"verdict": "ok", "reads": 2}'
        )
        assert live == PrinterStatus({"verdict": "ok", "reads": 2}, "", False)
        off = printer_status(
            "u", saved, True, lambda url: b'{"loading": true, "feed_error": "x"}'
        )
        assert off == PrinterStatus({"verdict": "ok", "reads": 2}, "x", True), (
            "printer off: last saved reading"
        )

        lpstat = (
            "HP_CP1025-14            21121210G        20480   Thu Oct 22 21:36:37 2026\n"
            "HP_CP1025-16            iklo            110592   Thu Oct 22 22:35:01 2026\n"
            "linha estranha\n"
        )
        jobs = jobs_from_lpstat(lpstat)
        assert [job.name for job in jobs] == ["HP_CP1025-16", "HP_CP1025-14"], (
            "newest first"
        )
        assert jobs[0].finished.hour == 22 and jobs[0].finished.tzinfo is not None
        printed = daily_pages(
            [
                {"read_at": "2026-10-20T08:00:00", "total_pages": 1300},
                {"read_at": "2026-10-21T09:00:00", "total_pages": 1310},
                {"read_at": "2026-10-21T18:00:00", "total_pages": 1320},
                {"read_at": "2026-10-22T22:50:06", "total_pages": 1328},
                {"read_at": "2026-10-22T23:00:00", "total_pages": None},
            ]
        )
        assert printed == ((date(2026, 10, 21), 20), (date(2026, 10, 22), 8))
        assert throttle_notes(0x0) == ([], []) and throttle_notes(0x50005) == (
            ["fonte fraca agora", "freando por calor"],
            ["fonte fraca", "freou por calor"],
        )
        net = network_from_output(
            "192.168.0.10 fd00::1 2606:4700::1",
            "rtt min/avg/max/mdev = 26.731/34.470/39.332/5.532 ms",
        )
        assert net == Network("192.168.0.10", 34.47)
        assert network_from_output("", "100% packet loss").ping_ms is None
        proc = Path(tmp) / "proc"
        proc.mkdir()
        (proc / "meminfo").write_text(
            "MemTotal: 926208 kB\nMemAvailable: 463104 kB\nCached: 1 kB\n"
        )
        (proc / "loadavg").write_text("0.42 0.30 0.20 1/200 999\n")
        (proc / "uptime").write_text("3600.5 100.0\n")
        thermal = Path(tmp) / "temp"
        thermal.write_text("49388\n")
        stats = pi_stats(proc, thermal, tmp)
        assert (
            stats.temperature == 49.388
            and stats.load == 0.42
            and stats.memory_used == 0.5
        )
        assert stats.memory_total_mb == 904 and stats.uptime == timedelta(
            seconds=3600.5
        )
        assert 0 <= stats.disk_used <= 1
    print("sources selftest ok")


if __name__ == "__main__":
    selftest()

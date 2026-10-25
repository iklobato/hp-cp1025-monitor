"""Data for the Pi dashboard: weather and air (Open-Meteo), alerts (INMET) and the radar
(RainViewer over Esri tiles).
"""

import math
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from net import SourceError, fetch_bytes, fetch_json


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


def selftest() -> None:
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
    print("weather_data selftest ok")


if __name__ == "__main__":
    selftest()

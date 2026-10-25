"""Weather pages for the home city (set in carrossel.toml, [blocos.clima]), no keys.

Clima: now (with wind, pressure, clouds), the next 24 hours, 16 days, the rain of the last
7 days, and sun and moon. Clima detalhes: rain radar map, INMET alerts for the home city,
air quality, and the home city next to a second one.
Sources: Open-Meteo (forecast, air), INMET (alerts), RainViewer (radar), CARTO (map).
"""

import io
import math
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import fields
import sources
import ui
from PIL import Image, ImageDraw
from sources import Air, Alert, Day, Hour, RadarFrame, SourceError, Weather


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Places:
    """The cities the weather blocks show; set on the settings page ([blocos.clima])."""

    home: Place
    ibge: str  # how INMET alerts list the home city
    other: Place


DEFAULT_PLACES = Places(
    Place("São Paulo", -23.5475, -46.63611),  # Open-Meteo geocoding for "São Paulo"
    "3550308",
    Place("Campinas", -22.90556, -47.06083),
)
REFRESH_EVERY = timedelta(minutes=15)
AIR_REFRESH = timedelta(minutes=30)
RADAR_REFRESH = timedelta(minutes=10)
RETRY_AFTER = timedelta(minutes=5)
HOURS_AHEAD = 24
RADAR_ZOOM = 7
SIDE_COLUMN = 776  # alerts, air quality and the two cities, right of the map
COMPACT_MAP_SHARE = 0.5  # in the narrow column the map takes the top half
TILE = 256
TILE_DIR = sources.CACHE_DIR / "tiles"

SUN = (250, 204, 21)
CLOUD = (176, 186, 197)
DARK_CLOUD = (110, 119, 131)
RAIN = (88, 166, 255)
MOON = (226, 232, 240)

# WMO weather codes used by Open-Meteo: description and which icon to draw.
CONDITIONS = {
    0: ("Céu limpo", "sun"),
    1: ("Quase limpo", "sun"),
    2: ("Parcialmente nublado", "partly"),
    3: ("Nublado", "cloud"),
    45: ("Neblina", "fog"),
    48: ("Neblina com geada", "fog"),
    51: ("Garoa fraca", "rain"),
    53: ("Garoa", "rain"),
    55: ("Garoa forte", "rain"),
    56: ("Garoa congelante", "rain"),
    57: ("Garoa congelante", "rain"),
    61: ("Chuva fraca", "rain"),
    63: ("Chuva", "rain"),
    65: ("Chuva forte", "rain"),
    66: ("Chuva congelante", "rain"),
    67: ("Chuva congelante", "rain"),
    71: ("Neve fraca", "snow"),
    73: ("Neve", "snow"),
    75: ("Neve forte", "snow"),
    77: ("Neve", "snow"),
    80: ("Pancadas fracas", "rain"),
    81: ("Pancadas de chuva", "rain"),
    82: ("Pancadas fortes", "rain"),
    85: ("Pancadas de neve", "snow"),
    86: ("Pancadas de neve", "snow"),
    95: ("Trovoada", "storm"),
    96: ("Trovoada com granizo", "storm"),
    99: ("Trovoada com granizo", "storm"),
}
COMPASS = ("N", "NE", "L", "SE", "S", "SO", "O", "NO")
WEEKDAYS_SHORT = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")

# Moon: mean synodic month and a known new moon (2000-01-06 18:14 UTC).
SYNODIC_DAYS = 29.530588853
NEW_MOON_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
MOON_PHASES = (
    "Lua nova",
    "Lua crescente",
    "Quarto crescente",
    "Crescente gibosa",
    "Lua cheia",
    "Minguante gibosa",
    "Quarto minguante",
    "Lua minguante",
)

# US AQI bands (EPA): upper bound, name, color.
AIR_BANDS = (
    (50, "Boa", (63, 185, 80)),
    (100, "Moderada", (210, 153, 34)),
    (150, "Ruim para sensíveis", (240, 136, 62)),
    (200, "Ruim", (248, 81, 73)),
    (300, "Muito ruim", (163, 113, 247)),
    (10_000, "Perigosa", (150, 40, 60)),
)
SEVERITY_COLORS = {
    "Perigo Potencial": (210, 153, 34),
    "Perigo": (240, 136, 62),
    "Grande Perigo": (248, 81, 73),
}


def condition(code: int) -> tuple[str, str]:
    return CONDITIONS.get(code, (f"código {code}", "cloud"))


def degrees(value: float) -> str:
    return f"{value:.0f}°"


def compass(direction: float) -> str:
    return COMPASS[round(direction / 45) % 8]


def pressure_trend(change: float) -> str:
    if change >= 1:
        return "subindo"
    return "caindo" if change <= -1 else "estável"


def air_band(aqi: float) -> tuple[str, tuple]:
    return next((name, color) for limit, name, color in AIR_BANDS if aqi <= limit)


@dataclass(frozen=True)
class Moon:
    age_days: float

    @classmethod
    def at(cls, moment: datetime) -> "Moon":
        return cls(((moment - NEW_MOON_EPOCH).total_seconds() / 86400) % SYNODIC_DAYS)

    @property
    def illumination(self) -> float:
        return (1 - math.cos(2 * math.pi * self.age_days / SYNODIC_DAYS)) / 2

    @property
    def name(self) -> str:
        return MOON_PHASES[int((self.age_days / SYNODIC_DAYS * 8 + 0.5) % 8)]

    @property
    def waxing(self) -> bool:
        return self.age_days < SYNODIC_DAYS / 2

    def days_to_full(self) -> float:
        return (SYNODIC_DAYS / 2 - self.age_days) % SYNODIC_DAYS

    def days_to_new(self) -> float:
        return SYNODIC_DAYS - self.age_days


def draw_moon(draw, cx: float, cy: float, radius: float, moon: Moon) -> None:
    """Lit part row by row: the terminator sits at k*w on each row (k = 1 new, -1 full)."""
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=ui.GRID)
    k = math.cos(2 * math.pi * moon.age_days / SYNODIC_DAYS)
    for row in range(-int(radius), int(radius) + 1):
        half = math.sqrt(max(radius * radius - row * row, 0))
        start, end = (k * half, half) if moon.waxing else (-half, -k * half)
        if end - start >= 0.5:
            draw.line([(cx + start, cy + row), (cx + end, cy + row)], fill=MOON)


def draw_cloud(draw, cx: float, cy: float, size: float, color: tuple) -> None:
    r = size * 0.22
    draw.ellipse(
        [cx - size * 0.42, cy - r * 0.6, cx - size * 0.42 + 2 * r, cy + r * 1.4],
        fill=color,
    )
    draw.ellipse(
        [cx - size * 0.2, cy - r * 1.6, cx - size * 0.2 + 2.4 * r, cy + r * 0.9],
        fill=color,
    )
    draw.ellipse(
        [cx + size * 0.02, cy - r * 0.9, cx + size * 0.02 + 2 * r, cy + r * 1.1],
        fill=color,
    )
    draw.rounded_rectangle(
        [cx - size * 0.42 + r, cy, cx + size * 0.02 + r, cy + r * 1.4],
        radius=r * 0.5,
        fill=color,
    )


def draw_sun(draw, cx: float, cy: float, size: float) -> None:
    r = size * 0.2
    for step in range(8):
        angle = step * math.pi / 4
        inner, outer = r * 1.35, r * 1.9
        draw.line(
            [
                (cx + inner * math.cos(angle), cy + inner * math.sin(angle)),
                (cx + outer * math.cos(angle), cy + outer * math.sin(angle)),
            ],
            fill=SUN,
            width=max(2, int(size / 30)),
        )
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SUN)


def draw_icon(draw, kind: str, cx: float, cy: float, size: float) -> None:
    """Simple vector weather icon, `size` pixels wide, centered on (cx, cy)."""
    if kind == "sun":
        draw_sun(draw, cx, cy, size)
        return
    if kind == "partly":
        draw_sun(draw, cx - size * 0.15, cy - size * 0.15, size * 0.8)
        draw_cloud(draw, cx + size * 0.05, cy + size * 0.08, size * 0.85, CLOUD)
        return
    if kind == "fog":
        for row in range(4):
            y = cy - size * 0.2 + row * size * 0.14
            draw.line(
                [(cx - size * 0.38, y), (cx + size * 0.38, y)],
                fill=CLOUD,
                width=max(2, int(size / 20)),
            )
        return
    draw_cloud(
        draw, cx, cy - size * 0.12, size, DARK_CLOUD if kind == "storm" else CLOUD
    )
    drop_top = cy + size * 0.18
    if kind == "rain":
        for offset in (-0.22, 0, 0.22):
            x = cx + offset * size
            draw.line(
                [(x, drop_top), (x - size * 0.06, drop_top + size * 0.2)],
                fill=RAIN,
                width=max(2, int(size / 25)),
            )
    elif kind == "snow":
        for offset in (-0.22, 0, 0.22):
            x, r = cx + offset * size, size * 0.04
            draw.ellipse(
                [x - r, drop_top + size * 0.08 - r, x + r, drop_top + size * 0.08 + r],
                fill=ui.TEXT,
            )
    elif kind == "storm":
        x, y, s = cx, drop_top - size * 0.06, size
        draw.polygon(
            [
                (x + 0.02 * s, y),
                (x - 0.1 * s, y + 0.17 * s),
                (x - 0.01 * s, y + 0.17 * s),
                (x - 0.08 * s, y + 0.32 * s),
                (x + 0.12 * s, y + 0.11 * s),
                (x + 0.02 * s, y + 0.11 * s),
                (x + 0.09 * s, y),
            ],
            fill=SUN,
        )


def upcoming_hours(hours: tuple[Hour, ...], now: datetime) -> list[Hour]:
    current = now.replace(minute=0, second=0, microsecond=0)
    return [hour for hour in hours if hour.time >= current][:HOURS_AHEAD]


def draw_now(draw, box: tuple, weather: Weather, today: Day | None, city: str) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, f"Agora em {city}")
    description, kind = condition(weather.code)
    draw_icon(draw, kind, left + 110, top + 150, 160)
    draw.text(
        (left + 215, top + 180),
        degrees(weather.temperature),
        font=ui.font(100, bold=True),
        fill=ui.TEXT,
        anchor="ls",
    )
    draw.text(
        (left + 222, top + 222),
        description,
        font=ui.font(26),
        fill=ui.TEXT,
        anchor="ls",
    )
    draw.text(
        (right - 16, top + 30),
        f"medido {weather.read_at:%H:%M}",
        font=ui.font(14),
        fill=ui.DIM,
        anchor="rs",
    )
    trend = pressure_trend(weather.pressure_3h_change)
    lines = [
        ("Sensação", degrees(weather.feels_like)),
        ("Umidade", f"{weather.humidity:.0f}%"),
        ("Vento", f"{weather.wind:.0f} km/h do {compass(weather.wind_from)}"),
        ("Rajadas", f"{weather.gusts:.0f} km/h"),
        ("Pressão", f"{weather.pressure:.0f} hPa, {trend}"),
        ("Nuvens", f"{weather.clouds:.0f}%"),
        ("Visibilidade", f"{ui.decimal(weather.visibility_km, 1)} km"),
        ("Orvalho", degrees(weather.dew_point)),
        ("Chuva agora", f"{ui.decimal(weather.rain_now, 1)} mm"),
        ("UV hoje", f"{today.uv:.0f}" if today else "-"),
    ]
    narrow = (
        right - left < 600
    )  # sharing the screen: label above value instead of beside it
    column_width = (right - left - 48) / 2
    for index, (label, value) in enumerate(lines):
        column, row = index % 2, index // 2
        x = left + 24 + column * (column_width if narrow else 340)
        y = top + 270 + row * 38 if narrow else top + 280 + row * 36
        if narrow:
            draw.text((x, y - 14), label, font=ui.font(13), fill=ui.DIM, anchor="ls")
            draw.text(
                (x, y + 6),
                ui.fit(value, ui.font(16, bold=True), column_width - 8),
                font=ui.font(16, bold=True),
                fill=ui.TEXT,
                anchor="ls",
            )
        else:
            draw.text((x, y), label, font=ui.font(17), fill=ui.DIM, anchor="ls")
            draw.text(
                (x + 130, y),
                value,
                font=ui.font(18, bold=True),
                fill=ui.TEXT,
                anchor="ls",
            )


def draw_hours(draw, box: tuple, hours: list[Hour], compact: bool = False) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, f"Próximas {len(hours) if compact else HOURS_AHEAD} horas")
    if not compact:
        draw.text(
            (right - 16, top + 30),
            "temperatura (linha) · chance de chuva (barras)",
            font=ui.font(14),
            fill=ui.DIM,
            anchor="rs",
        )
    if not hours:
        return
    chart_left, chart_right = left + 30, right - 30
    height = bottom - top  # 470 on the full page, 260 in the compact column
    temp_top, temp_bottom = top + 62 + 0.04 * height, top + 0.62 * height
    bar_top, bar_bottom = top + 0.66 * height, bottom - 44
    step = (chart_right - chart_left) / max(len(hours) - 1, 1)
    bar_width = max(step * 0.6, 4)
    for index, hour in enumerate(hours):
        x = chart_left + index * step
        height = (bar_bottom - bar_top) * hour.rain_chance / 100
        draw.rectangle(
            [x - bar_width / 2, bar_bottom - height, x + bar_width / 2, bar_bottom],
            fill=ui.blend(RAIN, ui.CARD, 0.75),
        )
        if index % 3 == 0:
            draw.text(
                (x, bottom - 18),
                f"{hour.time:%H}h",
                font=ui.font(15),
                fill=ui.DIM,
                anchor="ms",
            )
            if hour.rain_chance:
                draw.text(
                    (x, bar_bottom - height - 6),
                    f"{hour.rain_chance:.0f}%",
                    font=ui.font(13),
                    fill=RAIN,
                    anchor="ms",
                )
    temperatures = [hour.temperature for hour in hours]
    low, high = min(temperatures), max(temperatures)
    span = (high - low) or 1
    points = [
        (
            chart_left + i * step,
            temp_bottom - (h.temperature - low) / span * (temp_bottom - temp_top),
        )
        for i, h in enumerate(hours)
    ]
    draw.line(points, fill=ui.AMBER, width=3, joint="curve")
    for index, (x, y) in enumerate(points):
        if index % 3 == 0:
            draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=ui.AMBER)
            draw.text(
                (x, y - 12),
                degrees(hours[index].temperature),
                font=ui.font(16, bold=True),
                fill=ui.TEXT,
                anchor="ms",
            )


def draw_day(draw, box: tuple, day: Day, today: date) -> None:
    left, top, right, bottom = box
    ui.card(draw, box)
    center = (left + right) / 2
    name = "Hoje" if day.day == today else WEEKDAYS_SHORT[day.day.weekday()]
    draw.text(
        (center, top + 30), name, font=ui.font(19, bold=True), fill=ui.TEXT, anchor="ms"
    )
    draw.text(
        (center, top + 52),
        f"{day.day:%d/%m}",
        font=ui.font(14),
        fill=ui.DIM,
        anchor="ms",
    )
    draw_icon(draw, condition(day.code)[1], center, top + 112, 70)
    draw.text(
        (center, top + 196),
        degrees(day.high),
        font=ui.font(28, bold=True),
        fill=ui.TEXT,
        anchor="ms",
    )
    draw.text(
        (center, top + 226),
        degrees(day.low),
        font=ui.font(20),
        fill=ui.DIM,
        anchor="ms",
    )
    draw.text(
        (center, top + 262),
        f"{day.rain_chance:.0f}%",
        font=ui.font(17, bold=True),
        fill=RAIN,
        anchor="ms",
    )
    draw.text(
        (center, top + 284),
        f"{ui.decimal(day.rain_mm, 1)} mm",
        font=ui.font(13),
        fill=RAIN,
        anchor="ms",
    )


def draw_past_rain(draw, box: tuple, past: tuple[Day, ...]) -> None:
    left, top, right, bottom = box
    week = past[-7:]
    total = sum(day.rain_mm for day in week)
    ui.card(draw, box, "Chuva nos últimos 7 dias")
    draw.text(
        (right - 16, top + 30),
        f"total {ui.decimal(total, 1)} mm",
        font=ui.font(16, bold=True),
        fill=RAIN,
        anchor="rs",
    )
    if not week:
        return
    chart_top, chart_bottom = top + 52, bottom - 30
    slot = (right - left - 32) / len(week)
    high = max(max(day.rain_mm for day in week), 1)
    for index, day in enumerate(week):
        x0 = left + 16 + index * slot + slot * 0.2
        x1 = left + 16 + (index + 1) * slot - slot * 0.2
        height = (chart_bottom - chart_top - 18) * day.rain_mm / high
        draw.rectangle([x0, chart_bottom - height, x1, chart_bottom], fill=RAIN)
        draw.text(
            ((x0 + x1) / 2, chart_bottom - height - 4),
            f"{ui.decimal(day.rain_mm, 1)}",
            font=ui.font(13),
            fill=ui.TEXT,
            anchor="ms",
        )
        draw.text(
            ((x0 + x1) / 2, bottom - 10),
            f"{WEEKDAYS_SHORT[day.day.weekday()]} {day.day:%d}",
            font=ui.font(13),
            fill=ui.DIM,
            anchor="ms",
        )


def draw_sun_moon(
    draw, box: tuple, today: Day | None, yesterday: Day | None, now: datetime
) -> None:
    """Sun on the left, moon text right of it with the drawn moon at the edge; in a narrow card
    the moon text goes under the sun text instead of beside it."""
    left, top, right, bottom = box
    ui.card(draw, box, "Sol e lua")
    moon = Moon.at(now)
    cx = right - 70
    draw_moon(draw, cx, (top + bottom) / 2 + 8, 44, moon)
    narrow = right - left < 800
    sun_size, moon_size = (17, 16) if narrow else (20, 18)
    if today is not None:
        hours, rest = divmod(int(today.length.total_seconds()), 3600)
        draw.text(
            (left + 16, top + 66),
            f"Nasce {today.sunrise:%H:%M} · põe {today.sunset:%H:%M}",
            font=ui.font(sun_size, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        change = ""
        if yesterday is not None:
            minutes = round((today.length - yesterday.length).total_seconds() / 60)
            change = f" ({'+' if minutes >= 0 else ''}{minutes} min que ontem)"
        draw.text(
            (left + 16, top + 92),
            f"Dia de {hours}h{rest // 60:02d}{change}",
            font=ui.font(15),
            fill=ui.DIM,
            anchor="ls",
        )
    full = (now + timedelta(days=moon.days_to_full())).astimezone()
    new = (now + timedelta(days=moon.days_to_new())).astimezone()
    moon_line = f"{moon.name} · {moon.illumination:.0%} iluminada"
    dates = f"cheia {full:%d/%m} · nova {new:%d/%m}"
    if narrow:
        draw.text(
            (left + 16, top + 128),
            moon_line,
            font=ui.font(moon_size, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        draw.text(
            (left + 16, top + 152), dates, font=ui.font(15), fill=ui.DIM, anchor="ls"
        )
    else:
        draw.text(
            (cx - 64, top + 66),
            moon_line,
            font=ui.font(moon_size, bold=True),
            fill=ui.TEXT,
            anchor="rs",
        )
        draw.text(
            (cx - 64, top + 92), dates, font=ui.font(16), fill=ui.DIM, anchor="rs"
        )


class WeatherPage(ui.Page):
    """Owns the cities of both weather blocks; Clima detalhes reads them from here."""

    name = "Clima"
    settings_fields = (
        fields.Field(
            "cidade",
            "Cidade principal",
            "texto",
            DEFAULT_PLACES.home.name,
            help="Se o nome existe em mais de um estado, escreva com a sigla: Bom Jesus, PI.",
        ),
        fields.Field(
            "latitude",
            "Latitude",
            "decimal",
            DEFAULT_PLACES.home.latitude,
            low=-90,
            high=90,
        ),
        fields.Field(
            "longitude",
            "Longitude",
            "decimal",
            DEFAULT_PLACES.home.longitude,
            low=-180,
            high=180,
        ),
        fields.Field(
            "codigo_ibge",
            "Código IBGE da cidade (alertas do INMET)",
            "texto",
            DEFAULT_PLACES.ibge,
            pattern=r"\d{7}",
        ),
        fields.Field("cidade_2", "Segunda cidade", "texto", DEFAULT_PLACES.other.name),
        fields.Field(
            "latitude_2",
            "Latitude da segunda",
            "decimal",
            DEFAULT_PLACES.other.latitude,
            low=-90,
            high=90,
        ),
        fields.Field(
            "longitude_2",
            "Longitude da segunda",
            "decimal",
            DEFAULT_PLACES.other.longitude,
            low=-180,
            high=180,
        ),
    )

    cities = (
        fields.CityFields("cidade", "latitude", "longitude", "codigo_ibge"),
        fields.CityFields("cidade_2", "latitude_2", "longitude_2"),
    )

    def __init__(self, fetch_weather=sources.fetch_weather) -> None:
        self.fetch_weather = fetch_weather
        self.places = DEFAULT_PLACES
        self.weather: Weather | None = None
        self.error = ""
        self.next_fetch_at: datetime | None = None

    def apply_settings(self, values: dict) -> None:
        places = Places(
            Place(values["cidade"], values["latitude"], values["longitude"]),
            values["codigo_ibge"],
            Place(values["cidade_2"], values["latitude_2"], values["longitude_2"]),
        )
        if places.home != self.places.home:  # another city: its forecast is read now
            self.weather, self.next_fetch_at = None, None
        self.places = places

    def refresh(self, now: datetime) -> None:
        if self.next_fetch_at is not None and now < self.next_fetch_at:
            return
        try:
            home = self.places.home
            self.weather, self.error = (
                self.fetch_weather(home.latitude, home.longitude),
                "",
            )
            self.next_fetch_at = now + REFRESH_EVERY
        except SourceError as err:
            self.error = f"Clima: {err}"
            self.next_fetch_at = now + RETRY_AFTER

    def problems(self) -> list[str]:
        return [self.error] if self.error else []

    def draw_compact(
        self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime
    ) -> None:
        """A narrow column beside the news: now, the next 12 hours, the next 5 days."""
        left, top, right, bottom = box
        weather = self.weather
        if weather is None:
            ui.card(draw, box, f"Clima em {self.places.home.name}")
            ui.placeholder(draw, box, self.error)
            return
        local_now = now.astimezone(weather.read_at.tzinfo)
        today = next((day for day in weather.days if day.day == local_now.date()), None)
        now_bottom = top + 300
        ui.card(
            draw, (left, top, right, now_bottom), f"Agora em {self.places.home.name}"
        )
        description, kind = condition(weather.code)
        draw_icon(draw, kind, left + 70, top + 118, 104)
        draw.text(
            (left + 140, top + 142),
            degrees(weather.temperature),
            font=ui.font(72, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        draw.text(
            (left + 16, top + 196),
            ui.fit(description, ui.font(20), right - left - 32),
            font=ui.font(20),
            fill=ui.TEXT,
            anchor="ls",
        )
        rows = [
            ("Sensação", degrees(weather.feels_like)),
            ("Umidade", f"{weather.humidity:.0f}%"),
            ("Vento", f"{weather.wind:.0f} km/h {compass(weather.wind_from)}"),
        ]
        if today is not None:
            rows.append(
                (
                    "Chuva hoje",
                    f"{today.rain_chance:.0f}% · {ui.decimal(today.rain_mm, 1)} mm",
                )
            )
        for index, (label, value) in enumerate(rows):
            y = top + 226 + index * 20
            draw.text((left + 16, y), label, font=ui.font(15), fill=ui.DIM, anchor="ls")
            draw.text(
                (right - 16, y),
                value,
                font=ui.font(15, bold=True),
                fill=ui.TEXT,
                anchor="rs",
            )
        hours_bottom = now_bottom + ui.GUTTER + 260
        hours = upcoming_hours(weather.hours, local_now)[:12]
        draw_hours(
            draw,
            (left, now_bottom + ui.GUTTER, right, hours_bottom),
            hours,
            compact=True,
        )
        days_box = (left, hours_bottom + ui.GUTTER, right, bottom)
        ui.card(draw, days_box, "Próximos 5 dias")
        days = weather.days[:5]
        row = (bottom - days_box[1] - 46) / max(len(days), 1)
        for index, day in enumerate(days):
            y = days_box[1] + 46 + index * row
            name = (
                "Hoje"
                if day.day == local_now.date()
                else WEEKDAYS_SHORT[day.day.weekday()]
            )
            draw.text(
                (left + 16, y + row / 2 + 6),
                name,
                font=ui.font(18, bold=True),
                fill=ui.TEXT,
                anchor="ls",
            )
            draw_icon(draw, condition(day.code)[1], left + 110, y + row / 2, 46)
            draw.text(
                (left + 150, y + row / 2 + 6),
                f"{degrees(day.high)} / {degrees(day.low)}",
                font=ui.font(18, bold=True),
                fill=ui.TEXT,
                anchor="ls",
            )
            draw.text(
                (right - 16, y + row / 2 + 6),
                f"{day.rain_chance:.0f}%",
                font=ui.font(17, bold=True),
                fill=RAIN,
                anchor="rs",
            )

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        left, top, right, bottom = box
        weather = self.weather
        if weather is None:
            ui.card(draw, box, f"Clima em {self.places.home.name}")
            ui.placeholder(draw, box, self.error)
            return
        local_now = now.astimezone(weather.read_at.tzinfo)
        today = next((day for day in weather.days if day.day == local_now.date()), None)
        yesterday = weather.past[-1] if weather.past else None
        # Full screen: 700 px for "Agora"; sharing the screen it takes at most 42% of the block.
        split, upper_bottom = left + min(700, round((right - left) * 0.42)), top + 470
        draw_now(
            draw,
            (left, top, split - ui.GUTTER, upper_bottom),
            weather,
            today,
            self.places.home.name,
        )
        draw_hours(
            draw,
            (split, top, right, upper_bottom),
            upcoming_hours(weather.hours, local_now),
        )
        days = weather.days[
            : max(5, min(16, int((right - left + 8) // 108)))
        ]  # as many ~100 px days as fit
        strip_top, strip_bottom = (
            upper_bottom + ui.GUTTER,
            upper_bottom + ui.GUTTER + 300,
        )
        gap = 8
        width = (right - left - (len(days) - 1) * gap) / max(len(days), 1)
        for index, day in enumerate(days):
            x = left + index * (width + gap)
            draw_day(
                draw, (x, strip_top, x + width, strip_bottom), day, local_now.date()
            )
        half = (right - left - ui.GUTTER) / 2
        draw_past_rain(
            draw, (left, strip_bottom + ui.GUTTER, left + half, bottom), weather.past
        )
        draw_sun_moon(
            draw,
            (left + half + ui.GUTTER, strip_bottom + ui.GUTTER, right, bottom),
            today,
            yesterday,
            now,
        )


def compose_map(
    center: tuple[float, float],
    size: tuple[int, int],
    frame: RadarFrame | None,
    fetch=sources.fetch_bytes,
    tile_dir: Path = TILE_DIR,
) -> Image.Image:
    """Dark basemap and city names (both cached on disk) around the newest radar frame, centered on `center`."""
    x, y = sources.tile_xy(center[0], center[1], RADAR_ZOOM)
    width, height = size
    origin_x, origin_y = x * TILE - width / 2, y * TILE - height / 2
    first_x, first_y = int(origin_x // TILE), int(origin_y // TILE)
    last_x, last_y = int((origin_x + width) // TILE), int((origin_y + height) // TILE)
    canvas = Image.new(
        "RGBA",
        ((last_x - first_x + 1) * TILE, (last_y - first_y + 1) * TILE),
        ui.BACKGROUND,
    )

    def tile(url: str, name: str) -> Image.Image:
        body = sources.cached_tile(
            url.format(z=RADAR_ZOOM, x=tile_x, y=tile_y),
            tile_dir / f"{name}-{RADAR_ZOOM}-{tile_x}-{tile_y}",
            fetch,
        )
        return Image.open(io.BytesIO(body)).convert("RGBA")

    for tile_x in range(first_x, last_x + 1):
        for tile_y in range(first_y, last_y + 1):
            spot = ((tile_x - first_x) * TILE, (tile_y - first_y) * TILE)
            canvas.paste(tile(sources.BASEMAP_TILE_URL, "esri-base"), spot)
            if frame is not None:
                radar = fetch(
                    sources.RADAR_TILE_URL.format(
                        host=frame.host,
                        path=frame.path,
                        z=RADAR_ZOOM,
                        x=tile_x,
                        y=tile_y,
                    )
                )
                layer = Image.open(io.BytesIO(radar)).convert("RGBA")
                layer.putalpha(layer.getchannel("A").point(lambda a: a * 85 // 100))
                canvas.alpha_composite(layer, spot)
            canvas.alpha_composite(tile(sources.LABELS_TILE_URL, "esri-labels"), spot)
    left, top = int(origin_x - first_x * TILE), int(origin_y - first_y * TILE)
    return canvas.crop((left, top, left + width, top + height)).convert("RGB")


def map_point(
    latitude: float,
    longitude: float,
    center: tuple[float, float],
    size: tuple[int, int],
):
    cx, cy = sources.tile_xy(center[0], center[1], RADAR_ZOOM)
    px, py = sources.tile_xy(latitude, longitude, RADAR_ZOOM)
    return size[0] / 2 + (px - cx) * TILE, size[1] / 2 + (py - cy) * TILE


def draw_alerts(
    draw,
    box: tuple,
    alerts: tuple[Alert, ...],
    error: str,
    now: datetime,
    city: str,
    compact: bool = False,
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, f"Alertas do INMET para {city}")
    if error and not alerts:
        ui.placeholder(draw, box, error)
        return
    if not alerts:
        draw.text(
            (left + 16, top + 80),
            "Nenhum alerta agora",
            font=ui.font(22, bold=True),
            fill=ui.GREEN,
            anchor="ls",
        )
        return
    today = now.astimezone().date()
    chip_width, row, face = (
        (150, 32, ui.font(16)) if compact else (174, 40, ui.font(18))
    )
    y = top + 74
    for alert in alerts:
        if y > bottom - 12:
            break
        color = SEVERITY_COLORS.get(alert.severity, ui.AMBER)
        draw.rounded_rectangle(
            (left + 16, y - 20, left + 16 + chip_width, y + 6),
            radius=8,
            fill=ui.blend(color, ui.CARD, 0.3),
        )
        draw.text(
            (left + 16 + chip_width / 2, y - 7),
            alert.severity,
            font=ui.font(14 if compact else 15, bold=True),
            fill=color,
            anchor="mm",
        )
        day = "hoje" if alert.start.date() == today else f"{alert.start:%d/%m}"
        text = f"{alert.kind} · {day} {alert.start:%H:%M} até {alert.end:%d/%m %H:%M}"
        text_left = left + 30 + chip_width
        draw.text(
            (text_left, y),
            ui.fit(text, face, right - 16 - text_left),
            font=face,
            fill=ui.TEXT,
            anchor="ls",
        )
        y += row


def draw_air(
    draw,
    box: tuple,
    air: Air | None,
    error: str,
    now: datetime,
    city: str,
    compact: bool = False,
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, f"Qualidade do ar em {city}")
    if air is None:
        ui.placeholder(draw, box, error)
        return
    name, color = air_band(air.aqi)
    details = (("PM2,5", air.pm25), ("PM10", air.pm10), ("Ozônio", air.ozone))
    if compact:
        draw.text(
            (left + 16, top + 92),
            f"{air.aqi:.0f}",
            font=ui.font(48, bold=True),
            fill=color,
            anchor="ls",
        )
        draw.text(
            (left + 16, top + 118),
            name,
            font=ui.font(16, bold=True),
            fill=color,
            anchor="ls",
        )
        line = " · ".join(f"{label} {value:.0f}" for label, value in details) + " µg/m³"
        draw.text(
            (left + 16, top + 144),
            ui.fit(line, ui.font(14), 260),
            font=ui.font(14),
            fill=ui.DIM,
            anchor="ls",
        )
        chart_left = left + 290
    else:
        draw.text(
            (left + 16, top + 110),
            f"{air.aqi:.0f}",
            font=ui.font(64, bold=True),
            fill=color,
            anchor="ls",
        )
        draw.text(
            (left + 16, top + 140),
            f"{name} (índice EUA)",
            font=ui.font(18, bold=True),
            fill=color,
            anchor="ls",
        )
        for index, (label, value) in enumerate(details):
            y = top + 182 + index * 30
            draw.text((left + 16, y), label, font=ui.font(16), fill=ui.DIM, anchor="ls")
            draw.text(
                (left + 110, y),
                f"{value:.0f} µg/m³",
                font=ui.font(16, bold=True),
                fill=ui.TEXT,
                anchor="ls",
            )
        chart_left = left + 300
    ahead = [value for time, value in air.hours if time >= now][:HOURS_AHEAD]
    if len(ahead) > 1:
        draw.text(
            (chart_left, top + 66),
            f"próximas {len(ahead)} horas",
            font=ui.font(14),
            fill=ui.DIM,
            anchor="ls",
        )
        ui.line_chart(
            draw,
            (chart_left, top + 76, right - 16, bottom - 16),
            tuple(ahead),
            color,
            fill=0.18,
        )


def draw_city(
    draw,
    x: float,
    top: float,
    width: float,
    name: str,
    weather: Weather | None,
    height: float = 260,
) -> None:
    """Below ~200 px tall (the compact column) the icon and text shrink and the wind line goes."""
    small = height < 200
    draw.text(
        (x, top + 30),
        name,
        font=ui.font(20 if small else 22, bold=True),
        fill=ui.TEXT,
        anchor="ls",
    )
    if weather is None:
        draw.text(
            (x, top + 64), "carregando…", font=ui.font(18), fill=ui.DIM, anchor="ls"
        )
        return
    description, kind = condition(weather.code)
    icon, temp_size, line = (40, 34, 20) if small else (80, 52, 30)
    draw_icon(draw, kind, x + icon / 2 + 4, top + 44 + icon / 2, icon)
    draw.text(
        (x + icon + 18, top + 56 + icon / 2),
        degrees(weather.temperature),
        font=ui.font(temp_size, bold=True),
        fill=ui.TEXT,
        anchor="ls",
    )
    y = top + (60 if small else 70) + icon + (0 if small else 24)
    draw.text(
        (x, y),
        ui.fit(description, ui.font(16 if small else 18), width),
        font=ui.font(16 if small else 18),
        fill=ui.TEXT,
        anchor="ls",
    )
    today = weather.days[0] if weather.days else None
    if today is not None:
        y += line
        draw.text(
            (x, y),
            ui.fit(
                f"{degrees(today.low)} a {degrees(today.high)} · chuva {today.rain_chance:.0f}%",
                ui.font(15 if small else 17),
                width,
            ),
            font=ui.font(15 if small else 17),
            fill=ui.DIM,
            anchor="ls",
        )
    if not small:
        draw.text(
            (x, y + line),
            f"vento {weather.wind:.0f} km/h · umidade {weather.humidity:.0f}%",
            font=ui.font(17),
            fill=ui.DIM,
            anchor="ls",
        )


class WeatherDetailsPage(ui.Page):
    name = "Clima detalhes"

    def __init__(
        self,
        main: WeatherPage,
        fetch_weather=sources.fetch_weather,
        fetch_air=sources.fetch_air,
        fetch_alerts=sources.fetch_alerts,
        fetch_radar_frame=sources.fetch_radar_frame,
        fetch=sources.fetch_bytes,
        tile_dir: Path = TILE_DIR,
    ) -> None:
        self.main = (
            main  # the home city's forecast comes from the Clima page, fetched once
        )
        self.fetch_weather, self.fetch_air, self.fetch_alerts = (
            fetch_weather,
            fetch_air,
            fetch_alerts,
        )
        self.fetch_radar_frame, self.fetch = fetch_radar_frame, fetch
        self.tile_dir = tile_dir
        self.second: Weather | None = None
        self.air: Air | None = None
        self.alerts: tuple[Alert, ...] = ()
        self.radar: RadarFrame | None = None
        self.map_image: Image.Image | None = None
        self.map_size = (1100, 992)
        self.compact = False
        self.errors: dict[str, str] = {}
        self.due: dict[str, datetime] = {}
        self.places = main.places

    @property
    def _changed_places(self) -> bool:
        return self.places != self.main.places

    def _due(self, key: str, now: datetime) -> bool:
        return key not in self.due or now >= self.due[key]

    def _run(self, key: str, now: datetime, every: timedelta, action) -> None:
        if not self._due(key, now):
            return
        try:
            action()
            self.errors = {k: v for k, v in self.errors.items() if k != key}
            self.due = {**self.due, key: now + every}
        except SourceError as err:
            self.errors = {**self.errors, key: str(err)}
            self.due = {**self.due, key: now + RETRY_AFTER}

    def refresh(self, now: datetime) -> None:
        if (
            self._changed_places
        ):  # new cities on the settings page: drop the old ones' data
            self.places, self.due, self.errors = self.main.places, {}, {}
            self.second, self.air, self.alerts = None, None, ()
            self.radar, self.map_image = None, None
        self._run("campinas", now, REFRESH_EVERY, self._read_second)
        self._run("ar", now, AIR_REFRESH, self._read_air)
        self._run("alertas", now, REFRESH_EVERY, self._read_alerts)
        self._run("radar", now, RADAR_REFRESH, self._read_radar)

    def _read_second(self) -> None:
        self.second = self.fetch_weather(
            self.places.other.latitude, self.places.other.longitude
        )

    def _read_air(self) -> None:
        self.air = self.fetch_air(self.places.home.latitude, self.places.home.longitude)

    def _read_alerts(self) -> None:
        self.alerts = self.fetch_alerts(self.places.ibge)

    def _read_radar(self) -> None:
        frame = self.fetch_radar_frame()
        try:
            image = compose_map(
                (self.places.home.latitude, self.places.home.longitude),
                self.map_size,
                frame,
                self.fetch,
                self.tile_dir,
            )
        except OSError as err:  # a tile that is not a picture
            raise SourceError("imagem do radar inválida") from err
        self.radar, self.map_image = frame, image

    def problems(self) -> list[str]:
        return [f"{key}: {error}" for key, error in self.errors.items()]

    def layout(self, box: tuple, mode: str) -> None:
        """Full: the map takes what the side column leaves. Compact: the map is the top half of a
        narrow column. The map is rebuilt at this size on the next radar refresh."""
        width, height = box[2] - box[0], box[3] - box[1]
        self.compact = mode == "compacto"
        if self.compact:
            self.map_size = (width, round(height * COMPACT_MAP_SHARE))
        else:
            self.map_size = (max(width - SIDE_COLUMN - ui.GUTTER, 300), height)

    def compose(
        self, image: Image.Image, box: tuple, now: datetime, mode: str = "cheio"
    ) -> None:
        left, top = box[0], box[1]
        if self.map_image is not None:
            image.paste(self.map_image, (int(left), int(top)))
        draw = ImageDraw.Draw(image)
        if mode == "compacto":
            self.draw_compact(draw, box, now)
        else:
            self.draw(draw, box, now)

    def _draw_map(self, draw, box: tuple, now: datetime) -> None:
        if self.map_image is None:
            ui.card(draw, box, "Radar de chuva")
            ui.placeholder(draw, box, self.errors.get("radar", ""))
        else:
            self._draw_map_labels(draw, (box[0], box[1]), now)

    def _draw_cities(self, draw, box: tuple) -> None:
        left, top, right, bottom = box
        ui.card(draw, box)
        half = (right - left) / 2
        draw_city(
            draw,
            left + 16,
            top,
            half - 32,
            self.places.home.name,
            self.main.weather,
            bottom - top,
        )
        draw_city(
            draw,
            left + half + 8,
            top,
            half - 32,
            self.places.other.name,
            self.second,
            bottom - top,
        )

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        left, top, right, bottom = box
        map_right = left + self.map_size[0]
        self._draw_map(draw, (left, top, map_right, bottom), now)
        column = map_right + ui.GUTTER
        draw_alerts(
            draw,
            (column, top, right, top + 250),
            self.alerts,
            self.errors.get("alertas", ""),
            now,
            self.places.home.name,
        )
        draw_air(
            draw,
            (column, top + 262, right, top + 562),
            self.air,
            self.errors.get("ar", ""),
            now,
            self.places.home.name,
        )
        self._draw_cities(draw, (column, top + 574, right, bottom))

    def draw_compact(
        self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime
    ) -> None:
        """A narrow column: radar on top, then alerts, air quality and the two cities."""
        left, top, right, bottom = box
        map_bottom = top + self.map_size[1]
        self._draw_map(draw, (left, top, right, map_bottom), now)
        rest = bottom - map_bottom - 2 * ui.GUTTER
        alerts_bottom = map_bottom + ui.GUTTER + round(rest * 0.36)
        draw_alerts(
            draw,
            (left, map_bottom + ui.GUTTER, right, alerts_bottom),
            self.alerts,
            self.errors.get("alertas", ""),
            now,
            self.places.home.name,
            compact=True,
        )
        air_bottom = alerts_bottom + ui.GUTTER + round(rest * 0.32)
        draw_air(
            draw,
            (left, alerts_bottom + ui.GUTTER, right, air_bottom),
            self.air,
            self.errors.get("ar", ""),
            now,
            self.places.home.name,
            compact=True,
        )
        self._draw_cities(draw, (left, air_bottom + ui.GUTTER, right, bottom))

    def _draw_map_labels(self, draw, origin: tuple, now: datetime) -> None:
        home = self.places.home
        for place in (home, self.places.other):
            px, py = map_point(
                place.latitude,
                place.longitude,
                (home.latitude, home.longitude),
                self.map_size,
            )
            x, y = origin[0] + px, origin[1] + py
            draw.ellipse(
                [x - 6, y - 6, x + 6, y + 6],
                fill=ui.AMBER,
                outline=ui.BACKGROUND,
                width=2,
            )
            if place == home:  # Esri labels big cities itself, not small ones
                draw.text(
                    (x + 10, y + 5),
                    place.name,
                    font=ui.font(16, bold=True),
                    fill=ui.TEXT,
                    anchor="ls",
                )
        draw.rounded_rectangle(
            (origin[0] + 12, origin[1] + 12, origin[0] + 330, origin[1] + 50),
            radius=8,
            fill=ui.CARD,
        )
        stamp = sources.clock(self.radar.time, now) if self.radar else "-"
        draw.text(
            (origin[0] + 24, origin[1] + 38),
            f"Radar de chuva · {stamp}",
            font=ui.font(18, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        draw.text(
            (origin[0] + 12, origin[1] + self.map_size[1] - 10),
            "radar RainViewer · mapa Esri, HERE, Garmin, © OpenStreetMap",
            font=ui.font(12),
            fill=ui.DIM,
            anchor="ls",
        )


def sample_weather(now: datetime) -> Weather:
    hours = tuple(
        Hour(
            now.replace(minute=0) + timedelta(hours=i - 2),
            18 + 4 * math.sin(i / 4),
            (i * 7) % 100,
        )
        for i in range(48)
    )
    codes = (0, 2, 3, 45, 63, 95, 71, 80, 1, 2, 3, 61, 0, 95, 2, 3)
    days = tuple(
        Day(
            now.date() + timedelta(days=i),
            code,
            25 + i % 4,
            15 + i % 3,
            10 * (i % 10),
            1.5 * i,
            now.replace(hour=5, minute=28),
            now.replace(hour=18, minute=15 + i % 3),
            4.75,
        )
        for i, code in enumerate(codes)
    )
    past = tuple(
        Day(
            now.date() - timedelta(days=7 - i),
            61,
            24,
            16,
            80,
            [20.7, 5.2, 0.1, 0, 0, 0, 12.4][i],
            now.replace(hour=5, minute=30),
            now.replace(hour=18, minute=12),
            3,
        )
        for i in range(7)
    )
    return Weather(
        now,
        19.9,
        21.7,
        88,
        0.6,
        80,
        6.2,
        32.8,
        52,
        1018.2,
        2.2,
        99,
        3.68,
        17.9,
        hours,
        past,
        days,
    )


def selftest() -> Image.Image:
    zone = timezone(timedelta(hours=-3))
    now = datetime(2026, 10, 23, 9, 50, tzinfo=zone)
    page = WeatherPage()
    page.weather = sample_weather(now)
    upcoming = upcoming_hours(page.weather.hours, now)
    assert len(upcoming) == HOURS_AHEAD and upcoming[0].time == now.replace(minute=0)
    assert condition(95) == ("Trovoada", "storm") and condition(1234)[1] == "cloud"
    assert compass(52) == "NE" and compass(350) == "N" and compass(180) == "S"
    assert (
        pressure_trend(2.2) == "subindo"
        and pressure_trend(-1.5) == "caindo"
        and pressure_trend(0.3) == "estável"
    )
    assert air_band(42) == ("Boa", AIR_BANDS[0][2]) and air_band(151)[0] == "Ruim"
    full = Moon.at(
        datetime(2024, 1, 25, 17, 54, tzinfo=timezone.utc)
    )  # a published full moon
    assert full.name == "Lua cheia" and full.illumination > 0.99, (
        full.name,
        full.illumination,
    )
    new = Moon.at(
        datetime(2024, 2, 9, 22, 59, tzinfo=timezone.utc)
    )  # a published new moon
    assert new.name == "Lua nova" and new.illumination < 0.01, (
        new.name,
        new.illumination,
    )

    def down(*args):
        raise SourceError("sem conexao")

    failing = WeatherPage(down)
    failing.refresh(now)
    assert failing.next_fetch_at == now + RETRY_AFTER and failing.problems() == [
        "Clima: sem conexao"
    ]
    working = WeatherPage(lambda latitude, longitude: page.weather)
    working.refresh(now)
    assert (
        working.next_fetch_at == now + REFRESH_EVERY and working.weather is page.weather
    )

    image = Image.new("RGB", (1888, 992), ui.BACKGROUND)
    page.compose(image, (0, 0, 1888, 992), now)
    empty = WeatherPage()
    empty.error = "Clima: sem conexao"
    empty.compose(Image.new("RGB", (1888, 992)), (0, 0, 1888, 992), now)
    narrow = Image.new("RGB", (460, 992), ui.BACKGROUND)
    page.compose(narrow, (0, 0, 460, 992), now, "compacto")
    assert narrow.getpixel((230, 150)) != ui.BACKGROUND, "the compact column is drawn"
    empty.compose(Image.new("RGB", (460, 992)), (0, 0, 460, 992), now, "compacto")
    print("page_weather selftest ok")
    return image


def selftest_details() -> Image.Image:
    zone = timezone(timedelta(hours=-3))
    now = datetime(2026, 10, 23, 9, 50, tzinfo=zone)
    main = WeatherPage()
    main.weather = sample_weather(now)
    tile = io.BytesIO()
    Image.new("RGBA", (TILE, TILE), (30, 40, 50, 255)).save(tile, "PNG")
    rain = io.BytesIO()
    Image.new("RGBA", (TILE, TILE), (88, 166, 255, 200)).save(rain, "PNG")
    urls = []

    labels = io.BytesIO()
    Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0)).save(
        labels, "PNG"
    )  # the real label layer is transparent

    def fake_fetch(url: str) -> bytes:
        urls.append(url)
        if "rainviewer" in url:
            return rain.getvalue()
        return labels.getvalue() if "Reference" in url else tile.getvalue()

    alerts = (
        Alert("Tempestade", "Perigo", now, now + timedelta(hours=14)),
        Alert(
            "Tempestade",
            "Perigo Potencial",
            now + timedelta(days=1),
            now + timedelta(days=1, hours=23),
        ),
    )
    air = Air(
        42,
        9.1,
        15.0,
        60.0,
        tuple((now + timedelta(hours=i), 30 + i) for i in range(24)),
    )
    frame = RadarFrame(now, "https://tilecache.rainviewer.com", "/v2/radar/x")
    with tempfile.TemporaryDirectory() as tmp:
        page = WeatherDetailsPage(
            main,
            lambda lat, lon: sample_weather(now),
            lambda lat, lon: air,
            lambda code: alerts,
            lambda: frame,
            fake_fetch,
            Path(tmp),
        )
        page.refresh(now)
        first_basemap = sum("arcgisonline" in u for u in urls)
        page.due = {}
        page.refresh(now + timedelta(minutes=11))
        assert (
            first_basemap and sum("arcgisonline" in u for u in urls) == first_basemap
        ), "map tiles come from disk"
    assert (
        not page.problems()
        and page.map_image is not None
        and page.map_image.size == page.map_size
    )
    assert page.map_image.getpixel((550, 496)) != (
        30,
        40,
        50,
    ), "radar drawn over the map"

    def down(*args):
        raise SourceError("sem conexao")

    failing = WeatherDetailsPage(
        main, down, down, down, down, fake_fetch, Path(tempfile.gettempdir())
    )
    failing.refresh(now)
    assert set(failing.errors) == {"campinas", "ar", "alertas", "radar"}
    assert all(due == now + RETRY_AFTER for due in failing.due.values())

    asked = []
    moved_main = WeatherPage(
        lambda lat, lon: asked.append(("main", lat, lon)) or sample_weather(now)
    )
    moved = WeatherDetailsPage(
        moved_main,
        lambda lat, lon: asked.append(("second", lat, lon)) or sample_weather(now),
        lambda lat, lon: air,
        lambda code: asked.append(("alerts", code)) or (),
        down,
        fake_fetch,
        Path(tempfile.gettempdir()),
    )
    moved.refresh(now)
    moved_main.refresh(now)
    asked.clear()
    moved_main.apply_settings(
        fields.read_all(
            WeatherPage.settings_fields,
            {
                "cidade": "Jundiaí",
                "latitude": -23.186,
                "longitude": -46.884,
                "codigo_ibge": "3525904",
            },
            "blocos.clima",
        )
    )
    assert moved_main.weather is None and moved_main.next_fetch_at is None
    moved_main.refresh(now)
    moved.refresh(now)
    assert ("main", -23.186, -46.884) in asked and ("alerts", "3525904") in asked, asked
    assert ("second", -22.90556, -47.06083) in asked, (
        "the details block reloads for the new cities"
    )

    image = Image.new("RGB", (1888, 992), ui.BACKGROUND)
    page.compose(image, (0, 0, 1888, 992), now)
    failing.compose(Image.new("RGB", (1888, 992)), (0, 0, 1888, 992), now)
    page.layout((0, 0, 748, 992), "compacto")
    assert page.map_size == (748, 496) and page.compact
    column = Image.new("RGB", (748, 992), ui.BACKGROUND)
    page.compose(column, (0, 0, 748, 992), now, "compacto")
    print("page_weather details selftest ok")
    return image

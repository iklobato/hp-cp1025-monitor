"""Interest rates and economy, no keys.

Every daily figure shows how it moved in 1d, 1w, 1m and 1y (a caption and a small chart per
window); monthly figures (IPCA) only have 1m and 1y, weekly ones (Focus) 1w, 1m and 1y.
Full screen: the table, the Tesouro yield curve, credit rates and other indicators.
Compact: the eight main lines of the table, to share a screen with the world markets.

Sources: Banco Central SGS (Selic, CDI, PTAX, IPCA, IPCA-15, IGP-M, INPC, savings, TR), its
Focus survey and weekly credit-rate survey, IBGE SIDRA (unemployment, GDP), Tesouro Direto's
price file, and Yahoo (commercial dollar, US Treasury yields).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import prod

import sources
import ui
from PIL import Image, ImageDraw
from sources import RATE_PERIODS, Credit, Series, SourceError, Tesouro

REFRESH_EVERY = timedelta(hours=6)
SLOW_REFRESH = timedelta(hours=12)  # Tesouro file and credit survey
RETRY_AFTER = timedelta(minutes=5)
HISTORY_DAYS = 400  # a year of windows plus room for the point before the year started
MONTH_ABBREVIATIONS = (
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
)
WINDOWS = (("1d", 1), ("1w", 7), ("1m", 30), ("1y", 365))
SGS = {  # key -> (series number, days of history)
    "selic": (432, HISTORY_DAYS),
    "cdi": (4389, HISTORY_DAYS),
    "ptax": (1, HISTORY_DAYS),
    "ipca_month": (433, 760),
    "ipca_year": (13522, 760),
    "ipca15": (7478, 760),
    "igpm": (189, 760),
    "inpc": (188, 760),
    "savings": (196, 760),
    "tr": (7811, 760),
}
SIDRA = {
    "jobless": "t/6381/n1/all/v/4099/p/last%2012",  # PNAD: unemployment, rolling quarter
    "gdp": "t/5932/n1/all/v/6561/p/last%208/c11255/90707",  # GDP, quarter vs same quarter a year before
}
YAHOO = (
    ("BRL=X", "Dólar comercial"),
    ("^IRX", "EUA 3 meses"),
    ("^FVX", "EUA 5 anos"),
    ("^TNX", "EUA 10 anos"),
    ("^TYX", "EUA 30 anos"),
)
FOCUS_INDICATORS = ("Selic", "IPCA", "PIB Total", "Câmbio")
CREDIT_TYPES = (  # survey name -> short label
    ("Cartão de crédito - rotativo total - Prefixado", "Cartão rotativo"),
    ("Cartão de crédito - parcelado - Prefixado", "Cartão parcelado"),
    ("Cheque especial - Prefixado", "Cheque especial"),
    ("Crédito pessoal não consignado - Prefixado", "Empréstimo pessoal"),
    ("Crédito pessoal consignado INSS - Prefixado", "Consignado INSS"),
    ("Crédito pessoal consignado privado - Prefixado", "Consignado privado"),
    ("Aquisição de veículos - Prefixado", "Veículos"),
)
COMPACT_ROWS = (
    "Selic",
    "CDI",
    "Dólar comercial",
    "Dólar PTAX",
    "EUA 10 anos",
    "IPCA 12 meses",
    "Focus: Selic",
    "IPCA+",
)
# J23: the only free daily series (IPEA, JPM366_EMBI366) stopped on 2024-07-30; shown as a note, not a number.
RISK_NOTE = "Risco-Brasil (EMBI): a série pública do IPEA parou em 30/07/2024"

Points = tuple[tuple[date, float], ...]


def percent(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}%".replace(".", ",")


def signed(value: float, unit: str) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}".replace(".", ",") + f" {unit}".rstrip()


def month_label(day: date) -> str:
    return f"{MONTH_ABBREVIATIONS[day.month - 1]}/{day:%y}"


def compound(monthly: Points, months: int = 12) -> float:
    """Accumulated % over the last `months` monthly % values."""
    return (prod(1 + value / 100 for _, value in monthly[-months:]) - 1) * 100


def real_rate(nominal: float, inflation: float) -> float:
    return ((1 + nominal / 100) / (1 + inflation / 100) - 1) * 100


@dataclass(frozen=True)
class Window:
    label: str
    change: (
        float | None
    )  # None: this window does not exist for the figure (IPCA has no 1d)
    unit: str
    points: tuple[float, ...]

    @property
    def caption(self) -> str:
        return (
            f"{self.label} —"
            if self.change is None
            else f"{self.label} {signed(self.change, self.unit)}"
        )


@dataclass(frozen=True)
class Row:
    label: str
    value: str
    note: str
    windows: tuple[Window, ...]


def window_slice(points: Points, days: int) -> Points:
    """The points of the last `days`, starting at the last point on or before the window opens."""
    if not points:
        return ()
    start = points[-1][0] - timedelta(days=days)
    first = max(
        (index for index, (day, _) in enumerate(points) if day <= start), default=0
    )
    return points[first:]


def daily_row(
    label: str,
    points: Points,
    value_text,
    unit: str,
    relative: bool = False,
    windows: tuple = WINDOWS,
) -> Row:
    """`relative`: change in % of the starting value (currencies) instead of the difference."""
    built = []
    for name, days in WINDOWS:
        part = window_slice(points, days) if any(name == w for w, _ in windows) else ()
        if len(part) < 2:
            built.append(Window(name, None, unit, ()))
            continue
        first, last = part[0][1], part[-1][1]
        change = (last / first - 1) * 100 if relative else last - first
        built.append(
            Window(name, change, "%" if relative else unit, tuple(v for _, v in part))
        )
    last_day, last_value = points[-1]
    return Row(label, value_text(last_value), f"em {last_day:%d/%m}", tuple(built))


def series_row(
    label: str, series: list, value_text, unit: str, relative: bool
) -> Row | None:
    """Yahoo quotes: one Series per window (1d, 1w, 1m, 1y)."""
    if not series or any(not isinstance(s, Series) for s in series):
        return None
    windows = tuple(
        Window(
            period.label,
            s.change * 100 if relative else s.price - s.previous_close,
            "%" if relative else unit,
            s.path,
        )
        for period, s in zip(RATE_PERIODS, series)
    )
    stamp = sources.clock(series[0].traded_at, datetime.now(timezone.utc))
    return Row(
        label,
        value_text(series[0].price),
        f"às {stamp}" if len(stamp) <= 5 else f"em {stamp}",
        windows,
    )


def tesouro_pick(
    tesouro: Tesouro, kind: str, years_ahead: int
) -> tuple[date, Points] | None:
    """The title of `kind` whose maturity is closest to `years_ahead` years from its newest day."""
    target = tesouro.day.year + years_ahead
    titles = [
        (maturity, points)
        for (k, maturity), points in tesouro.history.items()
        if k == kind and points
    ]
    if not titles:
        return None
    return min(titles, key=lambda item: abs(item[0].year - target))


def build_rows(
    series: dict[str, Points],
    yahoo: dict[str, list],
    focus: dict,
    tesouro: Tesouro | None,
) -> list[Row]:
    rows = []
    if "selic" in series:
        rows.append(daily_row("Selic", series["selic"], percent, "p.p."))
    if "cdi" in series:
        rows.append(daily_row("CDI", series["cdi"], percent, "p.p."))
    dollar = series_row(
        "Dólar comercial",
        yahoo.get("BRL=X", []),
        lambda v: "R$ " + f"{v:.4f}".replace(".", ","),
        "",
        relative=True,
    )
    if dollar:
        rows.append(dollar)
    if "ptax" in series:
        rows.append(
            daily_row(
                "Dólar PTAX",
                series["ptax"],
                lambda v: "R$ " + f"{v:.4f}".replace(".", ","),
                "",
                relative=True,
            )
        )
    for symbol, label in YAHOO[1:]:
        row = series_row(label, yahoo.get(symbol, []), percent, "p.p.", relative=False)
        if row:
            rows.append(row)
    monthly = (("1m", 30), ("1y", 365))
    if "ipca_year" in series:
        rows.append(
            daily_row(
                "IPCA 12 meses", series["ipca_year"], percent, "p.p.", windows=monthly
            )
        )
    if "ipca15" in series:
        ipca15 = series["ipca15"]
        accumulated = tuple(
            (ipca15[i][0], compound(ipca15[: i + 1])) for i in range(11, len(ipca15))
        )
        if accumulated:
            rows.append(
                daily_row(
                    "IPCA-15 12 meses", accumulated, percent, "p.p.", windows=monthly
                )
            )
    weekly = (("1w", 7), ("1m", 30), ("1y", 365))
    this_year = str(date.today().year)
    for indicator, label, unit in (
        ("Selic", "Focus: Selic", "p.p."),
        ("IPCA", "Focus: IPCA", "p.p."),
    ):
        points = focus.get((indicator, this_year))
        if points:
            row = daily_row(label, points, percent, unit, windows=weekly)
            rows.append(Row(row.label, row.value, f"fim de {this_year}", row.windows))
    if tesouro is not None:
        for kind, years, label, text in (
            ("Tesouro Prefixado", 5, "Prefixado", lambda v: percent(v)),
            ("Tesouro IPCA+", 9, "IPCA+", lambda v: percent(v)),
        ):
            picked = tesouro_pick(tesouro, kind, years)
            if picked:
                maturity, points = picked
                row = daily_row(label, points, text, "p.p.")
                note = "IPCA + taxa" if kind == "Tesouro IPCA+" else "taxa ao ano"
                rows.append(
                    Row(
                        f"{label} {maturity:%Y}",
                        row.value,
                        f"{note} · {points[-1][0]:%d/%m}",
                        row.windows,
                    )
                )
    return rows


def draw_rows(draw, box: tuple, title: str, rows: list[Row]) -> None:
    """Name, value and four captioned mini charts (1d, 1w, 1m, 1y) per line."""
    left, top, right, bottom = box
    ui.card(draw, box, title)
    if not rows:
        draw.text(
            (left + 16, top + 80),
            "carregando…",
            font=ui.font(20),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    height = (bottom - top - 46) / len(rows)
    value_right = left + 330
    gap = 12
    cell = (right - 16 - (value_right + 16) - 3 * gap) / 4
    caption_font = ui.font(13, bold=True, condensed=True)
    for index, row in enumerate(rows):
        y = top + 46 + index * height
        ui.stripe(draw, (left + 8, y, right - 8, y + height - 2), index)
        draw.text(
            (left + 16, y + height / 2 - 2),
            ui.fit(row.label, ui.font(17, bold=True), 200),
            font=ui.font(17, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        draw.text(
            (left + 16, y + height / 2 + 18),
            row.note,
            font=ui.font(13),
            fill=ui.DIM,
            anchor="ls",
        )
        draw.text(
            (value_right, y + height / 2 + 6),
            row.value,
            font=ui.font(21, bold=True),
            fill=ui.TEXT,
            anchor="rs",
        )
        for column, window in enumerate(row.windows):
            x = value_right + 16 + column * (cell + gap)
            color = (
                ui.DIM
                if window.change is None
                else ui.trend_color(round(window.change, 4))
            )
            draw.text(
                (x, y + 16), window.caption, font=caption_font, fill=color, anchor="ls"
            )
            if len(window.points) > 1:
                ui.line_chart(
                    draw,
                    (x, y + 20, x + cell, y + height - 6),
                    window.points,
                    color,
                    fill=0.18,
                )


def draw_curve(draw, box: tuple, tesouro: Tesouro | None, error: str) -> None:
    """Buying rate by years to maturity, today and a year before, for Prefixado and IPCA+."""
    left, top, right, bottom = box
    ui.card(draw, box, "Curva do Tesouro")
    if tesouro is None:
        draw.text(
            (left + 16, top + 80),
            error or "carregando…",
            font=ui.font(18),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    draw.text(
        (right - 16, top + 30),
        f"preços de {tesouro.day:%d/%m/%Y}",
        font=ui.font(14),
        fill=ui.DIM,
        anchor="rs",
    )
    year_ago = tesouro.day - timedelta(days=365)
    curves = []
    for kind, color in (("Tesouro Prefixado", ui.AMBER), ("Tesouro IPCA+", ui.BLUE)):
        now_points, old_points = [], []
        for (k, maturity), points in tesouro.history.items():
            if k != kind or not points or points[-1][0] != tesouro.day:
                continue
            years = (maturity - tesouro.day).days / 365.25
            now_points.append((years, points[-1][1]))
            old = [v for d, v in points if d <= year_ago]
            if old:
                old_points.append((years, old[-1]))
        curves.append((kind, color, sorted(now_points), sorted(old_points)))
    every = [v for _, _, a, b in curves for _, v in a + b]
    every_years = [y for _, _, a, b in curves for y, _ in a + b]
    if not every:
        return
    chart = (left + 60, top + 60, right - 20, bottom - 50)
    low, high = min(every) - 0.5, max(every) + 0.5
    span_years = max(every_years) or 1

    def xy(years: float, value: float) -> tuple[float, float]:
        return (
            chart[0] + years / span_years * (chart[2] - chart[0]),
            chart[3] - (value - low) / (high - low) * (chart[3] - chart[1]),
        )

    for value in (low + 0.5, (low + high) / 2, high - 0.5):
        y = xy(0, value)[1]
        draw.line([(chart[0], y), (chart[2], y)], fill=ui.GRID, width=1)
        draw.text(
            (chart[0] - 8, y + 5),
            percent(value, 1),
            font=ui.font(13),
            fill=ui.DIM,
            anchor="rs",
        )
    for kind, color, now_points, old_points in curves:
        if len(old_points) > 1:
            for start, end in zip(old_points, old_points[1:]):
                a, b = xy(*start), xy(*end)
                draw.line([a, b], fill=ui.blend(color, ui.CARD, 0.45), width=2)
        if len(now_points) > 1:
            draw.line([xy(*p) for p in now_points], fill=color, width=3)
        for point in now_points:
            x, y = xy(*point)
            draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=color)
    for years in range(0, int(span_years) + 1, 5):
        x = xy(years, low)[0]
        draw.text(
            (x, bottom - 30), f"{years}a", font=ui.font(13), fill=ui.DIM, anchor="ms"
        )
    legend = [
        ("Prefixado hoje", ui.AMBER),
        ("IPCA+ hoje (+ IPCA)", ui.BLUE),
        ("linha clara: há 1 ano", ui.DIM),
    ]
    x = left + 16
    for text, color in legend:
        draw.text(
            (x, bottom - 10), text, font=ui.font(13, bold=True), fill=color, anchor="ls"
        )
        x += ui.font(13, bold=True).getlength(text) + 22


def draw_credit(draw, box: tuple, credit: Credit | None, error: str) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Juros do crédito (pessoa física)")
    if credit is None:
        draw.text(
            (left + 16, top + 80),
            error or "carregando…",
            font=ui.font(18),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    draw.text(
        (right - 16, top + 30),
        f"mediana dos bancos · semana de {credit.week:%d/%m}",
        font=ui.font(13),
        fill=ui.DIM,
        anchor="rs",
    )
    rows = [(label, name) for name, label in CREDIT_TYPES if name in credit.now]
    if not rows:
        return
    height = (bottom - top - 66) / len(rows)
    columns = (right - 270, right - 140, right - 16)
    for header, x in zip(("ao ano", "em 1 mês", "em 1 ano"), columns):
        draw.text((x, top + 58), header, font=ui.font(13), fill=ui.DIM, anchor="rs")
    for index, (label, name) in enumerate(rows):
        band = top + 66 + index * height
        ui.stripe(draw, (left + 8, band + 1, right - 8, band + height - 1), index)
        y = top + 66 + (index + 0.7) * height
        now = credit.now[name]
        draw.text(
            (left + 16, y),
            label,
            font=ui.font(16, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        draw.text(
            (columns[0], y),
            percent(now, 1),
            font=ui.font(17, bold=True),
            fill=ui.TEXT,
            anchor="rs",
        )
        for before, x in (
            (credit.month_ago.get(name), columns[1]),
            (credit.year_ago.get(name), columns[2]),
        ):
            if before is None:
                continue
            change = now - before
            draw.text(
                (x, y),
                signed(change, "p.p."),
                font=ui.font(15),
                fill=ui.trend_color(round(change, 2)),
                anchor="rs",
            )


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    note: str


def build_metrics(series: dict[str, Points], sidra: dict, focus: dict) -> list[Metric]:
    metrics = []
    if "selic" in series and "ipca_year" in series:
        selic, inflation = series["selic"][-1][1], series["ipca_year"][-1][1]
        metrics.append(
            Metric(
                "Juro real",
                percent(real_rate(selic, inflation)),
                "Selic menos a inflação de 12 meses",
            )
        )
    for key, label in (("igpm", "IGP-M"), ("inpc", "INPC")):
        if key in series:
            monthly = series[key]
            metrics.append(
                Metric(
                    label,
                    percent(compound(monthly)),
                    f"12 meses · {month_label(monthly[-1][0])}: {percent(monthly[-1][1])}",
                )
            )
    if "savings" in series:
        monthly = series["savings"][-1][1]
        metrics.append(
            Metric(
                "Poupança",
                percent(monthly) + " ao mês",
                f"cerca de {percent(((1 + monthly / 100) ** 12 - 1) * 100)} ao ano",
            )
        )
    if "tr" in series:
        metrics.append(
            Metric(
                "TR",
                percent(series["tr"][-1][1]) + " ao mês",
                month_label(series["tr"][-1][0]),
            )
        )
    if "jobless" in sidra:
        period, value = sidra["jobless"][-1]
        metrics.append(Metric("Desemprego", percent(value), period))
    if "gdp" in sidra:
        period, value = sidra["gdp"][-1]
        metrics.append(
            Metric(
                "PIB",
                signed(value, "%").replace(" %", "%"),
                f"{period}, contra o ano anterior",
            )
        )
    this_year = str(date.today().year)
    for indicator, label, text in (
        ("PIB Total", "Focus: PIB", lambda v: percent(v)),
        ("Câmbio", "Focus: dólar", lambda v: "R$ " + f"{v:.2f}".replace(".", ",")),
    ):
        points = focus.get((indicator, this_year))
        if points:
            metrics.append(
                Metric(
                    label, text(points[-1][1]), f"esperado para o fim de {this_year}"
                )
            )
    return metrics


def draw_metrics(draw, box: tuple, metrics: list[Metric]) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, "Mais indicadores")
    half = (right - left) / 2
    rows = -(-len(metrics) // 2) or 1
    height = (bottom - top - 76) / rows
    for index, metric in enumerate(metrics):
        column, row = index % 2, index // 2
        x = left + 16 + column * half
        y = top + 46 + row * height
        # One tile per metric: two columns of label ... value would pair a value with the next label.
        draw.rounded_rectangle(
            (x - 8, y + 2, x + half - 24, y + height - 4), radius=6, fill=ui.STRIPE
        )
        draw.text((x, y + 20), metric.label, font=ui.font(15), fill=ui.DIM, anchor="ls")
        draw.text(
            (x + half - 32, y + 20),
            metric.value,
            font=ui.font(18, bold=True),
            fill=ui.TEXT,
            anchor="rs",
        )
        draw.text(
            (x, y + 38),
            ui.fit(metric.note, ui.font(12), half - 40),
            font=ui.font(12),
            fill=ui.DIM,
            anchor="ls",
        )
    draw.text(
        (left + 16, bottom - 12), RISK_NOTE, font=ui.font(12), fill=ui.DIM, anchor="ls"
    )


class RatesPage(ui.Page):
    name = "Juros"

    def __init__(
        self,
        fetch_sgs=sources.fetch_sgs,
        fetch_sidra=sources.fetch_sidra,
        fetch_tesouro=sources.fetch_tesouro,
        fetch_focus=sources.fetch_focus,
        fetch_credit=sources.fetch_credit,
        fetch_series=sources.fetch_series,
    ) -> None:
        self.fetch_sgs, self.fetch_sidra, self.fetch_tesouro = (
            fetch_sgs,
            fetch_sidra,
            fetch_tesouro,
        )
        self.fetch_focus, self.fetch_credit, self.fetch_series = (
            fetch_focus,
            fetch_credit,
            fetch_series,
        )
        self.series: dict[str, Points] = {}
        self.sidra: dict[str, tuple] = {}
        self.yahoo: dict[str, list] = {}
        self.focus: dict = {}
        self.credit: Credit | None = None
        self.tesouro: Tesouro | None = None
        self.errors: dict[str, str] = {}
        self.due: dict[str, datetime] = {}

    def _run(self, key: str, now: datetime, every: timedelta, action) -> None:
        if key in self.due and now < self.due[key]:
            return
        try:
            failures = action()
        except SourceError as err:
            failures = {key: str(err)}
        self.errors = {
            **{k: v for k, v in self.errors.items() if not k.startswith(key)},
            **failures,
        }
        self.due = {**self.due, key: now + (RETRY_AFTER if failures else every)}

    def refresh(self, now: datetime) -> None:
        today = now.astimezone().date()
        self._run("bc", now, REFRESH_EVERY, lambda: self._read_banco_central(today))
        self._run("yahoo", now, timedelta(minutes=1), lambda: self._read_yahoo(now))
        self._run(
            "focus",
            now,
            REFRESH_EVERY,
            lambda: self._set(
                "focus",
                self.fetch_focus(
                    FOCUS_INDICATORS, today - timedelta(days=HISTORY_DAYS)
                ),
            ),
        )
        self._run(
            "credito",
            now,
            SLOW_REFRESH,
            lambda: self._set("credit", self.fetch_credit()),
        )
        self._run(
            "tesouro",
            now,
            SLOW_REFRESH,
            lambda: self._set(
                "tesouro", self.fetch_tesouro(today - timedelta(days=HISTORY_DAYS))
            ),
        )

    def _set(self, attribute: str, value) -> dict:
        setattr(self, attribute, value)
        return {}

    def _read_banco_central(self, today: date) -> dict:
        series, sidra, failures = dict(self.series), dict(self.sidra), {}
        for key, (code, days) in SGS.items():
            try:
                series[key] = self.fetch_sgs(code, days, today)
            except SourceError as err:
                failures[f"bc {key}"] = str(err)
        for key, path in SIDRA.items():
            try:
                sidra[key] = self.fetch_sidra(path)
            except SourceError as err:
                failures[f"bc {key}"] = str(err)
        self.series, self.sidra = series, sidra
        return failures

    def _read_yahoo(self, now: datetime) -> dict:
        found, failures = dict(self.yahoo), {}
        for symbol, label in YAHOO:
            try:
                found[symbol] = [
                    self.fetch_series(symbol, period, now) for period in RATE_PERIODS
                ]
            except SourceError as err:
                failures[f"yahoo {label}"] = str(err)
        self.yahoo = found
        return failures

    def problems(self) -> list[str]:
        return [f"Juros {key}: {error}" for key, error in self.errors.items()]

    def rows(self) -> list[Row]:
        return build_rows(self.series, self.yahoo, self.focus, self.tesouro)

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        left, top, right, bottom = box
        split = left + round((right - left) * 0.57)
        draw_rows(
            draw,
            (left, top, split, bottom),
            "Juros, câmbio e expectativas",
            self.rows(),
        )
        column = split + ui.GUTTER
        draw_curve(
            draw,
            (column, top, right, top + 330),
            self.tesouro,
            self.errors.get("tesouro", ""),
        )
        draw_credit(
            draw,
            (column, top + 342, right, top + 642),
            self.credit,
            self.errors.get("credito", ""),
        )
        draw_metrics(
            draw,
            (column, top + 654, right, bottom),
            build_metrics(self.series, self.sidra, self.focus),
        )

    def draw_compact(
        self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime
    ) -> None:
        wanted = [
            row
            for row in self.rows()
            if any(row.label.startswith(name) for name in COMPACT_ROWS)
        ]
        draw_rows(draw, box, "Juros e câmbio", wanted)


def selftest() -> Image.Image:
    now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    today = now.date()
    daily = tuple(
        (today - timedelta(days=400 - i), 14.0 - i * 0.005) for i in range(401)
    )
    row = daily_row("Selic", daily, percent, "p.p.")
    assert [w.label for w in row.windows] == ["1d", "1w", "1m", "1y"]
    assert (
        round(row.windows[0].change, 3) == -0.005
        and round(row.windows[3].change, 3) == -1.825
    )
    assert row.windows[1].caption == "1w -0,04 p.p.", row.windows[1].caption
    monthly = tuple(
        (date(2025 + (m // 12), m % 12 + 1, 1), 4.0 + m / 10) for m in range(0, 22)
    )
    ipca = daily_row(
        "IPCA 12 meses", monthly, percent, "p.p.", windows=(("1m", 30), ("1y", 365))
    )
    assert (
        ipca.windows[0].change is None
        and ipca.windows[0].caption == "1d —"
        and ipca.windows[2].change is not None
    )
    assert (
        window_slice(((today, 1.0),), 7) == ((today, 1.0),)
        and window_slice((), 7) == ()
    )
    assert (
        round(compound(tuple((date(2026, m, 1), 1.0) for m in range(1, 13))), 2)
        == 12.68
    )
    assert round(real_rate(13.75, 4.58), 2) == 8.77
    dollar = daily_row(
        "PTAX", ((today - timedelta(days=1), 5.0), (today, 5.1)), str, "", relative=True
    )
    assert round(dollar.windows[0].change, 2) == 2.0 and dollar.windows[0].unit == "%"

    series = Series(4.99, 4.95, (4.96, 4.99), now)
    yahoo = {symbol: [series] * len(RATE_PERIODS) for symbol, _ in YAHOO}
    tesouro = Tesouro(
        date(2026, 10, 15),
        (),
        {
            ("Tesouro IPCA+", date(2035, 5, 15)): tuple(
                (date(2025, 10, 1) + timedelta(days=i), 6.5 + i / 1000)
                for i in range(380)
            ),
            ("Tesouro Prefixado", date(2031, 1, 1)): tuple(
                (date(2025, 10, 1) + timedelta(days=i), 12.5) for i in range(380)
            ),
            ("Tesouro Prefixado", date(2027, 1, 1)): ((date(2026, 10, 15), 13.4),),
        },
    )
    assert tesouro_pick(tesouro, "Tesouro IPCA+", 9)[0] == date(2035, 5, 15)
    assert tesouro_pick(tesouro, "Tesouro Prefixado", 5)[0] == date(2031, 1, 1)
    focus = {
        ("Selic", str(today.year)): tuple(
            (today - timedelta(days=7 * i), 13.25 + i * 0.01)
            for i in reversed(range(60))
        ),
        ("PIB Total", str(today.year)): ((today, 1.83),),
    }
    page = RatesPage()
    page.series = {
        "selic": daily,
        "cdi": daily,
        "ptax": tuple((d, 5.0 + v / 100) for d, v in daily),
        "ipca_year": monthly,
        "ipca15": monthly,
        "igpm": monthly,
        "inpc": monthly,
        "savings": monthly,
        "tr": monthly,
    }
    page.sidra = {
        "jobless": (("jun-jul-ago 2026", 5.3),),
        "gdp": (("2º trimestre 2026", 2.0),),
    }
    page.yahoo, page.focus, page.tesouro = yahoo, focus, tesouro
    page.credit = Credit(
        date(2026, 10, 2),
        {
            "Cheque especial - Prefixado": 155.2,
            "Cartão de crédito - rotativo total - Prefixado": 476.3,
        },
        {"Cheque especial - Prefixado": 155.0},
        {"Cheque especial - Prefixado": 149.0},
    )
    labels = [row.label for row in page.rows()]
    assert (
        labels[:4] == ["Selic", "CDI", "Dólar comercial", "Dólar PTAX"]
        and "IPCA+ 2035" in labels
    ), labels
    assert "Focus: Selic" in labels and "IPCA-15 12 meses" in labels
    metrics = [m.label for m in build_metrics(page.series, page.sidra, page.focus)]
    assert metrics[0] == "Juro real" and "Focus: PIB" in metrics

    def down(*args):
        raise SourceError("demorou demais")

    failing = RatesPage(down, down, down, down, down, down)
    failing.refresh(now)
    assert (
        all(due == now + RETRY_AFTER for due in failing.due.values())
        and len(failing.due) == 5
    )
    assert any(p.startswith("Juros tesouro") for p in failing.problems())
    failing.refresh(now + timedelta(minutes=1))
    image = Image.new("RGB", (1888, 992), ui.BACKGROUND)
    page.draw(ImageDraw.Draw(image), (0, 0, 1888, 992), now)
    page.draw_compact(
        ImageDraw.Draw(Image.new("RGB", (938, 992))), (0, 0, 938, 992), now
    )
    RatesPage().draw(
        ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 1888, 992), now
    )
    compact = [
        row.label
        for row in page.rows()
        if any(row.label.startswith(n) for n in COMPACT_ROWS)
    ]
    assert len(compact) == len(COMPACT_ROWS), ("every compact line is found", compact)
    print("page_rates selftest ok")
    return image

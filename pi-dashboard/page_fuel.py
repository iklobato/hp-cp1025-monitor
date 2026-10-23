"""Fuel page: this week's pump prices in the home city from the ANP station survey (no key),
compared with last week and with nearby cities, plus the cheapest stations."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import mean

import fields
import sources
import ui
from PIL import Image, ImageDraw
from sources import FuelWeek, SourceError, StationPrice

CITY = "São Paulo"
NEIGHBOURS = ("Campinas", "Guarulhos")
PRODUCTS = (
    ("GASOLINA COMUM", "Gasolina comum", "R$/litro"),
    ("ETANOL", "Etanol", "R$/litro"),
    ("GASOLINA ADITIVADA", "Gasolina aditivada", "R$/litro"),
    ("DIESEL S10", "Diesel S10", "R$/litro"),
    ("GNV", "GNV", "R$/m³"),
    ("GLP", "Gás de cozinha", "R$/13 kg"),
)
# The usual rule of thumb: ethanol pays off below 70% of the gasoline price.
ETHANOL_BREAK_EVEN = 0.70
REFRESH_EVERY = timedelta(hours=12)
RETRY_AFTER = timedelta(minutes=5)
STATIONS_SHOWN = 3  # what fits under the product cards when sharing the screen
ADDITIVE = "GASOLINA ADITIVADA"
PRODUCT_CARD_HEIGHT = 284
# Below this width the six products go in two rows of three and the two station lists stack:
# a product card needs ~250 px for "R$ 110,65" and its week bars.
WIDE_LAYOUT = 1500
TWO_ROW_CARD_HEIGHT = 272


def reais(value: float, digits: int = 2) -> str:
    return "R$ " + f"{value:.{digits}f}".replace(".", ",")


@dataclass(frozen=True)
class Summary:
    average: float
    low: float
    high: float
    stations: int


def summarize(
    prices: tuple[StationPrice, ...], city: str, product: str
) -> Summary | None:
    key = sources.title_key(city)  # ANP writes JUNDIAI for Jundiaí
    values = [
        p.price
        for p in prices
        if sources.title_key(p.city) == key and p.product == product
    ]
    if not values:
        return None
    return Summary(mean(values), min(values), max(values), len(values))


def ethanol_ratio(prices: tuple[StationPrice, ...], city: str) -> float | None:
    gasoline, ethanol = (
        summarize(prices, city, "GASOLINA COMUM"),
        summarize(prices, city, "ETANOL"),
    )
    if gasoline is None or ethanol is None:
        return None
    return ethanol.average / gasoline.average


def cheapest(
    prices: tuple[StationPrice, ...], city: str, product: str
) -> list[StationPrice]:
    key = sources.title_key(city)
    return sorted(
        (
            p
            for p in prices
            if sources.title_key(p.city) == key and p.product == product
        ),
        key=lambda p: (p.price, p.station),
    )


def same_station(
    prices: tuple[StationPrice, ...], station: StationPrice, product: str
) -> float | None:
    """The price this station charges for another product (or charged in another week)."""
    return next(
        (
            p.price
            for p in prices
            if p.city == station.city
            and p.station == station.station
            and p.address == station.address
            and p.product == product
        ),
        None,
    )


def week_change(change: float) -> tuple[str, tuple]:
    """ANP sometimes repeats a city's prices from one week to the next; say so instead of "R$ 0,00"."""
    if abs(change) < 0.005:
        return "igual à semana anterior", ui.DIM
    return (
        f"{ui.arrow(change)} {reais(abs(change))} na semana",
        ui.RED if change > 0 else ui.GREEN,
    )


def draw_product(
    draw,
    box: tuple,
    label: str,
    unit: str,
    now: Summary | None,
    before: Summary | None,
    neighbours: list[tuple[str, Summary | None]],
    weeks: tuple[date, date | None],
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box)
    draw.text(
        (left + 16, top + 34),
        label,
        font=ui.font(19, bold=True),
        fill=ui.TEXT,
        anchor="ls",
    )
    draw.text((right - 16, top + 34), unit, font=ui.font(13), fill=ui.DIM, anchor="rs")
    if now is None:
        draw.text(
            (left + 16, top + 90),
            "sem preço na cidade",
            font=ui.font(16),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    draw.text(
        (left + 16, top + 84),
        reais(now.average),
        font=ui.font(34, bold=True),
        fill=ui.TEXT,
        anchor="ls",
    )
    draw.text(
        (left + 16, top + 106),
        f"média de {now.stations} postos",
        font=ui.font(13),
        fill=ui.DIM,
        anchor="ls",
    )
    if before is not None:
        draw.text(
            (left + 16, top + 130),
            *week_change(now.average - before.average),
            font=ui.font(15, bold=True),
            anchor="ls",
        )
    this_week, last_week = weeks
    strips = (
        [(f"{last_week:%d/%m}", before, False)]
        if before is not None and last_week
        else []
    )
    strips.append((f"{this_week:%d/%m}", now, True))
    draw_week_strips(draw, (left + 16, top + 158, right - 16, top + 210), strips)
    y = top + 238
    for name, summary in neighbours:
        draw.text(
            (left + 16, y),
            name,
            font=ui.font(14),
            fill=ui.DIM,
            anchor="ls",
        )
        draw.text(
            (right - 16, y),
            reais(summary.average) if summary else "-",
            font=ui.font(14, bold=True),
            fill=ui.TEXT,
            anchor="rs",
        )
        y += 22


def draw_week_strips(draw, box: tuple, strips: list[tuple[str, Summary, bool]]) -> None:
    """Each week's price range (bar) and average (dot) on one shared scale, last week above this one."""
    left, top, right, bottom = box
    low, high = min(s.low for _, s, _ in strips), max(s.high for _, s, _ in strips)
    bar_left, bar_right = left + 50, right - 4

    def x_of(value: float) -> float:
        if high == low:
            return (bar_left + bar_right) / 2
        return bar_left + (value - low) / (high - low) * (bar_right - bar_left)

    for index, (label, summary, current) in enumerate(strips):
        y = top + index * 22
        draw.text(
            (left, y + 5),
            label,
            font=ui.font(13, bold=current),
            fill=ui.TEXT if current else ui.DIM,
            anchor="ls",
        )
        draw.rounded_rectangle(
            (x_of(summary.low) - 4, y - 4, x_of(summary.high) + 4, y + 4),
            radius=4,
            fill=ui.blend(ui.BLUE, ui.CARD, 0.55) if current else ui.GRID,
        )
        dot = x_of(summary.average)
        draw.ellipse(
            (dot - 6, y - 6, dot + 6, y + 6), fill=ui.TEXT if current else ui.DIM
        )
    draw.text(
        (bar_left, bottom), reais(low), font=ui.font(12), fill=ui.DIM, anchor="ls"
    )
    draw.text(
        (bar_right, bottom), reais(high), font=ui.font(12), fill=ui.DIM, anchor="rs"
    )


def draw_stations(
    draw,
    box: tuple,
    title: str,
    note: tuple[str, tuple] | None,
    stations: list[StationPrice],
    current: tuple[StationPrice, ...],
    previous: tuple[StationPrice, ...],
    count: int,
    other: tuple[str, str],
) -> None:
    """The cheapest stations for one product; `other` = (product, label) priced beside each."""
    left, top, right, bottom = box
    ui.card(draw, box, title)
    if note is not None:
        draw.text(
            (right - 16, top + 32),
            note[0],
            font=ui.font(16, bold=True),
            fill=note[1],
            anchor="rs",
        )
    if not stations:
        draw.text(
            (left + 16, top + 80),
            "nenhum posto pesquisado",
            font=ui.font(18),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    shown = stations[:count]
    row = min((bottom - top - 52) / len(shown), 64)
    cheapest_price = stations[0].price
    for index, station in enumerate(shown):
        y = top + 50 + index * row
        ui.stripe(draw, (left + 8, y, right - 8, y + row - 4), index)
        color = ui.GREEN if station.price == cheapest_price else ui.TEXT
        draw.text(
            (left + 16, y + 30),
            reais(station.price),
            font=ui.font(22, bold=True),
            fill=color,
            anchor="ls",
        )
        text_left, extras_left = left + 140, right - 230
        draw.text(
            (text_left, y + 24),
            ui.fit(
                station.station, ui.font(16, bold=True), extras_left - text_left - 16
            ),
            font=ui.font(16, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        place = f"{station.address.title()} · {station.brand.title()}"
        draw.text(
            (text_left, y + 46),
            ui.fit(place, ui.font(13), extras_left - text_left - 16),
            font=ui.font(13),
            fill=ui.DIM,
            anchor="ls",
        )
        other_product, other_label = other
        other_price = same_station(current, station, other_product)
        draw.text(
            (right - 16, y + 24),
            f"{other_label} {reais(other_price)}"
            if other_price
            else f"{other_label}: sem preço na ANP",
            font=ui.font(15, bold=other_price is not None),
            fill=ui.TEXT if other_price else ui.DIM,
            anchor="rs",
        )
        before = same_station(previous, station, station.product)
        if before is not None:
            text, change_color = week_change(station.price - before)
            draw.text(
                (right - 16, y + 46),
                text,
                font=ui.font(13),
                fill=change_color,
                anchor="rs",
            )


class FuelPage(ui.Page):
    name = "Combustível"
    settings_fields = (
        fields.Field(
            "cidade",
            "Cidade",
            "texto",
            CITY,
            help="Como a ANP escreve, sem precisar de acento: São Paulo, Campinas.",
        ),
        fields.Field(
            "vizinhas",
            "Cidades para comparar",
            "palavras",
            list(NEIGHBOURS),
            low=0,
            high=3,
            help="Aparecem no pé de cada combustível com o preço médio.",
        ),
        fields.Field(
            "postos",
            "Postos mais baratos na lista",
            "numero",
            STATIONS_SHOWN,
            low=1,
            high=3,
            help="Cabem até 3 com a loteria na mesma tela.",
        ),
    )

    def __init__(self, fetch_weeks=sources.fetch_fuel_weeks) -> None:
        self.fetch_weeks = fetch_weeks
        self.city, self.neighbours, self.stations_shown = (
            CITY,
            NEIGHBOURS,
            STATIONS_SHOWN,
        )
        self.weeks: tuple[FuelWeek, ...] = ()
        self.error = ""
        self.next_fetch_at: datetime | None = None

    def apply_settings(self, values: dict) -> None:
        cities = (values["cidade"], tuple(values["vizinhas"]))
        if cities != (
            self.city,
            self.neighbours,
        ):  # other cities: download the survey again
            self.next_fetch_at = None
        self.city, self.neighbours = cities
        self.stations_shown = values["postos"]

    def refresh(self, now: datetime) -> None:
        if self.next_fetch_at is not None and now < self.next_fetch_at:
            return
        try:
            self.weeks, self.error = self.fetch_weeks((self.city, *self.neighbours)), ""
            self.next_fetch_at = now + REFRESH_EVERY
        except SourceError as err:
            self.error = f"ANP: {err}"
            self.next_fetch_at = now + RETRY_AFTER

    def problems(self) -> list[str]:
        return [self.error] if self.error else []

    def status_items(self, now: datetime) -> list[tuple[str, str, tuple]]:
        if not self.weeks:
            return []
        end = self.weeks[0].end
        return [
            (
                "ANP",
                f"pesquisa de {end - timedelta(days=6):%d/%m} a {end:%d/%m}",
                ui.BLUE,
            )
        ]

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        left, top, right, bottom = box
        if not self.weeks:
            ui.card(draw, box, f"Combustível em {self.city}")
            draw.text(
                (left + 16, top + 80),
                self.error or "carregando…",
                font=ui.font(20),
                fill=ui.DIM,
                anchor="ls",
            )
            return
        current = self.weeks[0].prices
        previous = self.weeks[1].prices if len(self.weeks) > 1 else ()
        weeks = (self.weeks[0].end, self.weeks[1].end if len(self.weeks) > 1 else None)
        wide = right - left >= WIDE_LAYOUT
        per_row = len(PRODUCTS) if wide else 3
        card_height = PRODUCT_CARD_HEIGHT if wide else TWO_ROW_CARD_HEIGHT
        rows = -(-len(PRODUCTS) // per_row)
        cards_bottom = top + rows * card_height + (rows - 1) * ui.GUTTER
        width = (right - left - (per_row - 1) * ui.GUTTER) / per_row
        for index, (product, label, unit) in enumerate(PRODUCTS):
            row, column = divmod(index, per_row)
            x = left + column * (width + ui.GUTTER)
            y = top + row * (card_height + ui.GUTTER)
            neighbours = [
                (city, summarize(current, city, product)) for city in self.neighbours
            ]
            draw_product(
                draw,
                (x, y, x + width, y + card_height),
                label,
                unit,
                summarize(current, self.city, product),
                summarize(previous, self.city, product),
                neighbours,
                weeks,
            )
        lists_top = cards_bottom + ui.GUTTER
        if wide:
            half = (right - left - ui.GUTTER) / 2
            gasoline_box = (left, lists_top, left + half, bottom)
            ethanol_box = (left + half + ui.GUTTER, lists_top, right, bottom)
        else:
            middle = lists_top + (bottom - lists_top - ui.GUTTER) / 2
            gasoline_box = (left, lists_top, right, middle)
            ethanol_box = (left, middle + ui.GUTTER, right, bottom)
        ratio = ethanol_ratio(current, self.city)
        note = None
        if ratio is not None:
            pays = ratio < ETHANOL_BREAK_EVEN
            note = (
                f"etanol custa {ratio:.0%} da gasolina: {'compensa' if pays else 'não compensa'}",
                ui.GREEN if pays else ui.AMBER,
            )
        draw_stations(
            draw,
            gasoline_box,
            f"Gasolina aditivada mais barata em {self.city}",
            None,
            cheapest(current, self.city, ADDITIVE),
            current,
            previous,
            self.stations_shown,
            ("GASOLINA COMUM", "comum"),
        )
        draw_stations(
            draw,
            ethanol_box,
            f"Etanol mais barato em {self.city}",
            note,
            cheapest(current, self.city, "ETANOL"),
            current,
            previous,
            self.stations_shown,
            (ADDITIVE, "aditivada"),
        )


def selftest() -> Image.Image:
    now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)

    def price(
        city: str, product: str, value: float, station: str = "POSTO"
    ) -> StationPrice:
        return StationPrice(
            city,
            f"{station} {value}",
            "AVENIDA MARECHAL CASTELO BRANCO, 760 - JARDIM DA LUZ",
            "BRANCA",
            product,
            value,
        )

    additive = StationPrice(
        "SAO PAULO",
        "POSTO 5.99",
        "AVENIDA MARECHAL CASTELO BRANCO, 760 - JARDIM DA LUZ",
        "BRANCA",
        "GASOLINA ADITIVADA",
        6.29,
    )
    current = (
        additive,
        price("SAO PAULO", "GASOLINA COMUM", 6.19),
        price("SAO PAULO", "GASOLINA COMUM", 5.99),
        price("SAO PAULO", "ETANOL", 4.09),
        price("SAO PAULO", "ETANOL", 4.29),
        price("CAMPINAS", "GASOLINA COMUM", 6.05),
        price("JUNDIAI", "ETANOL", 4.19),
    )
    previous = (
        price("SAO PAULO", "GASOLINA COMUM", 6.00),
        price("SAO PAULO", "GASOLINA COMUM", 6.10),
    )
    gasoline = summarize(current, CITY, "GASOLINA COMUM")
    assert gasoline == Summary(6.09, 5.99, 6.19, 2), gasoline
    assert summarize(current, CITY, "GNV") is None
    assert summarize(current, "Jundiaí", "ETANOL").average == 4.19, (
        "ANP writes JUNDIAI, no accent"
    )
    moved = FuelPage(lambda cities: ())
    moved.next_fetch_at = now + REFRESH_EVERY
    moved.apply_settings(
        fields.read_all(
            FuelPage.settings_fields,
            {"cidade": "Campinas", "vizinhas": []},
            "blocos.combustivel",
        )
    )
    assert moved.city == "Campinas" and moved.next_fetch_at is None, (
        "another city downloads again now"
    )
    moved.next_fetch_at = now
    moved.apply_settings(
        fields.read_all(
            FuelPage.settings_fields,
            {"cidade": "Campinas", "vizinhas": [], "postos": 2},
            "blocos.combustivel",
        )
    )
    assert moved.next_fetch_at == now and moved.stations_shown == 2, (
        "only the list size changed"
    )
    assert round(ethanol_ratio(current, CITY), 3) == round(4.19 / 6.09, 3)
    assert [p.price for p in cheapest(current, CITY, "ETANOL")] == [4.09, 4.29]
    best = cheapest(current, CITY, "GASOLINA COMUM")[0]
    assert same_station(current, best, ADDITIVE) == 6.29, (
        "the additive price of the same station"
    )
    assert (
        same_station(current, cheapest(current, CITY, "GASOLINA COMUM")[1], ADDITIVE)
        is None
    )
    assert (
        same_station(
            (price("SAO PAULO", "GASOLINA COMUM", 5.99),), best, "GASOLINA COMUM"
        )
        == 5.99
    )
    assert (
        same_station(
            (price("CAMPINAS", "GASOLINA COMUM", 5.99),), best, "GASOLINA COMUM"
        )
        is None
    ), "a station with the same name in another city is another station"
    assert reais(6.09) == "R$ 6,09"
    assert week_change(0.0) == ("igual à semana anterior", ui.DIM)
    assert (
        week_change(0.10) == ("▲ R$ 0,10 na semana", ui.RED)
        and week_change(-0.2)[1] == ui.GREEN
    )

    def down(cities):
        raise SourceError("demorou demais")

    failing = FuelPage(down)
    failing.refresh(now)
    assert failing.next_fetch_at == now + RETRY_AFTER and failing.problems() == [
        "ANP: demorou demais"
    ]
    page = FuelPage(
        lambda cities: (
            FuelWeek(date(2026, 10, 17), current),
            FuelWeek(date(2026, 10, 10), previous),
        )
    )
    page.refresh(now)
    assert page.next_fetch_at == now + REFRESH_EVERY
    assert page.status_items(now) == [("ANP", "pesquisa de 11/10 a 17/10", ui.BLUE)]
    page.draw(ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 1888, 992), now)
    image = Image.new("RGB", (1888, 992), ui.BACKGROUND)
    page.draw(ImageDraw.Draw(image), (0, 0, 1128, 992), now)  # beside the lottery
    failing.draw(ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 1888, 992), now)
    print("page_fuel selftest ok")
    return image

"""Market pages, quotes from Yahoo (no key).

Mercado: highlighted quotes and a table of stocks, each with its 1d/1w/1m change as numbers.
Full: big cards on top and a three-column table. Compact (beside the rates): two lists.
News lives only on the news screen.
Mercado mundo: US, European and Asian indices, currencies, crypto and commodities.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import fields
import market_data
import net
import ui
from market_data import PERIODS, Series
from net import SourceError
from PIL import Image, ImageDraw


@dataclass(frozen=True)
class Quote:
    symbol: str
    label: str
    scale: float = 1.0  # shown price per this many units (100 yen, 1000 pesos)


HIGHLIGHTS = (
    Quote("^BVSP", "Ibovespa"),
    Quote("BRL=X", "Dólar"),
    Quote("^GSPC", "S&P 500"),
    Quote("BTC-USD", "Bitcoin US$"),
)
BRAZIL_STOCKS = tuple(
    Quote(symbol, label)
    for symbol, label in (
        ("PETR4.SA", "Petrobras"),
        ("VALE3.SA", "Vale"),
        ("ITUB4.SA", "Itaú"),
        ("BBDC4.SA", "Bradesco"),
        ("ABEV3.SA", "Ambev"),
        ("WEGE3.SA", "WEG"),
        ("B3SA3.SA", "B3"),
        ("ITSA4.SA", "Itaúsa"),
        ("MGLU3.SA", "Magalu"),
        ("BBAS3.SA", "Banco do Brasil"),
        ("AXIA3.SA", "Axia (Eletrobras)"),
    )
)
WORLD_GROUPS = (
    (
        "EUA",
        (
            Quote("^GSPC", "S&P 500"),
            Quote("^IXIC", "Nasdaq"),
            Quote("^DJI", "Dow Jones"),
            Quote("^RUT", "Russell 2000"),
        ),
    ),
    (
        "Europa",
        (
            Quote("^STOXX", "Stoxx 600"),
            Quote("^GDAXI", "DAX (Alemanha)"),
            Quote("^FTSE", "FTSE (Londres)"),
        ),
    ),
    ("Ásia", (Quote("^N225", "Nikkei (Japão)"), Quote("^HSI", "Hang Seng"))),
    (
        "Moedas em reais",
        (
            Quote("BRL=X", "Dólar"),
            Quote("EURBRL=X", "Euro"),
            Quote("GBPBRL=X", "Libra"),
            Quote("CHFBRL=X", "Franco suíço"),
            Quote("CNYBRL=X", "Yuan"),
            Quote("JPYBRL=X", "100 ienes", 100),
            Quote("ARSBRL=X", "1.000 pesos arg.", 1000),
        ),
    ),
    ("Cripto", (Quote("BTC-USD", "Bitcoin US$"), Quote("ETH-USD", "Ethereum US$"))),
    (
        "Energia e metais",
        (
            Quote("BZ=F", "Brent US$"),
            Quote("CL=F", "WTI US$"),
            Quote("NG=F", "Gás natural US$"),
            Quote("GC=F", "Ouro US$"),
            Quote("SI=F", "Prata US$"),
            Quote("HG=F", "Cobre US$"),
        ),
    ),
    (
        "Agrícolas",
        (
            Quote("ZS=F", "Soja ¢/bushel"),
            Quote("ZC=F", "Milho ¢/bushel"),
            Quote("KC=F", "Café ¢/libra"),
        ),
    ),
)
# Status bar chips: one quote stands for each market's trading hours.
MARKET_CHIPS = (
    ("B3", "^BVSP"),
    ("NYSE", "^GSPC"),
    ("Moedas", "BRL=X"),
    ("Cripto", "BTC-USD"),
)
WORLD_CHIPS = (
    ("NYSE", "^GSPC"),
    ("Europa", "^GDAXI"),
    ("Ásia", "^N225"),
    ("Moedas", "BRL=X"),
    ("Cripto", "BTC-USD"),
)
FETCH_WORKERS = 8
HIGHLIGHT_BAND = (0, 170)
STOCKS_TOP = 182
STOCK_ROW = 36
TWO_LINE_ROW = 46  # rows this tall also get the "last session" note under the name
CARD_HEADER = 44
# Three columns of 6-7 quotes each, so no column is left half empty.
WORLD_COLUMNS = (
    ("EUA", "Europa", "Ásia"),
    ("Moedas em reais", "Cripto"),
    ("Energia e metais", "Agrícolas"),
)
# Sharing the screen (half the width): two columns.
WORLD_COMPACT_COLUMNS = (
    ("EUA", "Europa", "Ásia", "Agrícolas"),
    ("Moedas em reais", "Cripto", "Energia e metais"),
)


def world_row_height(height: float, stacks: list[list[tuple]]) -> int:
    """The tallest row every column can afford, so rows line up across the page."""
    return int(
        min(
            (height - len(stack) * CARD_HEADER - (len(stack) - 1) * ui.GUTTER)
            / sum(len(quotes) for _, quotes in stack)
            for stack in stacks
        )
    )


Outcome = Series | SourceError


def gather_quotes(
    quotes: tuple[Quote, ...], now: datetime, fetch=market_data.fetch_series
) -> tuple[dict[str, list[Outcome]], list[str]]:
    def outcome(job: tuple) -> Outcome:
        symbol, period = job
        try:
            return fetch(symbol, period, now)
        except SourceError as err:
            return err

    symbols = list(dict.fromkeys(quote.symbol for quote in quotes))
    jobs = [(symbol, period) for symbol in symbols for period in PERIODS]
    with ThreadPoolExecutor(FETCH_WORKERS) as pool:
        results = list(pool.map(outcome, jobs))
    found = {
        symbol: results[i * len(PERIODS) : (i + 1) * len(PERIODS)]
        for i, symbol in enumerate(symbols)
    }
    labels = {quote.symbol: quote.label for quote in quotes}
    problems = [
        f"{labels[symbol]}: {found[symbol][0]}"
        for symbol in symbols
        if isinstance(found[symbol][0], SourceError)
    ]
    return found, problems


def change_text(series: Series) -> str:
    stale = " ✘" if series.stale else ""
    return movement(series.change)[0] + stale


def old_quote_note(series: Series, now: datetime) -> str:
    """Empty while the quote is from today; otherwise when the last trade happened."""
    stamp = net.clock(series.traded_at, now)
    return "" if len(stamp) <= 5 else f"último pregão {stamp}"


# Before the first refresh nothing failed yet: drawn as a dim "carregando…", not a red error.
LOADING = SourceError("carregando…")


def failure(error: SourceError) -> tuple[str, tuple]:
    return ("carregando…", ui.DIM) if error is LOADING else (f"✘ {error}", ui.RED)


def missing_outcomes(found: dict) -> list[SourceError]:
    return [LOADING if not found else SourceError("sem dados")] * len(PERIODS)


def chips(
    found: dict[str, list[Outcome]], pairs: tuple, now: datetime
) -> list[tuple[str, str, tuple]]:
    items = []
    for name, symbol in pairs:
        day = found.get(symbol, missing_outcomes(found))[0]
        state = (
            day.state(now)
            if isinstance(day, Series)
            else failure(day)[0].removeprefix("✘ ")
        )
        color = (
            ui.GREEN
            if state == "aberto"
            else ui.AMBER
            if state.startswith("abre")
            else ui.DIM
        )
        items.append((name, state, color))
    return items


def movement(change: float) -> tuple[str, tuple]:
    """'▲ +1,78%' and its color; the arrow follows the number shown, so +0,00% gets none."""
    shown = round(change * 100, 2)
    return f"{ui.arrow(shown)} {net.br_percent(change)}", ui.trend_color(shown)


def change_cell(draw, x: float, y: float, label: str, outcome: Outcome, face) -> None:
    """'1d ▲ +1,78%': the window in grey, then arrow and change in the trend color."""
    if label:
        label_face = ui.font(face.size - 2)
        draw.text((x, y), label, font=label_face, fill=ui.DIM, anchor="ls")
        x += label_face.getlength(label) + 6
    if isinstance(outcome, SourceError):
        pending = outcome is LOADING
        draw.text(
            (x, y),
            "…" if pending else "✘",
            font=face,
            fill=ui.DIM if pending else ui.RED,
            anchor="ls",
        )
        return
    text, color = movement(outcome.change)
    draw.text(
        (x, y),
        text + (" ✘" if outcome.stale else ""),
        font=face,
        fill=color,
        anchor="ls",
    )


def draw_highlight(
    draw, box: tuple, quote: Quote, outcomes: list[Outcome], now: datetime
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box)
    pad = 16
    day = outcomes[0]
    draw.text(
        (left + pad, top + 36),
        quote.label,
        font=ui.font(22, bold=True),
        fill=ui.TEXT,
        anchor="ls",
    )
    if isinstance(day, SourceError):
        text, color = failure(day)
        draw.text(
            (left + pad, top + 92), text, font=ui.font(22), fill=color, anchor="ls"
        )
    else:
        draw.text(
            (right - pad, top + 34),
            old_quote_note(day, now),
            font=ui.font(14),
            fill=ui.DIM,
            anchor="rs",
        )
        draw.text(
            (left + pad, top + 90),
            net.br_number(day.price * quote.scale),
            font=ui.font(44, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        draw.text(
            (right - pad, top + 88),
            change_text(day),
            font=ui.font(24, bold=True),
            fill=movement(day.change)[1],
            anchor="rs",
        )
    width = (right - left - 2 * pad) / len(PERIODS)
    for index, (period, outcome) in enumerate(zip(PERIODS, outcomes)):
        change_cell(
            draw,
            left + pad + index * width,
            bottom - 20,
            period.label,
            outcome,
            ui.font(16, bold=True),
        )


def chart_columns(left: float, right: float) -> tuple[float, list[float], float]:
    """Where the price ends and the three charts start, and how wide each chart is."""
    price_right = left + min(230, round((right - left) * 0.45))
    gap = 12
    width = (right - (price_right + 18) - 2 * gap) / len(PERIODS)
    return (
        price_right,
        [price_right + 18 + index * (width + gap) for index in range(len(PERIODS))],
        width,
    )


NARROW_CELL = 110  # below this a cell cannot hold "1d ▲ -0,17%": the card header names the windows


def draw_quote_rows(
    draw,
    box: tuple,
    quotes: tuple[Quote, ...],
    found: dict[str, list[Outcome]],
    now: datetime,
    row_height: int,
) -> None:
    """Name, price and the 1d/1w/1m changes per quote, inside an existing card."""
    left, top, right, _ = box
    price_right, cell_lefts, cell_width = chart_columns(left, right)
    narrow = cell_width < NARROW_CELL  # the card header names the windows instead
    two_lines = row_height >= TWO_LINE_ROW
    middle = 22 if two_lines else row_height / 2 + 6
    for row, quote in enumerate(quotes):
        row_top = top + row * row_height
        ui.stripe(
            draw, (left - 8, row_top - 2, right + 8, row_top + row_height - 2), row
        )
        outcomes = found.get(quote.symbol, missing_outcomes(found))
        day = outcomes[0]
        draw.text(
            (left, row_top + middle),
            ui.fit(quote.label, ui.font(17, bold=True), price_right - left - 80),
            font=ui.font(17, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        if not isinstance(day, SourceError):
            draw.text(
                (price_right, row_top + middle),
                net.br_number(day.price * quote.scale),
                font=ui.font(19, bold=True),
                fill=ui.TEXT,
                anchor="rs",
            )
        if two_lines:
            text, color = (
                failure(day)
                if isinstance(day, SourceError)
                else (old_quote_note(day, now), ui.DIM)
            )
            draw.text(
                (left, row_top + 42), text, font=ui.font(13), fill=color, anchor="ls"
            )
        for period, outcome, cell_left in zip(PERIODS, outcomes, cell_lefts):
            change_cell(
                draw,
                cell_left,
                row_top + middle,
                "" if narrow else period.label,
                outcome,
                ui.font(15, bold=True, condensed=narrow),
            )


def draw_quote_card(
    draw,
    box: tuple,
    title: str,
    quotes: tuple[Quote, ...],
    found,
    now: datetime,
    *,
    columns: int = 1,
    row_height: int = STOCK_ROW,
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, title)
    pad = 14
    column_gap = (
        44  # wide, with a divider: a row's last chart must not read as the next name's
    )
    per_column = -(-len(quotes) // columns)
    width = (right - left - 2 * pad - (columns - 1) * column_gap) / columns
    for column in range(columns):
        column_left = left + pad + column * (width + column_gap)
        if column:
            ui.divider(draw, column_left - column_gap / 2, top + 48, bottom - 10)
        _, chart_lefts, chart_width = chart_columns(column_left, column_left + width)
        if chart_width < NARROW_CELL:
            for period, chart_left in zip(PERIODS, chart_lefts):
                draw.text(
                    (chart_left, top + 30),
                    period.label,
                    font=ui.font(13, bold=True),
                    fill=ui.DIM,
                    anchor="ls",
                )
        part = quotes[column * per_column : (column + 1) * per_column]
        draw_quote_rows(
            draw,
            (column_left, top + 44, column_left + width, bottom),
            part,
            found,
            now,
            row_height,
        )


def split_columns(box: tuple, count: int, band: tuple | None = None) -> list[tuple]:
    left, top, right, bottom = box
    band_top, band_bottom = (top + band[0], top + band[1]) if band else (top, bottom)
    width = (right - left - (count - 1) * ui.GUTTER) / count
    return [
        (
            left + i * (width + ui.GUTTER),
            band_top,
            left + i * (width + ui.GUTTER) + width,
            band_bottom,
        )
        for i in range(count)
    ]


QUOTE_COLUMNS = (
    fields.Column("simbolo", "Símbolo no Yahoo"),
    fields.Column("nome", "Nome na tela"),
)
SYMBOL_HELP = "Símbolo do Yahoo Finance: PETR4.SA (ação da B3), BRL=X (dólar), ^BVSP (Ibovespa), BTC-USD."


def quote_rows(quotes: tuple[Quote, ...]) -> list[dict]:
    return [{"simbolo": quote.symbol, "nome": quote.label} for quote in quotes]


def quotes_from(rows: list[dict]) -> tuple[Quote, ...]:
    return tuple(Quote(row["simbolo"], row["nome"]) for row in rows)


class MarketPage(ui.Page):
    name = "Mercado"
    settings_fields = (
        fields.Field(
            "destaques",
            "Destaques grandes (em cima)",
            "tabela",
            quote_rows(HIGHLIGHTS),
            help=SYMBOL_HELP,
            low=1,
            high=4,
            columns=QUOTE_COLUMNS,
            unique="simbolo",
        ),
        fields.Field(
            "acoes",
            "Tabela de ações (até 12)",
            "tabela",
            quote_rows(BRAZIL_STOCKS),
            low=0,
            high=12,
            columns=QUOTE_COLUMNS,
            unique="simbolo",
        ),
    )

    def __init__(self) -> None:
        self.highlights, self.stocks = HIGHLIGHTS, BRAZIL_STOCKS
        self.found: dict[str, list[Outcome]] = {}
        self.quote_problems: list[str] = []

    def apply_settings(self, values: dict) -> None:
        self.highlights, self.stocks = (
            quotes_from(values["destaques"]),
            quotes_from(values["acoes"]),
        )

    @property
    def quotes(self) -> tuple[Quote, ...]:
        """What is on screen, plus the quotes the status bar chips read market hours from."""
        shown = (*self.highlights, *self.stocks)
        symbols = {quote.symbol for quote in shown}
        chip_quotes = tuple(
            Quote(symbol, name)
            for name, symbol in MARKET_CHIPS
            if symbol not in symbols
        )
        return shown + chip_quotes

    def refresh(self, now: datetime) -> None:
        self.found, self.quote_problems = gather_quotes(self.quotes, now)

    def problems(self) -> list[str]:
        return self.quote_problems

    def status_items(self, now: datetime) -> list[tuple[str, str, tuple]]:
        return chips(self.found, MARKET_CHIPS, now)

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        left, top, right, bottom = box
        loading = missing_outcomes(self.found)
        highlights = self.highlights
        for column, quote in zip(
            split_columns(box, len(highlights), HIGHLIGHT_BAND), highlights
        ):
            draw_highlight(
                draw, column, quote, self.found.get(quote.symbol, loading), now
            )
        rows = -(-len(self.stocks) // 3) or 1
        draw_quote_card(
            draw,
            (left, top + STOCKS_TOP, right, bottom),
            "Ações",
            self.stocks,
            self.found,
            now,
            columns=3,
            row_height=min(
                60, int((bottom - top - STOCKS_TOP - CARD_HEADER - 12) / rows)
            ),
        )

    def draw_compact(
        self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime
    ) -> None:
        """A narrow column beside the rates: the highlights, then the stocks, one per line."""
        left, top, right, bottom = box
        count = len(self.highlights) + len(self.stocks)
        cards = 2 if self.stocks else 1
        row = int(
            (bottom - top - cards * (CARD_HEADER + 12) - ui.GUTTER) / max(count, 1)
        )
        split = top + CARD_HEADER + 12 + len(self.highlights) * row
        draw_quote_card(
            draw,
            (left, top, right, split),
            "Destaques",
            self.highlights,
            self.found,
            now,
            row_height=row,
        )
        if self.stocks:
            draw_quote_card(
                draw,
                (left, split + ui.GUTTER, right, bottom),
                "Ações",
                self.stocks,
                self.found,
                now,
                row_height=row,
            )


class WorldMarketPage(ui.Page):
    name = "Mercado mundo"
    QUOTES = tuple(quote for _, quotes in WORLD_GROUPS for quote in quotes)

    def __init__(self) -> None:
        self.found: dict[str, list[Outcome]] = {}
        self.quote_problems: list[str] = []

    def refresh(self, now: datetime) -> None:
        self.found, self.quote_problems = gather_quotes(self.QUOTES, now)

    def problems(self) -> list[str]:
        return self.quote_problems

    def status_items(self, now: datetime) -> list[tuple[str, str, tuple]]:
        return chips(self.found, WORLD_CHIPS, now)

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        self._draw_columns(draw, box, now, WORLD_COLUMNS)

    def draw_compact(
        self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime
    ) -> None:
        self._draw_columns(draw, box, now, WORLD_COMPACT_COLUMNS)

    def _draw_columns(self, draw, box: tuple, now: datetime, columns: tuple) -> None:
        groups = dict(WORLD_GROUPS)
        stacks = [[(title, groups[title]) for title in column] for column in columns]
        row_height = world_row_height(box[3] - box[1], stacks)
        for (left, top, right, _), stack in zip(
            split_columns(box, len(stacks)), stacks
        ):
            for title, quotes in stack:
                height = CARD_HEADER + len(quotes) * row_height
                draw_quote_card(
                    draw,
                    (left, top, right, top + height),
                    title,
                    quotes,
                    self.found,
                    now,
                    row_height=row_height,
                )
                top += height + ui.GUTTER


def selftest() -> Image.Image:
    now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    session = (now - timedelta(hours=2), now + timedelta(hours=5))
    rising = Series(110.0, 100.0, (101.0, 99.0, 104.0, 110.0), now, session=session)
    yesterday = Series(
        5.0,
        5.2,
        (5.1, 5.0),
        now - timedelta(days=1),
        stale=True,
        session=(now + timedelta(hours=1), now + timedelta(hours=8)),
    )
    page = MarketPage()
    page.found = {
        quote.symbol: [rising, yesterday, SourceError("sem dados")]
        for quote in page.quotes
    }
    assert old_quote_note(rising, now) == "" and old_quote_note(
        yesterday, now
    ).startswith("último pregão 22/10")
    assert change_text(yesterday) == "▼ -3,85% ✘"
    assert failure(missing_outcomes({})[0]) == ("carregando…", ui.DIM), (
        "nothing fetched yet is not an error"
    )
    assert chips({}, MARKET_CHIPS, now)[0] == ("B3", "carregando…", ui.DIM)
    assert movement(0.0178) == ("▲ +1,78%", ui.GREEN) and movement(-0.0147)[1] == ui.RED
    assert movement(0.00001) == ("■ +0,00%", ui.DIM), (
        "a change that shows as 0,00% gets no arrow"
    )
    assert failure(missing_outcomes({"X": []})[0]) == ("✘ sem dados", ui.RED), (
        "fetched but missing is"
    )
    assert len(BRAZIL_STOCKS) == 11 and len({q.symbol for q in page.quotes}) == 15
    assert all(symbol in {q.symbol for q in page.quotes} for _, symbol in MARKET_CHIPS)
    custom = MarketPage()
    custom.apply_settings(
        fields.read_all(
            MarketPage.settings_fields,
            {"destaques": [{"simbolo": "PETR4.SA", "nome": "Petrobras"}], "acoes": []},
            "blocos.mercado",
        )
    )
    assert [q.symbol for q in custom.quotes] == [
        "PETR4.SA",
        "^BVSP",
        "^GSPC",
        "BRL=X",
        "BTC-USD",
    ], (
        "the market-hours chips keep their quotes even when the screen no longer shows them"
    )
    assert all(
        symbol in {q.symbol for q in WorldMarketPage.QUOTES}
        for _, symbol in WORLD_CHIPS
    )
    assert dict((n, s) for n, s, _ in page.status_items(now))["B3"] == "aberto"
    page.found = {**page.found, "^BVSP": [yesterday, yesterday, yesterday]}
    assert dict((n, s) for n, s, _ in page.status_items(now))["B3"].startswith("abre ")
    assert (
        dict((n, s) for n, s, _ in chips({"X": []}, MARKET_CHIPS, now))["B3"]
        == "sem dados"
    ), "fetched, but this quote is missing"

    calls = []

    def fake_fetch(symbol, period, when):
        calls.append(symbol)
        if symbol == "^RUT":
            raise SourceError("HTTP 404")
        return rising

    found, problems = gather_quotes(
        WorldMarketPage.QUOTES + (Quote("^GSPC", "S&P de novo"),), now, fake_fetch
    )
    assert calls.count("^GSPC") == len(PERIODS), (
        "a symbol listed twice is fetched once per period"
    )
    assert problems == ["Russell 2000: HTTP 404"] and isinstance(
        found["^RUT"][0], SourceError
    )

    page.draw(ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 1888, 992), now)
    image = Image.new("RGB", (1888, 992), ui.BACKGROUND)
    page.draw_compact(ImageDraw.Draw(image), (0, 0, 560, 992), now)  # beside the rates
    world = WorldMarketPage()
    world.found = {
        quote.symbol: [rising, yesterday, rising] for quote in WorldMarketPage.QUOTES
    }
    world.draw(ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 1888, 992), now)
    stacks = [
        [(title, dict(WORLD_GROUPS)[title]) for title in column]
        for column in WORLD_COLUMNS
    ]
    for layout_columns in (WORLD_COLUMNS, WORLD_COMPACT_COLUMNS):
        assert sorted(t for column in layout_columns for t in column) == sorted(
            t for t, _ in WORLD_GROUPS
        ), "every group appears once in each layout"
    world.draw_compact(
        ImageDraw.Draw(Image.new("RGB", (938, 992))), (0, 0, 938, 992), now
    )
    row = world_row_height(992, stacks)
    assert all(
        len(stack) * CARD_HEADER
        + (len(stack) - 1) * ui.GUTTER
        + sum(len(q) for _, q in stack) * row
        <= 992
        for stack in stacks
    ), "every column fits the page"
    print("page_market selftest ok")
    return image


def selftest_world() -> Image.Image:
    now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    rising = Series(0.0321, 0.0320, (0.0319, 0.0321), now)
    page = WorldMarketPage()
    page.found = {
        quote.symbol: [rising, rising, rising] for quote in WorldMarketPage.QUOTES
    }
    image = Image.new("RGB", (1888, 992), ui.BACKGROUND)
    page.draw(ImageDraw.Draw(image), (0, 0, 1888, 992), now)
    assert net.br_number(0.0321 * 100) == "3,2100"
    return image

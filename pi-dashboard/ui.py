"""Pieces shared by the dashboard pages: the Page base, palette, fonts, cards, charts, news lists."""

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sources import Feed

BACKGROUND = (13, 17, 23)
CARD = (22, 27, 34)
GRID = (60, 68, 78)
DIM = (139, 148, 158)
TEXT = (230, 237, 243)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
AMBER = (210, 153, 34)
BLUE = (88, 166, 255)
BADGE_COLORS = (
    (88, 166, 255),
    (210, 153, 34),
    (163, 113, 247),
    (219, 97, 162),
    (57, 197, 187),
    (240, 136, 62),
)

WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
MONTHS = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)

GUTTER = 12
LOADING = "carregando…"
STRIPE = (30, 36, 45)  # between CARD and GRID


class Page:
    """One screen of the carousel.

    refresh() runs on the background thread and must replace whole attributes (never mutate
    them in place), so draw() on the display thread always sees a complete snapshot.
    """

    name = ""
    live = False  # True: the carousel redraws this block alone at the screen's ao_vivo interval
    settings_fields: tuple = ()  # fields.Field list: what [blocos.<id>] and the settings page set
    cities: tuple = ()  # fields.CityFields: filled from the city name when the page saves

    def apply_settings(self, values: dict) -> None:
        """Values already checked by fields.read_all; replace whole attributes, as refresh() does."""

    def refresh(self, now: datetime) -> None:
        """Fetch whatever is stale; record failures for problems() instead of raising."""

    def problems(self) -> list[str]:
        return []

    def live_key(self) -> object:
        """Changes whenever a live redraw would look different; None: redraw on every tick."""
        return None

    def status_items(self, now: datetime) -> list[tuple[str, str, tuple]]:
        """(name, state, dot color) chips this page adds to the status bar."""
        return []

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        raise NotImplementedError

    def draw_compact(
        self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime
    ) -> None:
        """The block when it shares a screen; by default the full drawing in the smaller box."""
        self.draw(draw, box, now)

    def layout(self, box: tuple, mode: str) -> None:
        """Called once with the block's box, for blocks whose data depends on their size (the radar map)."""

    def compose(
        self, image: Image.Image, box: tuple, now: datetime, mode: str = "cheio"
    ) -> None:
        """Paint the block onto the frame; blocks that paste pictures (the radar map) override this."""
        draw = ImageDraw.Draw(image)
        if mode == "compacto":
            self.draw_compact(draw, box, now)
        else:
            self.draw(draw, box, now)


FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


@lru_cache(maxsize=None)
def font(
    size: int, bold: bool = False, condensed: bool = False
) -> ImageFont.FreeTypeFont:
    family = "DejaVuSansCondensed" if condensed else "DejaVuSans"
    return ImageFont.truetype(
        str(FONT_DIR / f"{family}{'-Bold' if bold else ''}.ttf"), size
    )


def blend(color: tuple, background: tuple, amount: float) -> tuple:
    return tuple(round(b + (c - b) * amount) for c, b in zip(color, background))


def trend_color(change: float) -> tuple:
    if change > 0:
        return GREEN
    return RED if change < 0 else DIM


def arrow(change: float) -> str:
    if change > 0:
        return "▲"
    return "▼" if change < 0 else "■"


def decimal(value: float, places: int) -> str:
    """Brazilian decimal comma: 6,30 / 0,1 (no thousands separator; see sources.br_number)."""
    return f"{value:.{places}f}".replace(".", ",")


def placeholder(
    draw: ImageDraw.ImageDraw,
    box: tuple,
    text: str = "",
    size: int = 20,
    below: int = 80,
) -> None:
    """A card's body while its data is missing: the fetch error, or "carregando…" before the first."""
    draw.text(
        (box[0] + 16, box[1] + below),
        text or LOADING,
        font=font(size),
        fill=DIM,
        anchor="ls",
    )


def fit(text: str, face: ImageFont.FreeTypeFont, max_width: float) -> str:
    """The text, or its longest prefix plus an ellipsis that fits max_width pixels."""
    if face.getlength(text) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if face.getlength(text[:middle].rstrip() + "…") <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def card(draw: ImageDraw.ImageDraw, box: tuple, title: str = "") -> None:
    draw.rounded_rectangle(box, radius=12, fill=CARD)
    if title:
        draw.text(
            (box[0] + 16, box[1] + 32),
            title,
            font=font(20, bold=True),
            fill=TEXT,
            anchor="ls",
        )


def stripe(draw, box: tuple, index: int) -> None:
    """Faint band behind every other row: binds a name to values and charts far to its right."""
    if index % 2 == 0:
        draw.rounded_rectangle(box, radius=6, fill=STRIPE)


def divider(draw, x: float, top: float, bottom: float) -> None:
    """Thin line between side-by-side columns, so a row's end never reads as the next one's start."""
    draw.line([(x, top), (x, bottom)], fill=GRID, width=1)


def line_chart(
    draw,
    box: tuple,
    values: tuple[float, ...],
    color: tuple,
    baseline: float | None = None,
    fill: float = 0.22,
) -> None:
    """Line with a shaded area under it; a dashed reference line at `baseline` when given."""
    left, top, right, bottom = box
    if not values:
        return
    low, high = min(values), max(values)
    if baseline is not None:
        low, high = min(low, baseline), max(high, baseline)
    span = high - low
    inner = bottom - top - 6

    def y_of(value: float) -> float:
        return bottom - 3 - ((value - low) / span * inner if span else inner / 2)

    step = (right - left) / max(len(values) - 1, 1)
    points = [(left + index * step, y_of(value)) for index, value in enumerate(values)]
    if fill:
        draw.polygon(
            [(left, bottom), *points, (points[-1][0], bottom)],
            fill=blend(color, CARD, fill),
        )
    if baseline is not None:
        base = y_of(baseline)
        for x in range(int(left), int(right), 8):
            draw.line([(x, base), (min(x + 4, right), base)], fill=GRID, width=1)
    if len(points) > 1:
        draw.line(points, fill=color, width=2, joint="curve")
    last_x, last_y = points[-1]
    draw.ellipse([last_x - 3, last_y - 3, last_x + 3, last_y + 3], fill=color)


def badge(draw, x: float, center_y: float, letter: str, color: tuple) -> None:
    draw.ellipse([x, center_y - 9, x + 18, center_y + 9], fill=color)
    draw.text(
        (x + 9, center_y),
        letter,
        font=font(12, bold=True),
        fill=BACKGROUND,
        anchor="mm",
    )


def feed_colors(feeds: tuple[Feed, ...]) -> dict[str, tuple]:
    return {
        feed.letter: BADGE_COLORS[index % len(BADGE_COLORS)]
        for index, feed in enumerate(feeds)
    }


def selftest() -> None:
    face = font(16)
    long_title = "Reforma tributária: MEI fica dispensado de emitir nota fiscal " * 4
    cut = fit(long_title, face, 400)
    assert cut.endswith("…") and face.getlength(cut) <= 400 < face.getlength(
        cut[:-1] + "x…"
    )
    assert fit("curto", face, 400) == "curto"
    assert blend((255, 0, 0), (0, 0, 0), 0.5) == (128, 0, 0)
    assert trend_color(1) == GREEN and trend_color(-1) == RED and arrow(0) == "■"
    print("ui selftest ok")

"""Lottery page: the latest draw of each Caixa game and the prize of the next one
(Caixa's public results API, unofficial, no key)."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import fields
import lottery_data
import ui
from lottery_data import Draw
from net import SourceError
from PIL import Image, ImageDraw

# (API name, label, color of the game's own branding)
GAMES = (
    ("megasena", "Mega-Sena", (32, 152, 105)),
    ("lotofacil", "Lotofácil", (147, 0, 137)),
    ("quina", "Quina", (38, 0, 119)),
    ("maismilionaria", "+Milionária", (46, 50, 120)),
    ("duplasena", "Dupla Sena", (166, 0, 33)),
    ("lotomania", "Lotomania", (247, 130, 24)),
    ("timemania", "Timemania", (0, 255, 72)),
    ("diadesorte", "Dia de Sorte", (203, 133, 46)),
    ("supersete", "Super Sete", (168, 207, 69)),
)
REFRESH_EVERY = timedelta(minutes=30)
ONE_LINE_WIDTH = 1300
RETRY_AFTER = timedelta(minutes=5)


def money(value: float) -> str:
    """Prizes in words the way people say them: R$ 230 milhões, R$ 2,5 milhões, R$ 850 mil."""
    if value >= 1_000_000:
        millions = value / 1_000_000
        text = (
            f"{millions:.0f}"
            if millions >= 10 or millions == int(millions)
            else ui.decimal(millions, 1)
        )
        return f"R$ {text} {'milhão' if text == '1' else 'milhões'}"
    if value >= 1_000:
        return f"R$ {value / 1_000:.0f} mil"
    return "R$ " + ui.decimal(value, 2)


def draw_balls(draw, box: tuple, numbers: tuple[str, ...], color: tuple) -> float:
    """Numbers as balls, as many per row as fit; returns the y below the last row."""
    left, top, right, _ = box
    size = 40 if len(numbers) <= 7 else 34 if len(numbers) <= 15 else 28
    gap = 6
    per_row = max(int((right - left + gap) // (size + gap)), 1)
    for index, number in enumerate(numbers):
        row, column = divmod(index, per_row)
        x, y = left + column * (size + gap), top + row * (size + gap)
        draw.ellipse([x, y, x + size, y + size], fill=color)
        draw.text(
            (x + size / 2, y + size / 2),
            number,
            font=ui.font(int(size * 0.45), bold=True),
            fill=ui.TEXT,
            anchor="mm",
        )
    rows = -(-len(numbers) // per_row)
    return top + rows * (size + gap)


def text_color_on(color: tuple) -> tuple:
    """Dark text on light brand colors (Timemania, Super Sete), light text otherwise."""
    red, green, blue = color
    return (
        ui.BACKGROUND if 0.299 * red + 0.587 * green + 0.114 * blue > 140 else ui.TEXT
    )


def by_next_prize(games: tuple, draws: dict[str, Draw]) -> tuple:
    """Biggest next prize first; a game without a result yet goes last."""
    return tuple(
        sorted(
            games,
            key=lambda game: -draws[game[0]].next_estimate
            if game[0] in draws
            else float("inf"),
        )
    )


def outcome_of(result: Draw) -> tuple[str, tuple]:
    if result.accumulated:
        return "Acumulou!", ui.AMBER
    plural = "es" if result.top_winners != 1 else ""
    return (
        f"{result.top_winners} ganhador{plural} · {money(result.top_prize)} cada",
        ui.GREEN,
    )


def next_draw_day(day: date, today: date) -> str:
    return "hoje" if day == today else f"{ui.WEEKDAYS[day.weekday()][:3]} {day:%d/%m}"


def draw_game_row(
    draw,
    box: tuple,
    label: str,
    color: tuple,
    result: Draw | None,
    error: str,
    today: date,
) -> None:
    """One game on one line: brand chip, numbers, extras, the result and the next prize."""
    left, top, right, bottom = box
    middle = (top + bottom) / 2
    chip_right = left + 160
    draw.rounded_rectangle(
        (left, top + 4, chip_right, bottom - 4), radius=8, fill=color
    )
    draw.text(
        (left + 12, middle),
        label,
        font=ui.font(16, bold=True),
        fill=text_color_on(color),
        anchor="lm",
    )
    if result is None:
        draw.text(
            (chip_right + 16, middle),
            error or "carregando…",
            font=ui.font(15),
            fill=ui.DIM,
            anchor="lm",
        )
        return
    # Fixed columns sized for the widest text ("R$ 230 milhões", "próximo sáb 24/10"), aligned row to row.
    prize_right, when_right, outcome_right = right - 12, right - 196, right - 360
    if result.next_day is not None:
        draw.text(
            (prize_right, middle),
            money(result.next_estimate),
            font=ui.font(17, bold=True),
            fill=ui.TEXT,
            anchor="rm",
        )
        draw.text(
            (when_right, middle),
            f"próximo {next_draw_day(result.next_day, today)}",
            font=ui.font(13),
            fill=ui.DIM,
            anchor="rm",
        )
    outcome, outcome_color = outcome_of(result)
    outcome_face = ui.font(14, bold=True)
    draw.text(
        (outcome_right, middle),
        outcome,
        font=outcome_face,
        fill=outcome_color,
        anchor="rm",
    )
    size = min(bottom - top - 10, 28)
    ball_color = ui.blend(color, ui.CARD, 0.75)
    x = chip_right + 14
    for group in (result.numbers, result.second_numbers):
        for number in group:
            draw.ellipse(
                (x, middle - size / 2, x + size, middle + size / 2), fill=ball_color
            )
            draw.text(
                (x + size / 2, middle),
                number,
                font=ui.font(int(size * 0.46), bold=True),
                fill=ui.TEXT,
                anchor="mm",
            )
            x += size + 4
        x += 12  # Dupla Sena: a gap between the two draws
    extras = " · ".join(
        part
        for part in (
            "trevos " + " ".join(result.clovers) if result.clovers else "",
            result.extra.title(),
        )
        if part
    )
    if extras:
        room = outcome_right - outcome_face.getlength(outcome) - 16 - x
        draw.text(
            (x, middle),
            ui.fit(extras, ui.font(14, bold=True), room),
            font=ui.font(14, bold=True),
            fill=ui.TEXT,
            anchor="lm",
        )


def draw_game_lines(
    draw,
    box: tuple,
    label: str,
    color: tuple,
    result: Draw | None,
    error: str,
    today: date,
) -> None:
    """One game on two lines, for a narrow column: chip and next prize, then the numbers,
    then the result and extras in small type."""
    left, top, right, bottom = box
    height = bottom - top
    chip_bottom = top + min(36, height * 0.34)
    draw.rounded_rectangle(
        (left, top + 4, left + 160, chip_bottom), radius=8, fill=color
    )
    draw.text(
        (left + 12, (top + 4 + chip_bottom) / 2),
        label,
        font=ui.font(15, bold=True),
        fill=text_color_on(color),
        anchor="lm",
    )
    if result is None:
        draw.text(
            (left + 176, (top + 4 + chip_bottom) / 2),
            error or "carregando…",
            font=ui.font(14),
            fill=ui.DIM,
            anchor="lm",
        )
        return
    if result.next_day is not None:
        prize_face = ui.font(17, bold=True)
        prize = money(result.next_estimate)
        line = (top + 4 + chip_bottom) / 2
        draw.text((right - 12, line), prize, font=prize_face, fill=ui.TEXT, anchor="rm")
        draw.text(
            (right - 24 - prize_face.getlength(prize), line),
            f"próximo {next_draw_day(result.next_day, today)}",
            font=ui.font(13),
            fill=ui.DIM,
            anchor="rm",
        )
    size = min(28, (height - (chip_bottom - top) - 30) * 0.9)
    middle = chip_bottom + 6 + size / 2
    ball_color = ui.blend(color, ui.CARD, 0.75)
    x = left
    for group in (result.numbers, result.second_numbers):
        for number in group:
            draw.ellipse(
                (x, middle - size / 2, x + size, middle + size / 2), fill=ball_color
            )
            draw.text(
                (x + size / 2, middle),
                number,
                font=ui.font(int(size * 0.46), bold=True),
                fill=ui.TEXT,
                anchor="mm",
            )
            x += size + 4
        x += 12
    outcome, outcome_color = outcome_of(result)
    extras = [
        part
        for part in (
            "trevos " + " ".join(result.clovers) if result.clovers else "",
            result.extra.title(),
        )
        if part
    ]
    small = ui.font(13, bold=True)
    draw.text((left, bottom - 8), outcome, font=small, fill=outcome_color, anchor="ls")
    if extras:
        extras_left = left + small.getlength(outcome) + 16
        draw.text(
            (extras_left, bottom - 8),
            ui.fit(" · ".join(extras), small, right - extras_left - 12),
            font=small,
            fill=ui.TEXT,
            anchor="ls",
        )


def draw_game(
    draw,
    box: tuple,
    label: str,
    color: tuple,
    result: Draw | None,
    error: str,
    today: date,
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box)
    draw.rounded_rectangle((left, top, right, top + 46), radius=12, fill=color)
    draw.rectangle((left, top + 30, right, top + 46), fill=color)
    draw.text(
        (left + 16, top + 32),
        label,
        font=ui.font(21, bold=True),
        fill=text_color_on(color),
        anchor="ls",
    )
    if result is None:
        draw.text(
            (left + 16, top + 90),
            error or "carregando…",
            font=ui.font(17),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    draw.text(
        (right - 16, top + 32),
        f"concurso {result.number} · {result.day:%d/%m}",
        font=ui.font(15),
        fill=text_color_on(color),
        anchor="rs",
    )
    ball_color = ui.blend(color, ui.CARD, 0.75)
    y = draw_balls(
        draw, (left + 16, top + 60, right - 16, bottom), result.numbers, ball_color
    )
    if result.second_numbers:
        draw.text(
            (left + 16, y + 14),
            "2º sorteio",
            font=ui.font(13),
            fill=ui.DIM,
            anchor="ls",
        )
        y = draw_balls(
            draw,
            (left + 16, y + 20, right - 16, bottom),
            result.second_numbers,
            ball_color,
        )
    if result.clovers:
        draw.text(
            (left + 16, y + 18),
            "Trevos: " + "  ".join(result.clovers),
            font=ui.font(16, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        y += 26
    if result.extra:
        draw.text(
            (left + 16, y + 18),
            result.extra,
            font=ui.font(16, bold=True),
            fill=ui.TEXT,
            anchor="ls",
        )
        y += 26
    outcome, outcome_color = outcome_of(result)
    draw.text(
        (left + 16, bottom - 50),
        outcome,
        font=ui.font(17, bold=True),
        fill=outcome_color,
        anchor="ls",
    )
    if result.next_day is not None:
        draw.text(
            (left + 16, bottom - 18),
            f"Próximo {next_draw_day(result.next_day, today)}:",
            font=ui.font(17),
            fill=ui.DIM,
            anchor="ls",
        )
        draw.text(
            (right - 16, bottom - 16),
            money(result.next_estimate),
            font=ui.font(24, bold=True),
            fill=ui.TEXT,
            anchor="rs",
        )


class LotteryPage(ui.Page):
    name = "Loteria"
    settings_fields = (
        fields.Field(
            "jogos",
            "Jogos mostrados",
            "escolhas",
            [game for game, _, _ in GAMES],
            low=1,
            high=len(GAMES),
            options=tuple((game, label) for game, label, _ in GAMES),
        ),
    )

    def __init__(self, fetch_draw=lottery_data.fetch_draw) -> None:
        self.fetch_draw = fetch_draw
        self.games = GAMES
        self.draws: dict[str, Draw] = {}
        self.errors: dict[str, str] = {}
        self.next_fetch_at: datetime | None = None

    def apply_settings(self, values: dict) -> None:
        games = tuple(game for game in GAMES if game[0] in values["jogos"])
        if games != self.games:  # a game added on the settings page is fetched now
            self.games, self.next_fetch_at = games, None

    def refresh(self, now: datetime) -> None:
        if self.next_fetch_at is not None and now < self.next_fetch_at:
            return
        draws, errors = dict(self.draws), {}
        for game, label, _ in self.games:
            try:
                draws[game] = self.fetch_draw(game)
            except SourceError as err:
                errors[game] = f"{label}: {err}"
        self.draws, self.errors = draws, errors
        self.next_fetch_at = now + (RETRY_AFTER if errors else REFRESH_EVERY)

    def problems(self) -> list[str]:
        return list(self.errors.values())

    def draw(self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime) -> None:
        left, top, right, bottom = box
        width = (right - left - 2 * ui.GUTTER) / 3
        height = (bottom - top - 2 * ui.GUTTER) / 3
        today = now.astimezone().date()
        for index, (game, label, color) in enumerate(
            by_next_prize(self.games, self.draws)
        ):
            row, column = divmod(index, 3)
            x, y = left + column * (width + ui.GUTTER), top + row * (height + ui.GUTTER)
            draw_game(
                draw,
                (x, y, x + width, y + height),
                label,
                color,
                self.draws.get(game),
                self.errors.get(game, ""),
                today,
            )

    def draw_compact(
        self, draw: ImageDraw.ImageDraw, box: tuple, now: datetime
    ) -> None:
        left, top, right, bottom = box
        ui.card(draw, box, "Loterias")
        today = now.astimezone().date()
        row = (bottom - top - 50) / len(self.games)
        # One line per game needs room for 20 Lotomania balls plus the prize columns.
        draw_row = draw_game_row if right - left >= ONE_LINE_WIDTH else draw_game_lines
        for index, (game, label, color) in enumerate(
            by_next_prize(self.games, self.draws)
        ):
            y = top + 44 + index * row
            ui.stripe(draw, (left + 8, y, right - 8, y + row), index)
            draw_row(
                draw,
                (left + 16, y, right - 8, y + row),
                label,
                color,
                self.draws.get(game),
                self.errors.get(game, ""),
                today,
            )


def selftest() -> Image.Image:
    now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    assert (
        money(230_000_000) == "R$ 230 milhões" and money(2_500_000) == "R$ 2,5 milhões"
    )
    assert (
        money(1_000_000) == "R$ 1 milhão"
        and money(850_000) == "R$ 850 mil"
        and money(12.5) == "R$ 12,50"
    )
    assert (
        text_color_on((0, 255, 72)) == ui.BACKGROUND
        and text_color_on((38, 0, 119)) == ui.TEXT
    )

    def fake(game: str) -> Draw:
        many = {"lotofacil": 15, "lotomania": 20}.get(game, 6)
        return Draw(
            3073,
            date(2026, 10, 22),
            tuple(f"{n:02d}" for n in range(1, many + 1)),
            ("21", "34", "41", "45", "46", "50") if game == "duplasena" else (),
            ("2", "3") if game == "maismilionaria" else (),
            "TOMBENSE /MG" if game == "timemania" else "",
            0 if game != "quina" else 2,
            0.0 if game != "quina" else 4_300_000.0,
            game != "quina",
            date(2026, 10, 24),
            230_000_000.0,
        )

    page = LotteryPage(fake)
    page.refresh(now)
    assert len(page.draws) == len(GAMES) and page.next_fetch_at == now + REFRESH_EVERY
    few = LotteryPage(fake)
    few.apply_settings(
        fields.read_all(
            LotteryPage.settings_fields, {"jogos": ["quina", "megasena"]}, "b"
        )
    )
    few.refresh(now)
    assert [game for game, _, _ in few.games] == ["megasena", "quina"] and set(
        few.draws
    ) == {"megasena", "quina"}

    def flaky(game: str) -> Draw:
        if game == "lotomania":
            raise SourceError("HTTP 503")
        return fake(game)

    page.fetch_draw = flaky
    page.next_fetch_at = None
    page.refresh(now)
    assert page.problems() == ["Lotomania: HTTP 503"] and "lotomania" in page.draws, (
        "keeps the last good draw"
    )
    assert page.next_fetch_at == now + RETRY_AFTER
    assert outcome_of(fake("megasena")) == ("Acumulou!", ui.AMBER)
    assert outcome_of(fake("quina")) == ("2 ganhadores · R$ 4,3 milhões cada", ui.GREEN)
    assert next_draw_day(date(2026, 10, 24), date(2026, 10, 24)) == "hoje"
    prizes = {"megasena": 2e6, "quina": 230e6}
    ranked = by_next_prize(
        GAMES[:3],
        {
            game: replace(fake(game), next_estimate=value)
            for game, value in prizes.items()
        },
    )
    assert [game for game, _, _ in ranked] == ["quina", "megasena", "lotofacil"], (
        "biggest next prize first, a game with no result last"
    )
    assert next_draw_day(date(2026, 10, 24), date(2026, 10, 23)) == "sáb 24/10"
    page.draw(ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 1888, 992), now)
    page.draw_compact(
        ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 540, 1888, 992), now
    )
    image = Image.new("RGB", (1888, 992), ui.BACKGROUND)
    page.draw_compact(
        ImageDraw.Draw(image), (1140, 0, 1888, 992), now
    )  # beside the fuel block
    LotteryPage(fake).draw(
        ImageDraw.Draw(Image.new("RGB", (1888, 992))), (0, 0, 1888, 992), now
    )
    print("page_lottery selftest ok")
    return image

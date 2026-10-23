"""Carousel of dashboard screens, drawn with Pillow straight onto the HDMI framebuffer.

Run by dashboard.service on tty1. What is shown, how screens are split into blocks and
every timing come from carrossel.toml (see layout.py). Each block fetches its data on its
own clock in background threads; the display loop shows each screen for its time, redraws
it every `redesenho` seconds, redraws only its live blocks every `ao_vivo` seconds, moves
the progress line every PROGRESS_SECONDS (rewriting only those rows), and draws the next
screen just before the switch so the change is instant.

`screen.py preview DIR` saves one PNG per screen from live data; `screen.py selftest [DIR]`
runs every module's checks and saves the blocks drawn with fake data.
"""

import fcntl
import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import admin
import fields
import layout
import page_fuel
import page_lottery
import page_market
import page_news
import page_rates
import page_system
import page_weather
import sources
import ui
from layout import Plan, Screen
from PIL import Image, ImageDraw, ImageMath

WIDTH, HEIGHT = 1920, 1080
FRAMEBUFFER = Path("/dev/fb0")
STRIDE = WIDTH * 2  # bytes per framebuffer row (16 bits per pixel)
MARGIN = 16
STATUS_BAND = (12, 60)
STATUS_ROWS = 66  # framebuffer rows rewritten when only the progress line moves
CONTENT_BOX = (MARGIN, 72, WIDTH - MARGIN, HEIGHT - MARGIN)
PROGRESS_SECONDS = 2
PRERENDER_SECONDS = 4
SCHEDULER_TICK_SECONDS = 0.5
REFRESH_WORKERS = 4
KDSETMODE, KD_TEXT, KD_GRAPHICS = 0x4B3A, 0, 1

log = logging.getLogger("dashboard")


def build_blocks() -> dict[str, ui.Page]:
    """Every block the carousel file can name; blocks that share data share one source object."""
    news = page_news.NewsCache()
    weather = page_weather.WeatherPage()
    return {
        "mercado": page_market.MarketPage(news),
        "mercado_mundo": page_market.WorldMarketPage(),
        "juros": page_rates.RatesPage(),
        "noticias": page_news.NewsPage(news),
        "clima": weather,
        "clima_detalhes": page_weather.WeatherDetailsPage(weather),
        "combustivel": page_fuel.FuelPage(),
        "loteria": page_lottery.LotteryPage(),
        "impressora": page_system.PrinterPanel(),
        "pi": page_system.PiPanel(),
    }


def screen_problems(screen: Screen) -> list[str]:
    return [problem for part in screen.parts for problem in part.page.problems()]


def screen_chips(screen: Screen, now: datetime) -> list[tuple[str, str, tuple]]:
    chips, seen = [], set()
    for part in screen.parts:
        for chip in part.page.status_items(now):
            if chip[0] not in seen:
                seen.add(chip[0])
                chips.append(chip)
    return chips


def draw_status(
    draw: ImageDraw.ImageDraw,
    screens: tuple[Screen, ...],
    index: int,
    progress: float,
    now: datetime,
) -> None:
    top, bottom = STATUS_BAND
    left, right = MARGIN, WIDTH - MARGIN
    draw.rounded_rectangle((left, top, right, bottom), radius=10, fill=ui.CARD)
    baseline = bottom - 16
    local = now.astimezone()
    x = left + 16
    draw.text(
        (x, baseline + 2),
        f"{local:%H:%M}",
        font=ui.font(28, bold=True),
        fill=ui.TEXT,
        anchor="ls",
    )
    x += ui.font(28, bold=True).getlength("00:00") + 14
    date_line = (
        f"{ui.WEEKDAYS[local.weekday()]}, {local.day} de {ui.MONTHS[local.month - 1]}"
    )
    draw.text((x, baseline), date_line, font=ui.font(18), fill=ui.DIM, anchor="ls")
    x += ui.font(18).getlength(date_line) + 36
    screen = screens[index]
    # Right side first: where the carousel is (one dot per screen) and what comes next.
    following = screens[(index + 1) % len(screens)].name
    tab_x = right - 16
    draw.text(
        (tab_x, baseline),
        f"depois: {following}",
        font=ui.font(16),
        fill=ui.DIM,
        anchor="rs",
    )
    tab_x -= ui.font(16).getlength(f"depois: {following}") + 18
    draw.text(
        (tab_x, baseline),
        screen.name,
        font=ui.font(18, bold=True),
        fill=ui.TEXT,
        anchor="rs",
    )
    tab_x -= ui.font(18, bold=True).getlength(screen.name) + 16
    for position in reversed(range(len(screens))):
        radius = 5 if position == index else 3
        draw.ellipse(
            [tab_x - 2 * radius, baseline - 7 - radius, tab_x, baseline - 7 + radius],
            fill=ui.BLUE if position == index else ui.GRID,
        )
        tab_x -= 16
    limit = tab_x - 24
    for name, state, color in screen_chips(screen, now):
        width = (
            18
            + ui.font(17, bold=True).getlength(name + " ")
            + ui.font(17).getlength(state)
        )
        if x + width > limit:
            break
        draw.ellipse([x, baseline - 13, x + 12, baseline - 1], fill=color)
        x += 18
        draw.text(
            (x, baseline), name, font=ui.font(17, bold=True), fill=ui.TEXT, anchor="ls"
        )
        x += ui.font(17, bold=True).getlength(name + " ")
        draw.text((x, baseline), state, font=ui.font(17), fill=ui.DIM, anchor="ls")
        x += ui.font(17).getlength(state) + 28
    problems = screen_problems(screen)
    if problems and limit - x > 60:
        note = ui.fit("✘ " + " · ".join(problems), ui.font(16), limit - x)
        draw.text((x, baseline), note, font=ui.font(16), fill=ui.RED, anchor="ls")
    track = (left + 10, bottom - 4, right - 10)
    draw.line(
        [
            (track[0], track[1]),
            (track[0] + (track[2] - track[0]) * min(progress, 1.0), track[1]),
        ],
        fill=ui.BLUE,
        width=3,
    )


def render_screen(
    screens: tuple[Screen, ...], index: int, progress: float, now: datetime
) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), ui.BACKGROUND)
    draw_status(ImageDraw.Draw(image), screens, index, progress, now)
    screen = screens[index]
    for part, box in zip(screen.parts, screen.boxes(CONTENT_BOX)):
        part.page.compose(image, box, now, part.mode)
    return image


def render_status(
    screens: tuple[Screen, ...], index: int, progress: float, now: datetime
) -> Image.Image:
    image = Image.new("RGB", (WIDTH, STATUS_ROWS), ui.BACKGROUND)
    draw_status(ImageDraw.Draw(image), screens, index, progress, now)
    return image


def render_block(part: layout.Part, box: tuple, now: datetime) -> Image.Image:
    """One block alone, at its own size, for a live redraw."""
    width, height = box[2] - box[0], box[3] - box[1]
    image = Image.new("RGB", (width, height), ui.BACKGROUND)
    part.page.compose(image, (0, 0, width, height), now, part.mode)
    return image


def rgb565(image: Image.Image) -> bytes:
    """The HDMI framebuffer is 16 bits per pixel, little-endian 5-6-5."""
    red, green, blue = (channel.convert("I") for channel in image.split())
    packed = ImageMath.lambda_eval(
        lambda c: (c["r"] >> 3 << 11) | (c["g"] >> 2 << 5) | (c["b"] >> 3),
        r=red,
        g=green,
        b=blue,
    )
    return packed.convert("I;16").tobytes()


class Framebuffer:
    """Owns the console while running: graphics mode stops the text console drawing over us.

    Rows are contiguous (stride = width x 2 bytes): writing from offset 0 replaces the top rows
    (the status strip), and a block is written row by row at its own offset.
    """

    def __init__(self, console_fd: int = 0, device: Path = FRAMEBUFFER):
        self.console_fd = console_fd
        self.device = device

    def __enter__(self) -> "Framebuffer":
        fcntl.ioctl(self.console_fd, KDSETMODE, KD_GRAPHICS)
        return self

    def __exit__(self, *_exc) -> None:
        fcntl.ioctl(self.console_fd, KDSETMODE, KD_TEXT)

    def write(self, pixels: bytes) -> None:
        with self.device.open("r+b") as screen:
            screen.write(pixels)

    def write_region(self, pixels: bytes, x: int, y: int, width: int) -> None:
        row_bytes = width * 2
        with self.device.open("r+b") as screen:
            for row in range(len(pixels) // row_bytes):
                screen.seek((y + row) * STRIDE + x * 2)
                screen.write(pixels[row * row_bytes : (row + 1) * row_bytes])


class Carousel:
    """Shows each screen for its time; clock, sleep and wall time are injectable for tests."""

    def __init__(
        self,
        screens: tuple[Screen, ...],
        output,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        reload: Callable[[], tuple[Screen, ...] | None] = lambda: None,
    ):
        self.screens, self.output, self.reload = screens, output, reload
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.drawn: dict[
            str, object
        ] = {}  # block id -> live_key of what is on screen now

    def encode(self, draw: Callable[[], Image.Image]) -> bytes | None:
        try:
            return rgb565(draw())
        except Exception:
            # A block that fails to draw must not stop the carousel; the last frame stays up.
            log.exception("drawing failed")
            return None

    def show(self, pixels: bytes | None) -> None:
        if pixels is not None:
            self.output.write(pixels)

    def show_screen(self, pixels: bytes | None) -> None:
        """A whole frame: every live block on it is drawn fresh."""
        self.show(pixels)
        if pixels is not None:
            self.drawn = {}

    def show_live(self, index: int) -> None:
        for part, box in self.screens[index].live_parts(CONTENT_BOX):
            key = part.page.live_key()
            if key is not None and self.drawn.get(part.block) == key:
                continue  # unchanged: a redraw costs ~0.3 s of the Pi's CPU for nothing
            pixels = self.encode(lambda: render_block(part, box, self.wall()))
            if pixels is not None:
                self.output.write_region(pixels, box[0], box[1], box[2] - box[0])
                self.drawn[part.block] = key

    def run(self, screens_to_show: int | None = None) -> None:
        index, start, shown = 0, self.clock(), 0
        frame = self.encode(lambda: render_screen(self.screens, 0, 0.0, self.wall()))
        while screens_to_show is None or shown < screens_to_show:
            screen = self.screens[index]
            end = start + screen.seconds
            self.show_screen(frame)
            next_full = start + screen.redraw
            next_live = start + screen.live if screen.live else float("inf")
            next_status = start + PROGRESS_SECONDS
            while True:
                due = min(next_full, next_live, next_status, end - PRERENDER_SECONDS)
                self.sleep(max(due - self.clock(), 0))
                moment = self.clock()
                if moment >= end - PRERENDER_SECONDS:
                    break
                progress = (moment - start) / screen.seconds
                if moment >= next_full:
                    self.show_screen(
                        self.encode(
                            lambda: render_screen(
                                self.screens, index, progress, self.wall()
                            )
                        )
                    )
                    next_full += screen.redraw
                    next_status = moment + PROGRESS_SECONDS
                    if screen.live:
                        next_live = moment + screen.live
                    continue
                if moment >= next_live:
                    self.show_live(index)
                    # A slow draw must not pile up redraws: the next one counts from now.
                    next_live = max(next_live + screen.live, self.clock())
                if moment >= next_status:
                    self.show(
                        self.encode(
                            lambda: render_status(
                                self.screens, index, progress, self.wall()
                            )
                        )
                    )
                    next_status += PROGRESS_SECONDS
            following = (index + 1) % len(self.screens)
            new_screens = self.reload()
            if (
                new_screens
            ):  # carrossel.toml was saved: the new plan starts from its first screen
                self.screens, following = new_screens, 0
            switch_at = self.wall() + timedelta(seconds=max(end - self.clock(), 0))
            frame = self.encode(
                lambda: render_screen(self.screens, following, 0.0, switch_at)
            )
            self.sleep(max(end - self.clock(), 0))
            index, start, shown = following, end, shown + 1


class Refresher(threading.Thread):
    """Refreshes each block on its own clock (carrossel.toml [dados]) in a small thread pool.

    A block still busy (a 35 s Tesouro download) is skipped until it finishes, so a slow source
    never delays the one-second Pi samples.
    """

    def __init__(
        self,
        blocks: dict[str, ui.Page],
        data_every: dict[str, float],
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(daemon=True, name="refresher")
        self.blocks, self.data_every, self.clock = blocks, data_every, clock
        self.next_run = {block: 0.0 for block in data_every}
        self.busy: set[str] = set()
        self.lock = threading.Lock()
        self.reported: dict[str, set[str]] = {}
        self.pool = ThreadPoolExecutor(REFRESH_WORKERS, thread_name_prefix="dados")

    def update(self, data_every: dict[str, float]) -> None:
        """New intervals from a saved carrossel.toml; a block's next run never moves later."""
        moment = self.clock()
        with self.lock:
            self.next_run = {
                block: min(self.next_run.get(block, moment), moment + seconds)
                for block, seconds in data_every.items()
            }
            self.data_every = dict(data_every)

    def refresh_one(self, block: str) -> None:
        page = self.blocks[block]
        try:
            page.refresh(datetime.now(timezone.utc))
        except Exception:
            # One block's bug must not stop the others from updating.
            log.exception("refresh failed: %s", block)
        finally:
            with self.lock:
                self.busy.discard(block)
        self.report(page)

    def due(self) -> list[str]:
        moment = self.clock()
        with self.lock:
            ready = [
                block
                for block, at in self.next_run.items()
                if at <= moment and block not in self.busy
            ]
            for block in ready:
                self.busy.add(block)
                self.next_run[block] = moment + self.data_every[block]
        return ready

    def refresh_all(self) -> None:
        """Every block once, waiting for all (used by preview)."""
        for future in [
            self.pool.submit(self.refresh_one, block) for block in self.data_every
        ]:
            future.result()

    def report(self, page: ui.Page) -> None:
        """Log a source problem when it starts and when it clears, not on every refresh."""
        current = set(page.problems())
        with self.lock:
            before = self.reported.get(page.name, set())
            self.reported[page.name] = current
        for problem in sorted(current - before):
            log.warning("%s: %s", page.name, problem)
        for problem in sorted(before - current):
            log.info("%s: resolvido: %s", page.name, problem)

    def run(self) -> None:
        while True:
            for block in self.due():
                try:
                    self.pool.submit(self.refresh_one, block)
                except RuntimeError:  # the pool is shut down: the process is exiting
                    return
            time.sleep(SCHEDULER_TICK_SECONDS)


def prepare(plan: Plan, blocks: dict[str, ui.Page]) -> None:
    """Settings first: a block whose city changed must lay out its map for the new one."""
    for block, values in plan.settings.items():
        blocks[block].apply_settings(values)
    for screen in plan.screens:
        for part, box in zip(screen.parts, screen.boxes(CONTENT_BOX)):
            part.page.layout(box, part.mode)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    blocks = build_blocks()
    plan = layout.load(blocks)
    watcher = layout.Watcher(blocks)
    prepare(plan, blocks)
    # Blocks show "carregando…" until their first refresh lands.
    refresher = Refresher(blocks, plan.data_every)
    refresher.start()
    try:
        admin.serve(blocks, layout.CONFIG, admin.PORT)
    except OSError:
        log.exception("página de configuração não abriu na porta %s", admin.PORT)

    def reload() -> tuple[Screen, ...] | None:
        try:
            new_plan = watcher.changed()
        except layout.ConfigError as err:
            log.error("carrossel.toml novo ignorado, segue o anterior: %s", err)
            return None
        if new_plan is None:
            return None
        prepare(new_plan, blocks)
        refresher.update(new_plan.data_every)
        log.info("carrossel.toml recarregado: %d telas", len(new_plan.screens))
        return new_plan.screens

    with Framebuffer() as output:
        Carousel(plan.screens, output, reload=reload).run()


def preview(directory: Path) -> None:
    blocks = build_blocks()
    plan = layout.load(blocks)
    prepare(plan, blocks)
    Refresher(blocks, plan.data_every).refresh_all()
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    for index, screen in enumerate(plan.screens):
        render_screen(plan.screens, index, 0.4, now).save(
            directory / f"{index + 1}-{sources.title_key(screen.name)}.png"
        )


class FakeOutput:
    def __init__(self, clock: Callable[[], float]):
        self.clock, self.writes = clock, []

    def write(self, pixels: bytes) -> None:
        self.writes.append(
            (
                round(self.clock(), 1),
                "screen" if len(pixels) == WIDTH * HEIGHT * 2 else "status",
            )
        )

    def write_region(self, pixels: bytes, x: int, y: int, width: int) -> None:
        self.writes.append((round(self.clock(), 1), "live"))


class StubPage(ui.Page):
    def __init__(self, name: str, live: bool = False, key: object = None):
        self.name, self.live, self.key = name, live, key

    def live_key(self) -> object:
        return self.key

    def draw(self, draw, box, now) -> None:
        ui.card(draw, box, self.name)


def selftest(directory: Path) -> None:
    sources.selftest()
    ui.selftest()
    fields.selftest()
    layout.selftest()
    admin.selftest()
    directory.mkdir(parents=True, exist_ok=True)
    renders = (
        ("1-mercado", page_market.selftest),
        ("2-mercado-mundo", page_market.selftest_world),
        ("3-juros", page_rates.selftest),
        ("4-noticias", page_news.selftest),
        ("5-clima", page_weather.selftest),
        ("6-clima-detalhes", page_weather.selftest_details),
        ("7-combustivel", page_fuel.selftest),
        ("8-loteria", page_lottery.selftest),
        ("9-sistema", page_system.selftest),
    )
    for name, render_fake in renders:
        frame = Image.new("RGB", (WIDTH, HEIGHT), ui.BACKGROUND)
        frame.paste(render_fake(), (CONTENT_BOX[0], CONTENT_BOX[1]))
        frame.save(directory / f"{name}.png")

    probe = Image.new("RGB", (4, 1))
    for x, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))):
        probe.putpixel((x, 0), color)
    assert rgb565(probe).hex() == "00f8e0071f00ffff", rgb565(probe).hex()

    elapsed = [0.0]
    a, b, pi = StubPage("A"), StubPage("B"), StubPage("Pi", live=True)
    screens = (
        Screen("A", 60, 30, None, "lado", (layout.Part("a", a, "cheio", 1),)),
        Screen(
            "B e Pi",
            20,
            30,
            1,
            "lado",
            (layout.Part("b", b, "cheio", 1), layout.Part("pi", pi, "compacto", 1)),
        ),
    )
    output = FakeOutput(lambda: elapsed[0])
    carousel = Carousel(
        screens,
        output,
        clock=lambda: elapsed[0],
        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        wall=lambda: datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc),
    )
    carousel.run(screens_to_show=3)
    full = [moment for moment, kind in output.writes if kind == "screen"]
    assert full == [0.0, 30.0, 60.0, 80.0, 110.0], full
    live = [moment for moment, kind in output.writes if kind == "live"]
    assert live == [float(t) for t in range(61, 76)], (
        "live block every second on screen B only",
        live,
    )
    statuses = [moment for moment, kind in output.writes if kind == "status"]
    assert statuses[:3] == [2.0, 4.0, 6.0], statuses
    switches = (60.0, 80.0, 140.0)
    assert not [
        m for m, _ in output.writes for s in switches if s - PRERENDER_SECONDS < m < s
    ], "quiet while the next screen is drawn"
    assert elapsed[0] == 140.0

    replacement = (
        Screen("Nova", 30, 30, None, "lado", (layout.Part("a", a, "cheio", 1),)),
    )
    plans = [None, replacement]
    swapped = FakeOutput(lambda: elapsed[0])
    elapsed[0] = 0.0
    carousel = Carousel(
        screens,
        swapped,
        clock=lambda: elapsed[0],
        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        wall=lambda: datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc),
        reload=lambda: plans.pop(0) if plans else None,
    )
    carousel.run(screens_to_show=3)
    assert carousel.screens == replacement, "a saved file takes over at a screen switch"
    assert elapsed[0] == 60 + 20 + 30, (
        "A, then B, then the new plan's first screen",
        elapsed[0],
    )

    exiting = Refresher({"a": StubPage("A")}, {"a": 60}, clock=lambda: 0.0)
    exiting.pool.shutdown()
    exiting.run()  # returns instead of raising "cannot schedule new futures after shutdown"

    refresher = Refresher({}, {"a": 60, "b": 600}, clock=lambda: 100.0)
    refresher.next_run = {"a": 130.0, "b": 650.0}
    refresher.update({"a": 60, "b": 10, "c": 5})
    assert refresher.next_run == {"a": 130.0, "b": 110.0, "c": 100.0}, (
        refresher.next_run
    )
    assert refresher.data_every["b"] == 10

    still = StubPage("Impressora", live=True, key=("same reading",))
    quiet = FakeOutput(lambda: elapsed[0])
    elapsed[0] = 0.0
    Carousel(
        (Screen("I", 20, 30, 1, "lado", (layout.Part("i", still, "cheio", 1),)),),
        quiet,
        clock=lambda: elapsed[0],
        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        wall=lambda: datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc),
    ).run(screens_to_show=1)
    assert [m for m, kind in quiet.writes if kind == "live"] == [1.0], (
        "a block whose data did not change is drawn once, not every second",
        quiet.writes,
    )

    status = render_status(
        screens, 1, 0.5, datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    )
    assert (
        status.size == (WIDTH, STATUS_ROWS)
        and len(rgb565(status)) == WIDTH * STATUS_ROWS * 2
    )
    track_left, track_width, track_y = (
        MARGIN + 10,
        WIDTH - 2 * MARGIN - 20,
        STATUS_BAND[1] - 4,
    )
    assert status.getpixel((track_left + track_width // 4, track_y)) == ui.BLUE, (
        "50% fills the first half"
    )
    assert status.getpixel((track_left + 3 * track_width // 4, track_y)) != ui.BLUE, (
        "and not the second"
    )
    block = render_block(
        screens[1].parts[1], (950, 72, 1904, 1064), datetime.now(timezone.utc)
    )
    assert block.size == (954, 992)

    ticks = [0.0]
    calls = []
    slow, fast = StubPage("Lento"), StubPage("Rápido")
    slow.refresh = lambda now: calls.append("lento")
    fast.refresh = lambda now: calls.append("rapido")
    refresher = Refresher(
        {"lento": slow, "rapido": fast},
        {"lento": 60, "rapido": 1},
        clock=lambda: ticks[0],
    )
    assert sorted(refresher.due()) == ["lento", "rapido"]
    refresher.busy.discard("rapido")
    ticks[0] = 1.0
    assert refresher.due() == ["rapido"], (
        "the slow block is still busy, the fast one runs again"
    )
    refresher.busy.clear()
    ticks[0] = 30.0
    assert refresher.due() == ["rapido"], "the slow block waits for its 60 s"

    with_logs = []
    handler = logging.Handler()
    handler.emit = lambda record: with_logs.append(
        (record.levelname, record.getMessage())
    )
    log.addHandler(handler)
    level = log.level
    log.setLevel(logging.INFO)
    try:
        flaky = StubPage("Juros")
        flaky.problems = lambda: ["Tesouro: sem conexao"]
        refresher.report(flaky)
        refresher.report(flaky)
        flaky.problems = lambda: []
        refresher.report(flaky)
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    assert with_logs == [
        ("WARNING", "Juros: Tesouro: sem conexao"),
        ("INFO", "Juros: resolvido: Tesouro: sem conexao"),
    ]
    refresher.pool.shutdown()
    print("screen selftest ok")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "selftest":
        selftest(
            Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/dashboard-selftest")
        )
    elif command == "preview":
        preview(Path(sys.argv[2]))
    elif command == "run":
        run()
    else:
        sys.exit("usage: screen.py [run|preview DIR|selftest [DIR]]")

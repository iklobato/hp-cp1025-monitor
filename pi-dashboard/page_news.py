"""News page and the de-duplicated news cache it shares with the market page.

Layout: three columns of topic blocks, five stories each with a photo and a title of up to
two lines (an exception to the one-line rule, chosen by the user for this page): Brasil e
mundo + Regional, Mercado + Segurança, and a featured story with a large photo above
Tecnologia. Stories that mention a watched word are highlighted everywhere.
"""

import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

import fields
import sources
import summary
import ui
from PIL import Image, ImageDraw
from sources import Feed, SourceError, Story

WORLD_FEEDS = (
    Feed("G", "g1", "https://g1.globo.com/rss/g1/"),
    Feed("F", "Folha", "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml"),
)
REGIONAL_FEEDS = (
    Feed("R", "g1 São Paulo", "https://g1.globo.com/rss/g1/sp/sao-paulo/"),
)
MARKET_FEEDS = (
    Feed("I", "InfoMoney", "https://www.infomoney.com.br/feed/"),
    Feed("M", "Money Times", "https://www.moneytimes.com.br/feed/"),
    Feed("E", "g1 Economia", "https://g1.globo.com/rss/g1/economia/"),
    Feed("C", "CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
)
TECH_FEEDS = (
    Feed("V", "Verge", "https://www.theverge.com/rss/index.xml"),
    Feed("A", "Ars", "https://feeds.arstechnica.com/arstechnica/index"),
    Feed("T", "Tecnoblog", "https://tecnoblog.net/feed/"),
    Feed("O", "Olhar Digital", "https://olhardigital.com.br/feed/"),
    Feed("H", "Hacker News", "https://hnrss.org/frontpage"),
)
SECURITY_FEEDS = (
    Feed("B", "Bleeping", "https://www.bleepingcomputer.com/feed/"),
    Feed("N", "The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    Feed("K", "Krebs", "https://krebsonsecurity.com/feed/"),
)
# Order matters: a headline two feeds share is kept in the first topic that has it.
TOPICS = (
    ("market", MARKET_FEEDS),
    ("regional", REGIONAL_FEEDS),
    ("world", WORLD_FEEDS),
    ("tech", TECH_FEEDS),
    ("security", SECURITY_FEEDS),
)
TOPIC_NAMES = {
    "market": "Mercado",
    "regional": "Regional",
    "world": "Brasil e mundo",
    "tech": "Tecnologia",
    "security": "Segurança",
}
# (column, title, topic): the featured story sits on top of the third column.
COLUMNS = (
    (("Brasil e mundo", "world"), ("Regional", "regional")),
    (("Mercado", "market"), ("Segurança", "security")),
    (("Destaque", None), ("Tecnologia", "tech")),
)
# None by default: each installation sets its own in carrossel.toml ([blocos.noticias]).
WATCH_WORDS: tuple[str, ...] = ()
STORIES_PER_FEED = 25
STORIES_SHOWN = 3  # each card also holds a summary now (2 if they stop fitting)
SUMMARY_CHARS = (
    180  # what the model is asked for; the card check below decides what is shown
)
THUMB_SIZE = (120, 68)
FEATURE_SIZE = (436, 245)
NEWS_MAX_AGE = timedelta(minutes=10)
FEATURE_WINDOW = timedelta(hours=24)
FETCH_WORKERS = 8


def gather_news(feeds: tuple[Feed, ...]) -> tuple[list[Story], list[str]]:
    def outcome(feed: Feed) -> list[Story] | SourceError:
        try:
            return sources.fetch_stories(feed, STORIES_PER_FEED)
        except SourceError as err:
            return err

    with ThreadPoolExecutor(FETCH_WORKERS) as pool:
        results = list(pool.map(outcome, feeds))
    stories = [
        story for result in results if isinstance(result, list) for story in result
    ]
    problems = [
        f"{feed.label}: {result}"
        for feed, result in zip(feeds, results)
        if isinstance(result, SourceError)
    ]
    return stories, problems


def newest(stories: list[Story], count: int = STORIES_SHOWN) -> list[Story]:
    return sorted(stories, key=lambda story: story.published, reverse=True)[:count]


def featured_story(
    stories: dict[str, list[Story]], now: datetime, watch_words: tuple[str, ...]
) -> Story | None:
    """Newest recent story with a watched word; otherwise the newest Brazil/world or regional one."""
    recent = [
        s
        for topic in stories.values()
        for s in topic
        if now - s.published <= FEATURE_WINDOW
    ]
    watched = [story for story in recent if story.mentions(watch_words)]
    pool = watched or [
        s for s in recent if s in stories["world"] or s in stories["regional"]
    ]
    return max(pool, key=lambda story: story.published, default=None)


class NewsCache:
    """Feeds refetched every NEWS_MAX_AGE (a topic whose feeds all failed keeps its last good
    stories), plus the photos of the stories the news page shows. Shared by two blocks, so a
    refresh already running in another thread is not started twice."""

    def __init__(
        self,
        gather: Callable = gather_news,
        picture: Callable = sources.thumbnail,
        page_image: Callable[[str], str] | None = None,
        summaries: summary.Summaries | None = None,
    ) -> None:
        self.gather, self.picture = gather, picture
        self.page_image = page_image or (
            lambda link: sources.og_image(
                sources.fetch_bytes(link).decode("utf-8", errors="replace")
            )
        )
        self.feeds: dict[str, tuple[Feed, ...]] = dict(TOPICS)
        self.watch_words: tuple[str, ...] = WATCH_WORDS
        self.stories: dict[str, list[Story]] = {topic: [] for topic, _ in TOPICS}
        self.pictures: dict[tuple[str, tuple], Image.Image] = {}
        self.page_images: dict[
            str, str
        ] = {}  # story link -> og:image, so each page is read once
        self.problems: list[str] = []
        self.summaries = summaries or summary.Summaries()
        # Set by NewsPage.layout from the real card size; until then every summary "fits".
        self.fits: Callable[[Story, str], bool] = lambda story, text: True
        self.fetched_at: datetime | None = None
        self.lock = threading.Lock()

    def refresh(self, now: datetime) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.fetched_at is not None and now - self.fetched_at < NEWS_MAX_AGE:
                return
            topics = list(
                self.feeds.items()
            )  # one snapshot: settings may change meanwhile
            fetched, problems = {}, []
            for topic, feeds in topics:
                fetched[topic], topic_problems = self.gather(feeds)
                problems += topic_problems
            unique = sources.unique_stories([fetched[topic] for topic, _ in topics])
            stories = {
                topic: found or self.stories.get(topic, [])
                for (topic, _), found in zip(topics, unique)
            }
            self.pictures = self._load_pictures(stories, now)
            shown = [story for topic in stories.values() for story in newest(topic)]
            feature = featured_story(stories, now, self.watch_words)
            if feature is not None and feature not in shown:
                shown.append(feature)
            problems += self.summaries.fill(shown, SUMMARY_CHARS, self.fits, now)
            self.stories, self.problems, self.fetched_at = stories, problems, now
            sources.prune_thumbnails(now)
        finally:
            self.lock.release()

    def configure(
        self, feeds: dict[str, tuple[Feed, ...]], watch_words: tuple[str, ...]
    ) -> None:
        """New sources or watched words from the settings page; changed sources refetch now."""
        if feeds != self.feeds:
            self.feeds, self.fetched_at = feeds, None
        self.watch_words = watch_words

    def _image_url(self, story: Story) -> str:
        if story.image or not story.link:
            return story.image
        if story.link not in self.page_images:
            try:
                self.page_images[story.link] = self.page_image(story.link)
            except SourceError:
                self.page_images[story.link] = ""
        return self.page_images[story.link]

    def _load_pictures(self, stories: dict[str, list[Story]], now: datetime) -> dict:
        wanted = [
            (story, THUMB_SIZE) for topic in stories.values() for story in newest(topic)
        ]
        feature = featured_story(stories, now, self.watch_words)
        if feature is not None:
            wanted.append((feature, FEATURE_SIZE))

        def load(job):
            story, size = job
            url = self._image_url(story)
            if not url:
                return None
            try:
                return (picture_key(story, size), self.picture(url, size))
            except SourceError:
                return None  # a story without a usable picture is drawn without one

        with ThreadPoolExecutor(FETCH_WORKERS) as pool:
            return dict(found for found in pool.map(load, wanted) if found)


def picture_key(story: Story, size: tuple) -> tuple[str, tuple]:
    return (story.link or story.title, size)


def story_meta(story: Story, now: datetime) -> str:
    return f"{story.feed.label} · {sources.clock(story.published, now)} · {sources.short_age(now - story.published)}"


@dataclass(frozen=True)
class Part:
    text: str
    face: object
    step: int  # line height
    color: tuple
    badge: str = ""  # a feed letter drawn before the first line
    gap: int = 0  # space before this part


@dataclass(frozen=True)
class Line:
    x: float
    baseline: float
    text: str
    part: Part
    first: bool  # the part's first line (where its badge goes)


def flow(parts: list[Part], box: tuple, photo: tuple[int, int]) -> list[Line] | None:
    """Word-wrap the parts in order. Lines beside the photo use what is left of the width,
    lines below it the whole width. None when the text does not fit the box: callers drop a
    part instead of cutting text."""
    left, top, right, bottom = box
    photo_bottom = top + photo[1]
    lines, y = [], top
    for part in parts:
        y += part.gap
        words, first = part.text.split(), True
        while words:
            x = (
                left
                + (photo[0] + 12 if y < photo_bottom else 0)
                + (24 if part.badge and first else 0)
            )
            line, taken = words[0], 1
            if part.face.getlength(line) > right - x or y + part.step > bottom:
                return None
            while (
                taken < len(words)
                and part.face.getlength(f"{line} {words[taken]}") <= right - x
            ):
                line, taken = f"{line} {words[taken]}", taken + 1
            lines.append(Line(x, y + part.step - 5, line, part, first))
            words, y, first = words[taken:], y + part.step, False
    return lines


def card_parts(story: Story, text: str, now: datetime, watched: bool) -> list[Part]:
    title_face = (
        ui.font(17, bold=True, condensed=True)
        if watched
        else ui.font(17, condensed=True)
    )
    parts = [
        Part(story.title, title_face, 21, ui.AMBER if watched else ui.TEXT),
        Part(
            story_meta(story, now),
            ui.font(14, condensed=True),
            20,
            ui.DIM,
            badge=story.feed.letter,
        ),
    ]
    if text:
        parts.append(Part(text, ui.font(15, condensed=True), 19, SUMMARY_COLOR, gap=4))
    return parts


SUMMARY_COLOR = ui.blend(ui.TEXT, ui.CARD, 0.8)


def card_fits(story: Story, text: str, size: tuple[int, int]) -> bool:
    """Title, source line and summary all fit the card uncut."""
    box = (0, 0, size[0], size[1])
    return (
        flow(
            card_parts(story, text, datetime.now(timezone.utc), False), box, THUMB_SIZE
        )
        is not None
    )


def draw_story(
    image: Image.Image,
    draw,
    box: tuple,
    story: Story,
    picture,
    color: tuple,
    now: datetime,
    watch_words: tuple[str, ...],
    summary_text: str = "",
) -> None:
    """One card: photo on the left, then title, source and summary flowing around it, uncut."""
    left, top, right, bottom = box
    watched = story.mentions(watch_words)
    if watched:
        draw.rounded_rectangle(
            (left - 6, top - 4, right + 6, bottom - 2),
            radius=8,
            fill=ui.blend(ui.AMBER, ui.CARD, 0.16),
        )
    if picture is not None:
        image.paste(picture, (int(left), int(top)))
    else:
        draw.rounded_rectangle(
            (left, top, left + THUMB_SIZE[0], top + THUMB_SIZE[1]),
            radius=6,
            fill=ui.GRID,
        )
        draw.text(
            (left + THUMB_SIZE[0] / 2, top + THUMB_SIZE[1] / 2),
            story.feed.letter,
            font=ui.font(28, bold=True),
            fill=color,
            anchor="mm",
        )
    for text in (
        summary_text,
        "",
    ):  # with the summary when it fits, else title and source only
        lines = flow(card_parts(story, text, now, watched), box, THUMB_SIZE)
        if lines is not None:
            break
    if lines is None:  # not even the title fits: a layout bug, never cut silently
        draw.text(
            (left + THUMB_SIZE[0] + 12, top + 17),
            "texto não coube",
            font=ui.font(14),
            fill=ui.RED,
            anchor="ls",
        )
        return
    for line in lines:
        if line.part.badge and line.first:
            ui.badge(draw, line.x - 24, line.baseline - 5, line.part.badge, color)
        draw.text(
            (line.x, line.baseline),
            line.text,
            font=line.part.face,
            fill=line.part.color,
            anchor="ls",
        )


def draw_block(
    image: Image.Image,
    draw,
    box: tuple,
    title: str,
    topic: str,
    stories: list[Story],
    pictures: dict,
    now: datetime,
    news: NewsCache,
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box, title)
    colors = ui.feed_colors(news.feeds.get(topic, ()))
    shown = newest(stories)
    if not shown:
        draw.text(
            (left + 16, top + 80),
            "carregando…",
            font=ui.font(18),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    row = (bottom - top - 46) / STORIES_SHOWN
    for index, story in enumerate(shown):
        row_top = top + 46 + index * row
        draw_story(
            image,
            draw,
            (left + 14, row_top, right - 14, row_top + row),
            story,
            pictures.get(picture_key(story, THUMB_SIZE)),
            colors.get(story.feed.letter, ui.DIM),
            now,
            news.watch_words,
            news.summaries.text(story),
        )


def draw_feature(
    image: Image.Image,
    draw,
    box: tuple,
    story: Story | None,
    pictures: dict,
    now: datetime,
    watch_words: tuple[str, ...],
    summary_text: str = "",
) -> None:
    left, top, right, bottom = box
    ui.card(draw, box)
    if story is None:
        draw.text(
            (left + 16, top + 60),
            "sem notícias recentes",
            font=ui.font(20),
            fill=ui.DIM,
            anchor="ls",
        )
        return
    watched = story.mentions(watch_words)
    draw.text(
        (left + 16, top + 30),
        "VOCÊ ACOMPANHA" if watched else "DESTAQUE",
        font=ui.font(15, bold=True),
        fill=ui.AMBER,
        anchor="ls",
    )
    draw.text(
        (right - 16, top + 30),
        story_meta(story, now),
        font=ui.font(14),
        fill=ui.DIM,
        anchor="rs",
    )
    picture = pictures.get(picture_key(story, FEATURE_SIZE))
    photo_top = top + 44
    photo_height = 0
    if picture is not None:
        width = min(FEATURE_SIZE[0], right - left - 32)
        height = min(FEATURE_SIZE[1], bottom - top - 160)
        crop = picture.crop((0, 0, width, height))
        image.paste(crop, (int(left + 16), int(photo_top)))
        photo_height = height + 12
    area = (left + 16, photo_top + photo_height + 4, right - 16, bottom - 8)
    title = Part(
        story.title,
        ui.font(21, bold=True, condensed=True),
        26,
        ui.AMBER if watched else ui.TEXT,
    )
    lines = None
    for parts in (
        [title, Part(summary_text, ui.font(15), 20, SUMMARY_COLOR, gap=6)],
        [title],
    ):
        if parts[-1].text:
            lines = flow(parts, area, (0, 0))
            if lines is not None:
                break
    for line in lines or ():
        draw.text(
            (line.x, line.baseline),
            line.text,
            font=line.part.face,
            fill=line.part.color,
            anchor="ls",
        )


class NewsPage(ui.Page):
    """Owns the news settings; the market page reads the same NewsCache."""

    name = "Notícias"
    settings_fields = (
        fields.Field(
            "palavras_vigiadas",
            "Palavras vigiadas",
            "palavras",
            list(WATCH_WORDS),
            low=0,
            high=20,
            help="Notícia com uma destas palavras fica em amarelo e vira o destaque.",
        ),
        fields.Field(
            "fontes",
            "Fontes de notícias (RSS)",
            "tabela",
            [
                {
                    "assunto": topic,
                    "sigla": feed.letter,
                    "nome": feed.label,
                    "url": feed.url,
                }
                for topic, feeds in TOPICS
                for feed in feeds
            ],
            low=1,
            high=30,
            unique="url",
            help="Cada fonte aparece no bloco do seu assunto; a letra é o selo colorido ao lado da notícia.",
            columns=(
                fields.Column(
                    "assunto", "Assunto", "escolha", tuple(TOPIC_NAMES.items())
                ),
                fields.Column("sigla", "Letra", "sigla"),
                fields.Column("nome", "Nome"),
                fields.Column("url", "Endereço do RSS", "url"),
            ),
        ),
    )

    def __init__(self, news: NewsCache) -> None:
        self.news = news

    def apply_settings(self, values: dict) -> None:
        feeds = {
            topic: tuple(
                Feed(row["sigla"], row["nome"], row["url"])
                for row in values["fontes"]
                if row["assunto"] == topic
            )
            for topic, _ in TOPICS
        }
        self.news.configure(feeds, tuple(values["palavras_vigiadas"]))

    def refresh(self, now: datetime) -> None:
        self.news.refresh(now)

    def problems(self) -> list[str]:
        return self.news.problems

    def layout(self, box: tuple, mode: str) -> None:
        """The card size decides which summaries fit (see compose for the same arithmetic)."""
        left, top, right, bottom = box
        width = (right - left - 2 * ui.GUTTER) / 3
        block_height = (bottom - top - ui.GUTTER) / 2
        card = (int(width - 28), int((block_height - 46) / STORIES_SHOWN))
        self.news.fits = lambda story, text: card_fits(story, text, card)

    def compose(
        self, image: Image.Image, box: tuple, now: datetime, mode: str = "cheio"
    ) -> None:
        draw = ImageDraw.Draw(image)
        left, top, right, bottom = box
        stories, pictures = self.news.stories, self.news.pictures
        width = (right - left - 2 * ui.GUTTER) / 3
        middle = top + (bottom - top - ui.GUTTER) / 2
        for column, blocks in enumerate(COLUMNS):
            column_left = left + column * (width + ui.GUTTER)
            for row, (title, topic) in enumerate(blocks):
                block = (
                    column_left,
                    top if row == 0 else middle + ui.GUTTER,
                    column_left + width,
                    middle if row == 0 else bottom,
                )
                if topic is None:
                    feature = featured_story(stories, now, self.news.watch_words)
                    draw_feature(
                        image,
                        draw,
                        block,
                        feature,
                        pictures,
                        now,
                        self.news.watch_words,
                        self.news.summaries.text(feature) if feature else "",
                    )
                else:
                    draw_block(
                        image,
                        draw,
                        block,
                        title,
                        topic,
                        stories.get(topic, []),
                        pictures,
                        now,
                        self.news,
                    )


def selftest() -> Image.Image:
    now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    same = "Preço dos alimentos: o que ficou mais caro"

    def fake_gather(feeds):
        stories = [
            Story(
                now - timedelta(minutes=i + 5),
                f"{feed.label} notícia número {i} com um título bem comprido "
                "que precisa de duas linhas para caber",
                feed,
                "Resumo da notícia " * 12,
                f"https://site/{feed.letter}{i}",
                f"https://img/{feed.letter}{i}.jpg" if i % 2 else "",
            )
            for i, feed in enumerate(feeds * 12)
        ]
        stories.append(Story(now - timedelta(minutes=1), same, feeds[0]))
        if feeds is REGIONAL_FEEDS:
            stories.append(
                Story(
                    now - timedelta(hours=3),
                    "Chuva forte em Sorocaba derruba árvores",
                    feeds[0],
                    link="https://site/sorocaba",
                    image="https://img/sorocaba.jpg",
                )
            )
        return stories, (
            [f"{feeds[0].label}: sem conexao"] if feeds is SECURITY_FEEDS else []
        )

    requested = []

    def fake_picture(url, size):
        requested.append((url, size))
        if url.endswith("0.jpg"):
            raise SourceError("imagem ilegível")
        return Image.new("RGB", size, (90, 120, 160))

    pages_read = []

    def fake_page_image(link):
        pages_read.append(link)
        return f"{link}/og.jpg"

    cache = NewsCache(
        fake_gather,
        fake_picture,
        fake_page_image,
        summary.Summaries("", Path(tempfile.mkdtemp()) / "s.json"),
    )
    cache.configure(dict(TOPICS), ("Sorocaba",))
    cache.refresh(now)
    assert (
        sum(story.title == same for topic in cache.stories.values() for story in topic)
        == 1
    )
    assert any(story.title == same for story in cache.stories["market"]), (
        "market keeps the shared headline"
    )
    assert cache.problems == ["Bleeping: sem conexao"]
    feature = featured_story(cache.stories, now, cache.watch_words)
    assert feature is not None and feature.mentions(cache.watch_words), (
        "a watched story wins over newer ones"
    )
    settings_cache = NewsCache(
        summaries=summary.Summaries("", Path(tempfile.mkdtemp()) / "s.json")
    )  # settings only
    settings_cache.fetched_at = fetched_at = now
    page = NewsPage(settings_cache)
    page.apply_settings(
        fields.read_all(NewsPage.settings_fields, {}, "blocos.noticias")
    )
    assert (
        settings_cache.feeds == dict(TOPICS) and settings_cache.fetched_at == fetched_at
    ), "the defaults are today's sources"
    page.apply_settings(
        fields.read_all(
            NewsPage.settings_fields,
            {
                "palavras_vigiadas": ["Copom"],
                "fontes": [
                    {
                        "assunto": "tech",
                        "sigla": "V",
                        "nome": "Verge",
                        "url": "https://v/rss",
                    }
                ],
            },
            "blocos.noticias",
        )
    )
    assert (
        settings_cache.watch_words == ("Copom",) and settings_cache.fetched_at is None
    ), "new sources are read now"
    assert (
        settings_cache.feeds["tech"] == (Feed("V", "Verge", "https://v/rss"),)
        and settings_cache.feeds["world"] == ()
    )
    assert (
        feature.image,
        FEATURE_SIZE,
    ) in requested, "the featured story gets the large picture"
    assert all(size in (THUMB_SIZE, FEATURE_SIZE) for _, size in requested)
    assert pages_read and all(
        url.endswith("/og.jpg") for url, _ in requested if "site/" in url
    ), "stories without a feed picture use the page's og:image"
    shown_links = {
        story.link
        for topic in cache.stories.values()
        for story in newest(topic)
        if story.link
    }
    assert set(pages_read) <= shown_links, "only the stories on screen cost a page read"
    before_pages = len(pages_read)
    cache.fetched_at = None
    cache.refresh(now + timedelta(minutes=1))
    assert len(pages_read) == before_pages, "each page is read for its picture once"
    face = ui.font(17, condensed=True)
    long_title = Part("palavra " * 30, face, 21, ui.TEXT)
    lines = flow([long_title], (0, 0, 433, 400), THUMB_SIZE)
    assert lines and lines[0].x == THUMB_SIZE[0] + 12 and lines[-1].x == 0, (
        "beside the photo, then full width"
    )
    assert all(face.getlength(line.text) <= 433 - line.x for line in lines), (
        "no line wider than its room"
    )
    assert " ".join(line.text for line in lines) == long_title.text.strip(), (
        "every word shown, none cut"
    )
    assert flow([long_title], (0, 0, 433, 60), THUMB_SIZE) is None, (
        "too long for the box: refused, not cut"
    )
    meta = Part("g1 · 10:00 · 5m", ui.font(14), 20, ui.DIM, badge="G")
    assert flow([meta], (0, 0, 433, 100), THUMB_SIZE)[0].x == THUMB_SIZE[0] + 12 + 24, (
        "room for the badge"
    )
    story = Story(now, "Título curto", Feed("G", "g1", "u"))
    assert card_fits(story, "Resumo de duas frases curtas. Cabe.", (433, 148))
    assert not card_fits(story, "resumo " * 120, (433, 148)), (
        "a summary that would be cut is not shown"
    )
    page = NewsPage(cache)
    page.layout((0, 0, 1407, 992), "cheio")
    assert cache.fits(story, "Cabe.") and not cache.fits(story, "resumo " * 120)
    assert cache.lock.acquire(blocking=False)
    cache.fetched_at = None
    cache.refresh(now + timedelta(hours=1))
    assert cache.fetched_at is None, (
        "a refresh already running in another thread is not repeated"
    )
    cache.lock.release()

    image = Image.new("RGB", (1888, 992), ui.BACKGROUND)
    NewsPage(cache).compose(image, (0, 0, 1416, 992), now)
    print("page_news selftest ok")
    return image

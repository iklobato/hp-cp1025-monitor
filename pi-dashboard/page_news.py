"""News page and the de-duplicated news cache it shares with the market page.

Layout: three columns of topic blocks, five stories each with a photo and a title of up to
two lines (an exception to the one-line rule, chosen by the user for this page): Brasil e
mundo + Regional, Mercado + Segurança, and a featured story with a large photo above
Tecnologia. Stories that mention a watched word are highlighted everywhere.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

import fields
import sources
import ui
from PIL import Image, ImageDraw
from sources import Feed, SourceError, Story

WORLD_FEEDS = (
    Feed("G", "g1", "https://g1.globo.com/rss/g1/"),
    Feed("F", "Folha", "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml"),
)
REGIONAL_FEEDS = (Feed("R", "g1 São Paulo", "https://g1.globo.com/rss/g1/sao-paulo/"),)
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
STORIES_SHOWN = 5
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


def wrap_to_width(text: str, face, width: float, max_lines: int) -> list[str]:
    """Word-wrap by measured pixels; the last allowed line ends in an ellipsis when text remains."""
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if face.getlength(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    else:
        if current:
            lines.append(current)
        return lines[:max_lines]
    lines[-1] = (
        ui.fit(lines[-1] + " " + current, face, width) if lines[-1] else lines[-1]
    )
    if not lines[-1].endswith("…"):
        lines[-1] = ui.fit(lines[-1] + " …", face, width)
    return lines


def story_meta(story: Story, now: datetime) -> str:
    return f"{story.feed.label} · {sources.clock(story.published, now)} · {sources.short_age(now - story.published)}"


def draw_story(
    image: Image.Image,
    draw,
    box: tuple,
    story: Story,
    picture,
    color: tuple,
    now: datetime,
    watch_words: tuple[str, ...],
) -> None:
    """One row: photo on the left, title in up to two lines, then the source and time."""
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
    text_left = left + THUMB_SIZE[0] + 12
    face = (
        ui.font(17, bold=True, condensed=True)
        if watched
        else ui.font(17, condensed=True)
    )
    for index, line in enumerate(
        wrap_to_width(story.title, face, right - text_left, max_lines=2)
    ):
        draw.text(
            (text_left, top + 17 + index * 21),
            line,
            font=face,
            fill=ui.AMBER if watched else ui.TEXT,
            anchor="ls",
        )
    ui.badge(draw, text_left, top + 63, story.feed.letter, color)
    draw.text(
        (text_left + 24, top + 68),
        story_meta(story, now),
        font=ui.font(14, condensed=True),
        fill=ui.DIM,
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
        )


def draw_feature(
    image: Image.Image,
    draw,
    box: tuple,
    story: Story | None,
    pictures: dict,
    now: datetime,
    watch_words: tuple[str, ...],
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
    face = ui.font(21, bold=True, condensed=True)
    y = photo_top + photo_height + 22
    for line in wrap_to_width(story.title, face, right - left - 32, max_lines=2):
        draw.text(
            (left + 16, y),
            line,
            font=face,
            fill=ui.AMBER if watched else ui.TEXT,
            anchor="ls",
        )
        y += 26
    for line in wrap_to_width(
        story.summary,
        ui.font(15),
        right - left - 32,
        max_lines=max(int((bottom - y) // 20), 0),
    ):
        draw.text((left + 16, y + 4), line, font=ui.font(15), fill=ui.DIM, anchor="ls")
        y += 20


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

    cache = NewsCache(fake_gather, fake_picture, fake_page_image)
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
    settings_cache = NewsCache()  # never refreshed: only its settings are under test
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
    face = ui.font(18)
    wrapped = wrap_to_width("palavra " * 200, face, 300, max_lines=2)
    assert (
        len(wrapped) == 2
        and wrapped[-1].endswith("…")
        and all(face.getlength(line) <= 300 for line in wrapped)
    )
    assert (
        wrap_to_width("curto", face, 300, max_lines=2) == ["curto"]
        and wrap_to_width("", face, 300, 2) == []
    )
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

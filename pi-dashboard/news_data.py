"""Data for the Pi dashboard: news (RSS/Atom), with their photos and thumbnails."""

import gzip
import hashlib
import io
import html
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps, UnidentifiedImageError

from net import CACHE_DIR, SourceError, clock, fetch_bytes


@dataclass(frozen=True)
class Feed:
    """One news source: a letter for its badge, a label, a URL."""

    letter: str
    label: str
    url: str


@dataclass(frozen=True)
class Story:
    published: datetime
    title: str
    feed: Feed
    summary: str = ""
    link: str = ""
    image: str = ""  # picture URL the feed itself carries; empty when it has none

    def mentions(self, words: tuple[str, ...]) -> bool:
        """Whole-word match, ignoring case and accents, in the title or the summary."""
        text = f" {title_key(self.title)} {title_key(self.summary)} "
        return any(f" {title_key(word)} " in text for word in words)


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, *names: str) -> str:
    for child in element:
        if _local_name(child) in names and child.text:
            return child.text.strip()
    return ""


def parse_published(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        published = parsedate_to_datetime(stamp)
    except (TypeError, ValueError):
        pass
    else:
        # RFC 2822 "-0000" (used by g1) parses as naive but means UTC.
        return published if published.tzinfo else published.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


TAG = re.compile(r"<[^>]+>")


def plain_text(fragment: str) -> str:
    """Feed summaries carry HTML (images, paragraphs); keep the words only."""
    return " ".join(html.unescape(TAG.sub(" ", html.unescape(fragment))).split())


IMG_SRC = re.compile(r"""<img[^>]+src=["']([^"']+)""")


def story_link(element: ET.Element) -> str:
    """RSS <link>text</link>, or Atom <link href=... rel="alternate">."""
    for child in element:
        if _local_name(child) != "link":
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        if child.get("href") and child.get("rel", "alternate") == "alternate":
            return child.get("href")
    return ""


def story_image(element: ET.Element) -> str:
    """media:content / media:thumbnail / an image enclosure, else the first <img> in the body."""
    for child in element:
        name = _local_name(child)
        if (
            name in ("content", "thumbnail")
            and child.get("url")
            and "image" in child.get("medium", "image")
        ):
            return child.get("url")
        if (
            name == "enclosure"
            and child.get("url")
            and child.get("type", "image/").startswith("image/")
        ):
            return child.get("url")
    for child in element:
        if (
            _local_name(child) in ("description", "encoded", "summary", "content")
            and child.text
        ):
            match = IMG_SRC.search(html.unescape(child.text))
            if match:
                return match.group(1)
    return ""


OG_IMAGE = (
    re.compile(
        r"""<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)["'][^>]+content=["']([^"']+)"""
    ),
    re.compile(
        r"""<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["'](?:og:image|twitter:image)["']"""
    ),
)


def og_image(page: str) -> str:
    """The picture a site declares for sharing (og:image or twitter:image), if any."""
    for pattern in OG_IMAGE:
        match = pattern.search(page)
        if match and match.group(1).startswith(("http://", "https://")):
            return html.unescape(match.group(1))
    return ""


THUMB_DIR = CACHE_DIR / "thumbs"
THUMB_KEEP = timedelta(days=3)


def thumbnail(
    url: str,
    size: tuple[int, int],
    fetch: Callable[[str], bytes] = fetch_bytes,
    folder: Path = THUMB_DIR,
):
    """The picture cut to fill `size` (center crop), cached on disk by URL so each loads once."""
    path = folder / f"{hashlib.sha1(f'{url}|{size}'.encode()).hexdigest()}.jpg"
    if path.exists():
        try:
            return Image.open(path).convert("RGB")
        except (UnidentifiedImageError, OSError):
            path.unlink(missing_ok=True)  # cut short by a power loss: fetch it again
    try:
        picture = Image.open(io.BytesIO(fetch(url))).convert("RGB")
    except (UnidentifiedImageError, OSError) as err:
        raise SourceError("imagem ilegível") from err
    picture = ImageOps.fit(picture, size, Image.Resampling.LANCZOS)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb", dir=folder, suffix=".tmp", delete=False
    ) as handle:
        picture.save(handle, "JPEG", quality=85)
    Path(handle.name).replace(path)
    return picture


def prune_thumbnails(now: datetime, folder: Path = THUMB_DIR) -> int:
    """Delete cached pictures older than THUMB_KEEP; returns how many went."""
    if not folder.exists():
        return 0
    cutoff = (now - THUMB_KEEP).timestamp()
    old = [path for path in folder.glob("*.jpg") if path.stat().st_mtime < cutoff]
    for path in old:
        path.unlink(missing_ok=True)
    return len(old)


def stories_from_feed(xml: bytes, feed: Feed, limit: int) -> list[Story]:
    """RSS <item> and Atom <entry> both carry a title and a publish time."""
    stories = []
    for element in ET.fromstring(xml).iter():
        if _local_name(element) not in ("item", "entry"):
            continue
        published = parse_published(
            _child_text(element, "pubDate", "published", "updated")
        )
        title = " ".join(html.unescape(_child_text(element, "title")).split())
        if title and published:
            stories.append(
                Story(
                    published,
                    title,
                    feed,
                    plain_text(_child_text(element, "description", "summary")),
                    story_link(element),
                    story_image(element),
                )
            )
    stories.sort(key=lambda story: story.published, reverse=True)
    return stories[:limit]


def fetch_stories(feed: Feed, limit: int) -> list[Story]:
    try:
        return stories_from_feed(fetch_bytes(feed.url), feed, limit)
    except (ET.ParseError, EOFError, gzip.BadGzipFile) as err:
        raise SourceError("feed invalido") from err


def title_key(title: str) -> str:
    """Accents, case and punctuation removed: g1 and g1 Economia publish the same headline."""
    plain = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    )
    return " ".join("".join(c if c.isalnum() else " " for c in plain).split())


def unique_stories(groups: list[list[Story]]) -> list[list[Story]]:
    """Each headline once, in the first group (in the given order) that has it."""
    seen: set[str] = set()
    result = []
    for group in groups:
        kept = []
        for story in sorted(group, key=lambda s: s.published, reverse=True):
            key = title_key(story.title)
            if key not in seen:
                seen.add(key)
                kept.append(story)
        result.append(kept)
    return result


def selftest() -> None:
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    feed = Feed("S", "AWS", "u")
    rss = b"""<rss><channel><title>x</title>
<item><title>Old  one</title><pubDate>Thu, 22 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>New one</title><pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>No date</title></item>
<item><title>Meta&amp;#8217;s AI</title><pubDate>Fri, 23 Oct 2026 11:30:00 -0000</pubDate></item>
</channel></rss>"""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>y</title>
<entry><title>Atom story</title><updated>2026-10-23T11:00:00Z</updated></entry></feed>"""
    stories = stories_from_feed(rss, feed, 5) + stories_from_feed(
        atom, Feed("V", "Verge", "u"), 5
    )
    assert [s.title for s in stories] == [
        "Meta’s AI",
        "New one",
        "Old one",
        "Atom story",
    ]
    assert stories[0].published == now - timedelta(minutes=30), "-0000 must mean UTC"
    assert len(stories_from_feed(rss, feed, 1)) == 1
    summarized = stories_from_feed(
        b"""<rss><channel><item><title>PagBank contrata</title>
<description>&lt;img src="x.jpg"&gt;&lt;p&gt;Vagas em Sorocaba &amp;amp; Campinas&lt;/p&gt;</description>
<pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>""",
        feed,
        5,
    )[0]
    assert summarized.summary == "Vagas em Sorocaba & Campinas", summarized.summary
    assert summarized.image == "x.jpg", "image from the <img> in the description"
    media = stories_from_feed(
        b"""<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item><title>A</title>
<link>https://site/a</link><media:content url="https://site/a.jpg" medium="image"/>
<pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>""",
        feed,
        5,
    )[0]
    assert media.link == "https://site/a" and media.image == "https://site/a.jpg"
    atom_story = stories_from_feed(
        b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title>
<link rel="alternate" href="https://site/b"/><updated>2026-10-23T11:00:00Z</updated></entry></feed>""",
        feed,
        5,
    )[0]
    assert atom_story.link == "https://site/b" and atom_story.image == ""
    assert (
        og_image('<meta property="og:image" content="https://s/i.jpg?a=1&amp;b=2">')
        == "https://s/i.jpg?a=1&b=2"
    )
    assert (
        og_image('<meta content="https://s/j.png" name="twitter:image">')
        == "https://s/j.png"
    )
    assert og_image("<html></html>") == ""
    assert og_image('<meta property="og:image" content="None">') == "", (
        "g1 writes the word None"
    )
    with tempfile.TemporaryDirectory() as tmp:
        png = io.BytesIO()
        Image.new("RGB", (400, 100), (200, 10, 10)).save(png, "PNG")
        fetched = []

        def fake(url):
            fetched.append(url)
            return png.getvalue() if url.endswith(".png") else b"not a picture"

        first = thumbnail("https://s/p.png", (120, 68), fake, Path(tmp))
        again = thumbnail("https://s/p.png", (120, 68), fake, Path(tmp))
        assert first.size == again.size == (120, 68) and fetched == [
            "https://s/p.png"
        ], "second read from disk"
        try:
            thumbnail("https://s/broken", (120, 68), fake, Path(tmp))
        except SourceError as err:
            assert str(err) == "imagem ilegível"
        else:
            raise AssertionError("a non-image must fail")
        (cut_short,) = Path(tmp).glob("*.jpg")
        cut_short.write_bytes(cut_short.read_bytes()[:200])
        fetched.clear()
        assert thumbnail("https://s/p.png", (120, 68), fake, Path(tmp)).size == (
            120,
            68,
        ) and fetched == ["https://s/p.png"], (
            "a picture cut short on disk (power loss) is fetched again, not raised"
        )
        assert not list(Path(tmp).glob("*.tmp")), "no temporary file is left behind"
        later = datetime.now(timezone.utc) + THUMB_KEEP + timedelta(hours=1)
        assert prune_thumbnails(later, Path(tmp)) == 1 and not list(
            Path(tmp).glob("*.jpg")
        )
    assert summarized.mentions(("sorocaba",)) and summarized.mentions(("Pagbank",))
    assert not summarized.mentions(("Itati",)), "whole words only"
    assert clock(stories[2].published, now).startswith("22/10")

    g1 = Story(now, "Preço dos alimentos: o que ficou mais caro", Feed("G", "g1", "u"))
    economia = Story(
        now,
        "Preco dos alimentos - o que ficou MAIS caro",
        Feed("E", "g1 Economia", "u"),
    )
    other = Story(now, "Outra notícia", Feed("G", "g1", "u"))
    market, general = unique_stories([[economia], [g1, other]])
    assert market == [economia] and general == [other]
    print("news_data selftest ok")


if __name__ == "__main__":
    selftest()

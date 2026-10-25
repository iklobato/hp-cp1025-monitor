"""Data for the Pi dashboard: the shared HTTP fetch (with retries), the download cache and the
text helpers (ages, clock, Brazilian numbers).
"""

import gzip
import hashlib
import io
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "Mozilla/5.0 (pi-dashboard)"
GZIP_MAGIC = b"\x1f\x8b"
CACHE_DIR = Path.home() / ".cache/pi-dashboard"
# Big files kept on disk so a restart does not download them again: every start fetched the
# 14.6 MB Tesouro file, and the first try after a restart sometimes failed to connect.
DOWNLOAD_DIR = CACHE_DIR / "downloads"
DOWNLOAD_PRUNE = timedelta(days=30)


class SourceError(Exception):
    """A feed or quote could not be read; the screen shows it instead of crashing."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# Every fetch here is a read (GET), so a second try is safe; only failures that can pass are retried.
FETCH_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 3


def decoded(body: bytes) -> bytes:
    """g1 sometimes answers gzip-compressed without Content-Encoding."""
    if not body.startswith(GZIP_MAGIC):
        return body
    try:
        return gzip.decompress(body)
    except (OSError, EOFError, zlib.error) as err:
        raise SourceError("resposta comprimida corrompida", retryable=True) from err


def web_address(url: str) -> str:
    """Feeds hand out picture addresses with raw accents and spaces; http.client sends ASCII
    only and raised UnicodeEncodeError, which aborted the whole news refresh (2026-10-24).
    """
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def fetch_once(url: str, timeout: float, opener=urllib.request.urlopen) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise SourceError(
            f"endereço inválido: {url[:40]!r}"
        )  # g1 pages declare og:image "None"
    request = urllib.request.Request(
        web_address(url), headers={"User-Agent": USER_AGENT}
    )
    try:
        with opener(request, timeout=timeout) as response:
            return decoded(response.read())
    except urllib.error.HTTPError as err:
        if err.code == 429:
            raise SourceError("HTTP 429 (muitos pedidos)", retryable=True) from err
        if err.code >= 500:
            raise SourceError(
                f"HTTP {err.code} (erro no servidor deles)", retryable=True
            ) from err
        raise SourceError(f"HTTP {err.code}") from err
    except http.client.HTTPException as err:
        # e.g. IncompleteRead: the server cut the body short
        raise SourceError("resposta cortada", retryable=True) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        timed_out = isinstance(err, TimeoutError) or isinstance(
            getattr(err, "reason", None), TimeoutError
        )
        raise SourceError(
            "demorou demais" if timed_out else "sem conexao", retryable=True
        ) from err


def fetch_bytes(
    url: str,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    opener=urllib.request.urlopen,
    sleep=time.sleep,
) -> bytes:
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return fetch_once(url, timeout, opener)
        except SourceError as err:
            if not err.retryable or attempt == FETCH_ATTEMPTS:
                raise
            sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable")


def parse_json(body: bytes):
    """JSON, or a SourceError that says what came instead (servers under load send HTML error pages)."""
    try:
        return json.loads(body)
    except ValueError as err:
        if not body.strip():
            raise SourceError("resposta vazia", retryable=True) from err
        if body.lstrip()[:1] == b"<":
            raise SourceError(
                "veio página de erro em vez de dados", retryable=True
            ) from err
        raise SourceError("dados em formato inesperado") from err


def short_age(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    hours = minutes // 60
    if hours >= 24:
        return f"{hours // 24}d"
    return f"{hours}h" if hours else f"{minutes}m"


def clock(moment: datetime, now: datetime) -> str:
    """Local time of day, with the date in front when it is not today."""
    local = moment.astimezone()
    if local.date() == now.astimezone().date():
        return f"{local:%H:%M}"
    return f"{local:%d/%m %H:%M}"


def br_number(value: float) -> str:
    """Brazilian notation (204.120 / 5,0533), with fewer decimals as the number grows."""
    decimals = 0 if value >= 10_000 else 2 if value >= 10 else 4
    us = f"{value:,.{decimals}f}"
    return us.replace(",", "_").replace(".", ",").replace("_", ".")


def br_percent(change: float) -> str:
    return f"{change * 100:+.2f}%".replace(".", ",")


def fetch_json(url: str, fetch: Callable[[str], bytes]):
    return parse_json(fetch(url))


def kept_download(url: str, keep: timedelta, folder: Path, now: float) -> bytes | None:
    """The copy of `url` saved by store_download, while younger than `keep`."""
    path = folder / hashlib.sha1(url.encode()).hexdigest()
    try:
        if now - path.stat().st_mtime < keep.total_seconds():
            return path.read_bytes()
    except OSError:
        pass
    return None


def store_download(url: str, body: bytes, folder: Path, now: float) -> None:
    """Called only after the body was read successfully, so an error page is never kept."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / hashlib.sha1(url.encode()).hexdigest()
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(body)
    temporary.replace(path)
    for old in folder.iterdir():
        if now - old.stat().st_mtime > DOWNLOAD_PRUNE.total_seconds():
            old.unlink(missing_ok=True)


def selftest() -> None:
    rss = b"""<rss><channel><title>x</title>
<item><title>Old  one</title><pubDate>Thu, 22 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>New one</title><pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>No date</title></item>
<item><title>Meta&amp;#8217;s AI</title><pubDate>Fri, 23 Oct 2026 11:30:00 -0000</pubDate></item>
</channel></rss>"""
    try:
        fetch_bytes("None")
    except SourceError as err:
        assert not err.retryable
    else:
        raise AssertionError(
            "a non-web address must fail as a SourceError, not a ValueError"
        )
    assert decoded(gzip.compress(rss)) == rss and decoded(rss) == rss
    assert (
        short_age(timedelta(minutes=30)) == "30m"
        and short_age(timedelta(hours=26)) == "1d"
    )
    assert (
        br_number(5.0533) == "5,0533"
        and br_number(57.83) == "57,83"
        and br_number(204120.12) == "204.120"
    )
    calls, waits = [], []

    def opener_factory(outcomes):
        def opener(request, timeout):
            calls.append(request.full_url)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return io.BytesIO(outcome)

        return opener

    gateway = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
    assert (
        fetch_bytes("http://x", 1, opener_factory([gateway, b"ok"]), waits.append)
        == b"ok"
    )
    assert len(calls) == 2 and waits == [RETRY_BACKOFF_SECONDS], (
        "a 502 is tried once more after a pause"
    )
    calls.clear()
    missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    try:
        fetch_bytes("http://x", 1, opener_factory([missing, b"ok"]), waits.append)
    except SourceError as err:
        assert str(err) == "HTTP 404" and len(calls) == 1, "a 404 is not retried"
    else:
        raise AssertionError("404 must fail")
    calls.clear()
    try:
        fetch_bytes("http://x", 1, opener_factory([gateway, gateway]), waits.append)
    except SourceError as err:
        assert "erro no servidor deles" in str(err) and len(calls) == FETCH_ATTEMPTS
    else:
        raise AssertionError("two 502s must fail")

    class CutShort(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"")

    replies = [CutShort(), io.BytesIO(b"ok")]
    assert (
        fetch_bytes(
            "http://x", 1, lambda request, timeout: replies.pop(0), waits.append
        )
        == b"ok"
    ), "a body cut short is tried once more instead of escaping as IncompleteRead"
    calls.clear()
    assert (
        fetch_bytes(
            "https://s/fotos/Ação São Paulo.jpg",
            1,
            opener_factory([b"ok"]),
            waits.append,
        )
        == b"ok"
    )
    assert calls == ["https://s/fotos/A%C3%A7%C3%A3o%20S%C3%A3o%20Paulo.jpg"], (
        "accents and spaces are percent-encoded: http.client sends ASCII only"
    )
    calls.clear()
    fetch_bytes("https://s/a%20b?q=1&r=%C3%A7", 1, opener_factory([b"ok"]))
    assert calls == ["https://s/a%20b?q=1&r=%C3%A7"], "an encoded address is kept"
    broken_gzip = [GZIP_MAGIC + b"\x08\x00garbage", GZIP_MAGIC + b"\x08\x00garbage"]
    try:
        fetch_bytes("http://x", 1, opener_factory(broken_gzip), waits.append)
    except SourceError as err:
        assert err.retryable, "a corrupt gzip body is tried once more"
    else:
        raise AssertionError("a corrupt gzip body must fail as a SourceError")
    for body, message in (
        (b"<html>erro</html>", "veio página de erro em vez de dados"),
        (b"  ", "resposta vazia"),
        (b"x{", "dados em formato inesperado"),
    ):
        try:
            parse_json(body)
        except SourceError as err:
            assert str(err) == message, err
        else:
            raise AssertionError(body)
    print("net selftest ok")


if __name__ == "__main__":
    selftest()

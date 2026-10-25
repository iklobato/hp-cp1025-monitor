"""One-paragraph Portuguese summaries of news stories, written by a cheap model on OpenRouter.

Each story is summarized once: the text is kept on disk by the story's link, so a restart
asks for nothing again, and old entries are dropped after KEEP. Every answer carries its
cost; the month's total is kept with the summaries, and nothing more is asked once it
reaches MONTHLY_BUDGET. Without OPENROUTER_API_KEY in the environment the feature is off.
"""

import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from net import CACHE_DIR, SourceError
from news_data import Feed, Story, plain_text

API_URL = "https://openrouter.ai/api/v1/chat/completions"
# Compared on 10 real stories on 2026-10-23: both kept the facts and the size, in Portuguese;
# qwen was the cheapest (~US$ 0.09 a month at 7,000 summaries), deepseek is the fallback.
MODELS = ("qwen/qwen3.7-flash", "deepseek/deepseek-v4-flash")
MONTHLY_BUDGET = 3.0  # US$, approved by the owner on 2026-10-23
KEEP = timedelta(days=3)
TIMEOUT_SECONDS = 30
MAX_INPUT_CHARS = 1500
SHORTER = 0.7  # second try when the first answer does not fit the card
PROMPT = (
    "Resuma a notícia abaixo em português do Brasil, em no máximo {chars} caracteres, numa ou duas "
    "frases completas. Diga o fato principal e o número mais importante, se houver. Não repita o "
    "título, não opine, sem emojis, sem markdown, sem aspas. Responda só com o resumo."
)
STORE = CACHE_DIR / "summaries.json"


def post_json(url: str, body: dict, key: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        raise SourceError(f"HTTP {err.code}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise SourceError("sem conexao") from err
    except ValueError as err:
        raise SourceError("resposta ilegível") from err


class Summaries:
    """The summaries already written, and the asking for new ones within the month's budget."""

    def __init__(
        self,
        key: str | None = None,
        path: Path = STORE,
        post: Callable[[str, dict, str], dict] = post_json,
    ) -> None:
        self.key = key if key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self.path, self.post = path, post
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError):
            stored = {}
        self.texts: dict[str, dict] = stored.get("texts", {})
        self.spent: dict[str, float] = stored.get("spent", {})

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def text(self, story: Story) -> str:
        """The kept summary, or "" when there is none (not asked yet, or it did not fit)."""
        return self.texts.get(story.link or story.title, {}).get("text", "")

    def spent_this_month(self, now: datetime) -> float:
        return self.spent.get(f"{now:%Y-%m}", 0.0)

    def fill(
        self,
        stories: list[Story],
        chars: int,
        fits: Callable[[Story, str], bool],
        now: datetime,
    ) -> list[str]:
        """Ask for each story without a summary; returns problems to show, saves once at the end."""
        if not self.enabled:
            return []
        problems, changed = [], False
        for story in stories:
            key = story.link or story.title
            if key in self.texts:
                continue
            if self.spent_this_month(now) >= MONTHLY_BUDGET:
                problems.append(
                    f"resumos parados: gasto do mês chegou a US$ {MONTHLY_BUDGET:.0f}"
                )
                break
            try:
                text = self._ask(story, chars, now)
                if not fits(story, text):
                    text = self._ask(story, int(chars * SHORTER), now)
            except SourceError as err:
                problems.append(f"resumo: {err}")
                break  # the service is down or refusing: try again on the next refresh
            # One that still does not fit is kept as "" so it is not asked again every refresh.
            self.texts[key] = {
                "text": text if fits(story, text) else "",
                "at": now.isoformat(),
            }
            changed = True
        if changed:
            self._save(now)
        return problems

    def _ask(self, story: Story, chars: int, now: datetime) -> str:
        source = plain_text(story.summary or "")[:MAX_INPUT_CHARS]
        failure = SourceError("sem modelo")
        for model in MODELS:
            body = {
                "model": model,
                "max_tokens": 160,
                "temperature": 0.3,
                "reasoning": {
                    "effort": "none"
                },  # thinking is billed; a summary does not need it
                "usage": {"include": True},
                "messages": [
                    {"role": "system", "content": PROMPT.format(chars=chars)},
                    {
                        "role": "user",
                        "content": f"Título: {story.title}\n\nTexto: {source}",
                    },
                ],
            }
            try:
                answer = self.post(API_URL, body, self.key)
                month = f"{now:%Y-%m}"
                self.spent[month] = self.spent.get(month, 0.0) + float(
                    answer.get("usage", {}).get("cost") or 0
                )
                text = " ".join(
                    (answer["choices"][0]["message"].get("content") or "").split()
                )
            except SourceError as err:
                failure = err
                continue
            except (KeyError, IndexError, TypeError, ValueError):
                failure = SourceError("resposta em formato inesperado")
                continue
            if text:
                return text
        raise failure

    def _save(self, now: datetime) -> None:
        cutoff = now - KEEP
        self.texts = {
            key: entry
            for key, entry in self.texts.items()
            if datetime.fromisoformat(entry["at"]) >= cutoff
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"texts": self.texts, "spent": self.spent}, ensure_ascii=False)
        )
        temporary.replace(self.path)


def selftest() -> None:
    now = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
    feed = Feed("G", "g1", "u")
    story = Story(
        now,
        "Operação prende 11 pessoas",
        feed,
        "Texto longo da notícia " * 20,
        "https://s/1",
    )
    other = Story(now, "Outra notícia", feed, "Texto", "https://s/2")
    calls = []

    def answer(text: str, cost: float = 0.0001) -> dict:
        return {"choices": [{"message": {"content": text}}], "usage": {"cost": cost}}

    def post(url: str, body: dict, key: str) -> dict:
        calls.append(body["model"])
        assert key == "k" and body["reasoning"] == {"effort": "none"}
        if "Outra" in body["messages"][1]["content"]:
            return answer("curto")
        return answer("  A operação   prendeu 11 pessoas.  ")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.json"
        off = Summaries("", path, post)
        assert off.fill([story], 180, lambda s, t: True, now) == [] and calls == [], (
            "no key: nothing asked"
        )
        summaries = Summaries("k", path, post)
        assert summaries.fill([story, other], 180, lambda s, t: len(t) < 40, now) == []
        assert summaries.text(story) == "A operação prendeu 11 pessoas." and calls == [
            MODELS[0],
            MODELS[0],
        ]
        again = Summaries("k", path, post)
        assert (
            again.fill([story], 180, lambda s, t: True, now) == [] and len(calls) == 2
        ), "kept on disk: not asked again"
        assert round(again.spent_this_month(now), 6) == 0.0002

        too_long = Summaries("k", Path(tmp) / "t.json", post)
        too_long.fill([story], 180, lambda s, t: False, now)
        assert too_long.text(story) == "" and len(calls) == 4, (
            "asked shorter once, then kept empty (nothing cut)"
        )

        def first_down(url: str, body: dict, key: str) -> dict:
            calls.append(body["model"])
            if body["model"] == MODELS[0]:
                raise SourceError("HTTP 503")
            return answer("Da reserva.")

        calls.clear()
        fallback = Summaries("k", Path(tmp) / "f.json", first_down)
        fallback.fill([story], 180, lambda s, t: True, now)
        assert fallback.text(story) == "Da reserva." and calls == list(MODELS), (
            "the second model takes over"
        )

        def all_down(url: str, body: dict, key: str) -> dict:
            raise SourceError("HTTP 429")

        down = Summaries("k", Path(tmp) / "d.json", all_down)
        assert down.fill([story, other], 180, lambda s, t: True, now) == [
            "resumo: HTTP 429"
        ]
        assert down.text(story) == "" and (story.link not in down.texts), (
            "a failure is retried later, not kept"
        )

        broke = Summaries("k", Path(tmp) / "b.json", post)
        broke.spent = {"2026-10": MONTHLY_BUDGET}
        calls.clear()
        assert (
            broke.fill([story], 180, lambda s, t: True, now)[0].startswith(
                "resumos parados"
            )
            and calls == []
        )

        old = Summaries("k", Path(tmp) / "o.json", post)
        old.texts = {
            "https://old": {"text": "x", "at": (now - timedelta(days=4)).isoformat()}
        }
        old.fill([story], 180, lambda s, t: True, now)
        assert "https://old" not in old.texts, "summaries older than 3 days are dropped"
    print("summary selftest ok")


if __name__ == "__main__":
    selftest()

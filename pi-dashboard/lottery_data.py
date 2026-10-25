"""Data for the Pi dashboard: lottery draws (Caixa)."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from net import SourceError, fetch_bytes, fetch_json


LOTTERY_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/{game}"


@dataclass(frozen=True)
class Draw:
    number: int
    day: date
    numbers: tuple[str, ...]
    second_numbers: tuple[str, ...]  # Dupla Sena's second draw
    clovers: tuple[str, ...]  # +Milionária's trevos
    extra: str  # Timemania's team or Dia de Sorte's month
    top_winners: int
    top_prize: float
    accumulated: bool
    next_day: date | None
    next_estimate: float


def draw_from_json(payload: dict) -> Draw:
    def day(text: str | None) -> date | None:
        return datetime.strptime(text, "%d/%m/%Y").date() if text else None

    try:
        top = (payload.get("listaRateioPremio") or [{}])[0]
        return Draw(
            int(payload["numero"]),
            day(payload["dataApuracao"]),
            tuple(payload["listaDezenas"]),
            tuple(payload.get("listaDezenasSegundoSorteio") or ()),
            tuple(payload.get("trevosSorteados") or ()),
            " ".join(
                "".join(
                    c
                    for c in payload.get("nomeTimeCoracaoMesSorte") or ""
                    if c.isprintable()
                ).split()
            ),
            int(top.get("numeroDeGanhadores") or 0),
            float(top.get("valorPremio") or 0),
            bool(payload.get("acumulado")),
            day(payload.get("dataProximoConcurso")),
            float(payload.get("valorEstimadoProximoConcurso") or 0),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err


def fetch_draw(game: str, fetch: Callable[[str], bytes] = fetch_bytes) -> Draw:
    return draw_from_json(fetch_json(LOTTERY_URL.format(game=game), fetch))


def selftest() -> None:
    draw = draw_from_json(
        {
            "numero": 3073,
            "dataApuracao": "22/10/2026",
            "listaDezenas": ["04", "13"],
            "listaDezenasSegundoSorteio": None,
            "trevosSorteados": ["2", "3"],
            "nomeTimeCoracaoMesSorte": "TOMBENSE         /MG",
            "acumulado": True,
            "listaRateioPremio": [{"numeroDeGanhadores": 0, "valorPremio": 0.0}],
            "dataProximoConcurso": "24/10/2026",
            "valorEstimadoProximoConcurso": 230000000.0,
        }
    )
    assert (
        draw.number == 3073
        and draw.clovers == ("2", "3")
        and draw.extra == "TOMBENSE /MG"
        and draw.accumulated
    )
    assert draw.next_day == date(2026, 10, 24) and draw.second_numbers == ()
    blank = draw_from_json(
        {
            "numero": 1,
            "dataApuracao": "22/10/2026",
            "listaDezenas": ["01"],
            "nomeTimeCoracaoMesSorte": "\x00" * 17,
        }
    )
    assert blank.extra == "", (
        "Caixa pads the team field with NUL characters in games without one"
    )
    print("lottery_data selftest ok")


if __name__ == "__main__":
    selftest()

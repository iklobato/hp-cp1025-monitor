"""Data for the Pi dashboard: Brazilian rates (Banco Central SGS, Focus and credit rates, IBGE
SIDRA, Tesouro Direto)."""

import csv
import io
import json
import os
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Callable

from net import (
    DOWNLOAD_DIR,
    SourceError,
    fetch_bytes,
    fetch_json,
    kept_download,
    parse_json,
    store_download,
)


# Banco Central's SGS answered one of three identical requests in 12.1 s (measured 2026-10-23).
SGS_TIMEOUT_SECONDS = 40
SIDRA_URL = "https://apisidra.ibge.gov.br/values/{path}"
TESOURO_URL = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/"
    "resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv"
)
# The Tesouro file holds every day since 2002 (14.6 MB on 2026-10-23); it comes over slowly.
TESOURO_TIMEOUT_SECONDS = 120
TESOURO_KEEP = timedelta(hours=12)  # one new price row per business day
SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados?formato=json&dataInicial={start}&dataFinal={end}"


def sgs_from_json(payload: list, today: date) -> tuple[tuple[date, float], ...]:
    """Banco Central series as (day, value); the Selic target is published ahead, so future days are dropped."""
    try:
        points = tuple(
            (datetime.strptime(row["data"], "%d/%m/%Y").date(), float(row["valor"]))
            for row in payload
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err
    kept = tuple(point for point in points if point[0] <= today)
    if not kept:
        raise SourceError("sem dados")
    return kept


def fetch_sgs(
    code: int,
    days: int,
    today: date,
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=SGS_TIMEOUT_SECONDS),
):
    start = today - timedelta(days=days)
    url = SGS_URL.format(code=code, start=f"{start:%d/%m/%Y}", end=f"{today:%d/%m/%Y}")
    payload = parse_json(fetch(url))
    return sgs_from_json(payload, today)


def sidra_from_json(payload: list) -> tuple[tuple[str, float], ...]:
    """IBGE SIDRA rows as (period name, value); the first row is the column legend."""
    try:
        rows = tuple(
            (row["D3N"], float(row["V"]))
            for row in payload[1:]
            if row["V"] not in ("...", "-", "X")
        )
    except (KeyError, TypeError, ValueError, IndexError) as err:
        raise SourceError("formato dos dados mudou") from err
    if not rows:
        raise SourceError("sem dados")
    return rows


def fetch_sidra(
    path: str,
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=SGS_TIMEOUT_SECONDS),
):
    payload = parse_json(fetch(SIDRA_URL.format(path=path)))
    return sidra_from_json(payload)


@dataclass(frozen=True)
class Bond:
    kind: str
    maturity: date
    rate: float  # % a.a. for buying; on top of IPCA or Selic for those titles
    price: float


@dataclass(frozen=True)
class Tesouro:
    day: date  # newest prices in the file (it runs about a week behind)
    on_sale: tuple[Bond, ...]
    history: dict  # (kind, maturity) -> ((day, buying rate), ...) since `since`, oldest first


def tesouro_from_csv(text: str, since: date) -> Tesouro:
    """Titles on sale on the newest day, and each title's buying rate from `since` on.

    The file holds every day since 2002; older rows are skipped while reading to keep memory low.
    """

    def number(value: str) -> float:
        return float(value.replace(".", "").replace(",", ".")) if value.strip() else 0.0

    by_day: dict[date, list[Bond]] = {}
    try:
        for row in csv.DictReader(io.StringIO(text), delimiter=";"):
            day = datetime.strptime(row["Data Base"], "%d/%m/%Y").date()
            if day < since:
                continue
            bond = Bond(
                row["Tipo Titulo"],
                datetime.strptime(row["Data Vencimento"], "%d/%m/%Y").date(),
                number(row["Taxa Compra Manha"]),
                number(row["PU Compra Manha"]),
            )
            by_day.setdefault(day, []).append(bond)
    except (KeyError, ValueError) as err:
        raise SourceError("planilha do Tesouro mudou") from err
    if not by_day:
        raise SourceError("sem dados")
    newest = max(by_day)
    on_sale = tuple(
        sorted(
            (b for b in by_day[newest] if b.price > 0),
            key=lambda b: (b.kind, b.maturity),
        )
    )
    history: dict[tuple[str, date], list[tuple[date, float]]] = {}
    for day in sorted(by_day):
        for bond in by_day[day]:
            if bond.rate:
                history.setdefault((bond.kind, bond.maturity), []).append(
                    (day, bond.rate)
                )
    return Tesouro(
        newest, on_sale, {key: tuple(points) for key, points in history.items()}
    )


def fetch_tesouro(
    since: date,
    fetch: Callable[[str], bytes] = partial(
        fetch_bytes, timeout=TESOURO_TIMEOUT_SECONDS
    ),
    folder: Path = DOWNLOAD_DIR,
    now: Callable[[], float] = time.time,
):
    body = kept_download(TESOURO_URL, TESOURO_KEEP, folder, now())
    downloaded = body is None
    if downloaded:
        body = fetch(TESOURO_URL)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    result = tesouro_from_csv(text, since)
    if downloaded:
        store_download(TESOURO_URL, body, folder, now())
    return result


FOCUS_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais"
    "?$format=json&$top=20000&$select=Indicador,Data,DataReferencia,Mediana&$filter={filter}"
)


def focus_from_json(
    payload: dict,
) -> dict[tuple[str, str], tuple[tuple[date, float], ...]]:
    """Focus survey medians per (indicator, year), oldest first; one survey a week."""
    try:
        found: dict[tuple[str, str], dict[date, float]] = {}
        for row in payload["value"]:
            key = (row["Indicador"], str(row["DataReferencia"]))
            found.setdefault(key, {})[date.fromisoformat(row["Data"])] = float(
                row["Mediana"]
            )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("formato dos dados mudou") from err
    if not found:
        raise SourceError("sem dados")
    return {key: tuple(sorted(points.items())) for key, points in found.items()}


def fetch_focus(
    indicators: tuple[str, ...],
    since: date,
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=SGS_TIMEOUT_SECONDS),
):
    names = " or ".join(f"Indicador eq '{name}'" for name in indicators)
    odata = f"({names}) and baseCalculo eq 0 and Data ge '{since:%Y-%m-%d}'"
    return focus_from_json(
        fetch_json(FOCUS_URL.format(filter=urllib.parse.quote(odata)), fetch)
    )


CREDIT_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/taxaJuros/versao/v2/odata/TaxasJurosDiariaPorInicioPeriodo"
    "?$format=json&$top=20000&{query}"
)
CREDIT_SEGMENT = "PESSOA FÍSICA"


def credit_medians(rows: list[dict]) -> dict[str, float]:
    """Median yearly rate across banks for each loan type."""
    by_type: dict[str, list[float]] = {}
    for row in rows:
        if (
            row.get("Segmento") == CREDIT_SEGMENT
            and row.get("TaxaJurosAoAno") is not None
        ):
            by_type.setdefault(row["Modalidade"], []).append(
                float(row["TaxaJurosAoAno"])
            )
    return {
        name: sorted(values)[len(values) // 2]
        if len(values) % 2
        else (sorted(values)[len(values) // 2 - 1] + sorted(values)[len(values) // 2])
        / 2
        for name, values in by_type.items()
    }


@dataclass(frozen=True)
class Credit:
    week: date
    now: dict[str, float]
    month_ago: dict[str, float]
    year_ago: dict[str, float]


def fetch_credit(
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=SGS_TIMEOUT_SECONDS),
) -> Credit:
    """Banco Central's weekly survey of what banks charge individuals, now, ~1 month and ~1 year ago."""
    segment = urllib.parse.quote(
        f"Segmento eq '{CREDIT_SEGMENT}' and Modalidade eq 'Cheque especial - Prefixado'"
    )
    weeks_query = (
        f"$select=InicioPeriodo&$orderby=InicioPeriodo%20desc&$filter={segment}"
    )
    weeks = sorted(
        {
            date.fromisoformat(row["InicioPeriodo"])
            for row in fetch_json(CREDIT_URL.format(query=weeks_query), fetch).get(
                "value", []
            )
        },
        reverse=True,
    )
    if not weeks:
        raise SourceError("sem dados")

    def at(target: date) -> tuple[date, dict[str, float]]:
        week = next((w for w in weeks if w <= target), weeks[-1])
        odata = urllib.parse.quote(f"InicioPeriodo eq '{week:%Y-%m-%d}'")
        rows = fetch_json(
            CREDIT_URL.format(
                query=f"$select=Segmento,Modalidade,TaxaJurosAoAno&$filter={odata}"
            ),
            fetch,
        )
        return week, credit_medians(rows.get("value", []))

    newest, now = at(weeks[0])
    return Credit(
        newest,
        now,
        at(newest - timedelta(days=28))[1],
        at(newest - timedelta(days=364))[1],
    )


def selftest() -> None:
    selic = sgs_from_json(
        [
            {"data": "23/10/2026", "valor": "13.75"},
            {"data": "04/11/2026", "valor": "13.75"},
        ],
        date(2026, 10, 23),
    )
    assert selic == ((date(2026, 10, 23), 13.75),), "future Selic days are dropped"
    try:
        sgs_from_json([{"data": "x"}], date(2026, 10, 23))
    except SourceError:
        pass
    else:
        raise AssertionError("bad SGS row must fail")
    focus = focus_from_json(
        {
            "value": [
                {
                    "Indicador": "Selic",
                    "Data": "2026-10-16",
                    "DataReferencia": "2026",
                    "Mediana": 13.25,
                },
                {
                    "Indicador": "Selic",
                    "Data": "2026-10-09",
                    "DataReferencia": "2026",
                    "Mediana": 13.5,
                },
                {
                    "Indicador": "IPCA",
                    "Data": "2026-10-16",
                    "DataReferencia": 2027,
                    "Mediana": 4.32,
                },
            ]
        }
    )
    assert focus[("Selic", "2026")] == (
        (date(2026, 10, 9), 13.5),
        (date(2026, 10, 16), 13.25),
    )
    assert ("IPCA", "2027") in focus, (
        "a year that comes as a number is a key like the others"
    )
    medians = credit_medians(
        [
            {"Segmento": CREDIT_SEGMENT, "Modalidade": "Cheque", "TaxaJurosAoAno": v}
            for v in (100.0, 150.0, 200.0, 400.0)
        ]
        + [
            {
                "Segmento": "PESSOA JURÍDICA",
                "Modalidade": "Cheque",
                "TaxaJurosAoAno": 1.0,
            },
            {
                "Segmento": CREDIT_SEGMENT,
                "Modalidade": "Rotativo",
                "TaxaJurosAoAno": 476.33,
            },
        ]
    )
    assert medians == {"Cheque": 175.0, "Rotativo": 476.33}, medians
    pages = {
        "weeks": {
            "value": [
                {"InicioPeriodo": d} for d in ("2026-10-02", "2026-09-04", "2025-10-03")
            ]
        }
    }

    def fake_credit(url):
        if "orderby" in url:
            return json.dumps(pages["weeks"]).encode()
        week = urllib.parse.unquote(url).split("InicioPeriodo eq '")[1][:10]
        return json.dumps(
            {
                "value": [
                    {
                        "Segmento": CREDIT_SEGMENT,
                        "Modalidade": "Cheque",
                        "TaxaJurosAoAno": {
                            "2026-10-02": 155.0,
                            "2026-09-04": 150.0,
                            "2025-10-03": 140.0,
                        }[week],
                    }
                ]
            }
        ).encode()

    credit = fetch_credit(fake_credit)
    assert credit.week == date(2026, 10, 2) and credit.now == {"Cheque": 155.0}
    assert credit.month_ago == {"Cheque": 150.0} and credit.year_ago == {
        "Cheque": 140.0
    }
    jobless = sidra_from_json(
        [
            {"V": "Valor", "D3N": "Trimestre"},
            {"V": "5.4", "D3N": "abr-mai-jun 2026"},
            {"V": "...", "D3N": "mai-jun-jul 2026"},
        ]
    )
    assert jobless == (("abr-mai-jun 2026", 5.4),), (
        "IBGE placeholders like ... are skipped"
    )
    tesouro = tesouro_from_csv(
        "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        "Tesouro Selic;01/03/2027;15/10/2026;0,01;0,02;20097,80;20086,80;20086,80\n"
        "Tesouro IPCA+;15/05/2035;22/10/2026;7,45;7,57;2.345,67;2.300,00;2.300,00\n"
        "Tesouro Selic;01/03/2027;22/10/2026;0,02;0,03;20101,10;20090,00;20090,00\n"
        "Tesouro Prefixado;01/01/2027;22/10/2026;;13,50;0,00;980,00;980,00\n"
        "Tesouro Selic;01/03/2027;01/01/2020;0,10;0,20;10000,00;10000,00;10000,00\n",
        date(2025, 10, 1),
    )
    newest, bonds = tesouro.day, tesouro.on_sale
    assert tesouro.history[("Tesouro Selic", date(2027, 3, 1))] == (
        (date(2026, 10, 15), 0.01),
        (date(2026, 10, 22), 0.02),
    ), "history oldest first, rows before `since` skipped"
    assert newest == date(2026, 10, 22) and [b.kind for b in bonds] == [
        "Tesouro IPCA+",
        "Tesouro Selic",
    ]
    assert bonds[0].price == 2345.67 and bonds[0].rate == 7.45, (
        "Brazilian thousands and decimal marks"
    )
    try:
        tesouro_from_csv("outra;coisa\n1;2\n", date(2025, 1, 1))
    except SourceError:
        pass
    else:
        raise AssertionError("a changed spreadsheet must fail loudly")
    csv = (
        "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;PU Venda Manha;PU Base Manha\n"
        "Tesouro Selic;01/03/2027;22/10/2026;0,02;0,03;20101,10;20090,00;20090,00\n"
    ).encode()
    with tempfile.TemporaryDirectory() as tmp:
        downloads, moment = (
            [],
            [time.time()],
        )  # file times are real, so the clock starts now

        def counted(url: str) -> bytes:
            downloads.append(url)
            return csv

        def broken(url: str) -> bytes:
            downloads.append(url)
            return b"<html>pagina de erro</html>"

        folder = Path(tmp)
        try:
            fetch_tesouro(date(2026, 1, 1), broken, folder, lambda: moment[0])
        except SourceError:
            pass
        else:
            raise AssertionError("an error page must fail")
        assert not list(folder.iterdir()), "an error page is never kept"
        first = fetch_tesouro(date(2026, 1, 1), counted, folder, lambda: moment[0])
        moment[0] += 3600
        again = fetch_tesouro(date(2026, 1, 1), counted, folder, lambda: moment[0])
        assert first == again and len(downloads) == 2, (
            "a restart within 12 h reads the disk copy"
        )
        stamp = moment[0] + TESOURO_KEEP.total_seconds()
        for kept in folder.iterdir():
            os.utime(kept, (stamp - 13 * 3600, stamp - 13 * 3600))
        fetch_tesouro(date(2026, 1, 1), counted, folder, lambda: stamp)
        assert len(downloads) == 3, "an old copy is downloaded again"
        old = folder / "sobra"
        old.write_bytes(b"x")
        os.utime(old, (0, 0))
        store_download("https://x", b"y", folder, stamp)
        assert not old.exists(), "copies older than 30 days are deleted"
    print("rates_data selftest ok")


if __name__ == "__main__":
    selftest()

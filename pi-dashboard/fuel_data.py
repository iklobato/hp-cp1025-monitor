"""Data for the Pi dashboard: fuel prices at the stations (ANP weekly survey)."""

import io
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import Callable

from net import DOWNLOAD_DIR, SourceError, fetch_bytes, kept_download, store_download
from news_data import title_key


ANP_PAGE_URL = (
    "https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/precos/"
    "levantamento-de-precos-de-combustiveis-ultimas-semanas-pesquisadas"
)
ANP_TIMEOUT_SECONDS = 90
ANP_KEEP = timedelta(days=7)  # a published weekly survey file never changes
ANP_DATES = re.compile(
    r"(\d{4})[-_](\d{2})[-_](\d{2})[-_](\d{4})[-_](\d{2})[-_](\d{2})"
)


def anp_station_files(page: str) -> list[tuple[date, str]]:
    """Per-station weekly price sheets linked on the ANP page, newest first (file names vary in - and _)."""
    found = {}
    for url in re.findall(r'https?://[^"\s]+revendas_lpc[^"\s]+\.xlsx', page):
        match = ANP_DATES.search(url.rsplit("/", 1)[-1])
        if match:
            end = date(int(match[4]), int(match[5]), int(match[6]))
            found[end] = url
    return sorted(found.items(), reverse=True)


XLSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def xlsx_rows(data: bytes):
    """Rows of the first sheet as lists of strings, read with the standard library (no openpyxl)."""
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        shared = [
            "".join(t.text or "" for t in item.iter(XLSX + "t"))
            for item in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(
                XLSX + "si"
            )
        ]
        with book.open("xl/worksheets/sheet1.xml") as sheet:
            for _, element in ET.iterparse(sheet):
                if element.tag != XLSX + "row":
                    continue
                values = []
                for cell in element.iter(XLSX + "c"):
                    value = cell.find(XLSX + "v")
                    text = "" if value is None or value.text is None else value.text
                    values.append(
                        shared[int(text)] if cell.get("t") == "s" and text else text
                    )
                yield values
                element.clear()


@dataclass(frozen=True)
class StationPrice:
    city: str
    station: str
    address: str
    brand: str
    product: str
    price: float


def station_prices(rows, cities: tuple[str, ...]) -> tuple[StationPrice, ...]:
    """Rows after the ANP header line (CNPJ, RAZÃO, FANTASIA, ...) for the given cities in São Paulo state."""
    wanted = {title_key(city) for city in cities}
    columns: dict[str, int] | None = None
    prices = []
    for row in rows:
        if columns is None:
            if row and row[0] == "CNPJ":
                columns = {name: index for index, name in enumerate(row)}
            continue
        try:
            city = row[columns["MUNICÍPIO"]]
            if title_key(city) not in wanted or row[columns["ESTADO"]] != "SAO PAULO":
                continue
            address = f"{row[columns['ENDEREÇO']]}, {row[columns['NÚMERO']]} - {row[columns['BAIRRO']]}"
            prices.append(
                StationPrice(
                    city,
                    row[columns["FANTASIA"]] or row[columns["RAZÃO"]],
                    address,
                    row[columns["BANDEIRA"]],
                    row[columns["PRODUTO"]],
                    float(row[columns["PREÇO DE REVENDA"]].replace(",", ".")),
                )
            )
        except (IndexError, KeyError, ValueError):
            continue
    if columns is None:
        raise SourceError("planilha da ANP mudou")
    return tuple(prices)


@dataclass(frozen=True)
class FuelWeek:
    end: date
    prices: tuple[StationPrice, ...]


def fetch_fuel_weeks(
    cities: tuple[str, ...],
    weeks: int = 2,
    fetch: Callable[[str], bytes] = partial(fetch_bytes, timeout=ANP_TIMEOUT_SECONDS),
    folder: Path = DOWNLOAD_DIR,
    now: Callable[[], float] = time.time,
):
    """The newest `weeks` station surveys for these cities, newest first."""
    files = anp_station_files(fetch(ANP_PAGE_URL).decode("utf-8", errors="replace"))
    if not files:
        raise SourceError("ANP sem planilhas")
    result = []
    for end, url in files[:weeks]:
        body = kept_download(url, ANP_KEEP, folder, now())
        downloaded = body is None
        if downloaded:
            body = fetch(url)
        try:
            prices = station_prices(xlsx_rows(body), cities)
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as err:
            raise SourceError("planilha da ANP ilegível") from err
        if downloaded:
            store_download(url, body, folder, now())
        result.append(FuelWeek(end, prices))
    return tuple(result)


def selftest() -> None:
    page = (
        '<a href="https://www.gov.br/x/arquivos-lpc/2026/revendas_lpc_2026-10-04_2026-10-10.xlsx">'
        '<a href="https://www.gov.br/x/arquivos-lpc/2026/revendas_lpc_2026_10_11-2026_10_17.xlsx">'
        '<a href="https://www.gov.br/x/arquivos-lpc/2026/resumo_semanal_lpc_2026-10-11_2026-10-17.xlsx">'
    )
    files = anp_station_files(page)
    assert [end for end, _ in files] == [date(2026, 10, 17), date(2026, 10, 10)], (
        "mixed - and _ names, newest first"
    )
    header = [
        "CNPJ",
        "RAZÃO",
        "FANTASIA",
        "ENDEREÇO",
        "NÚMERO",
        "COMPLEMENTO",
        "BAIRRO",
        "CEP",
        "MUNICÍPIO",
        "ESTADO",
        "BANDEIRA",
        "PRODUTO",
        "UNIDADE DE MEDIDA",
        "PREÇO DE REVENDA",
    ]
    rows = [
        ["PERÍODO: 11/10/2026 A 17/10/2026"],
        header,
        [
            "1",
            "POSTO A LTDA",
            "POSTO A",
            "AV X",
            "10",
            "",
            "CENTRO",
            "1",
            "SOROCABA",
            "SAO PAULO",
            "SHELL",
            "GASOLINA COMUM",
            "R$ / litro",
            "6.19",
        ],
        [
            "2",
            "POSTO B LTDA",
            "",
            "RUA Y",
            "5",
            "",
            "VILA",
            "1",
            "SOROCABA",
            "MINAS GERAIS",
            "BRANCA",
            "ETANOL",
            "R$ / litro",
            "4.09",
        ],
        [
            "3",
            "POSTO C LTDA",
            "",
            "RUA Z",
            "7",
            "",
            "CENTRO",
            "1",
            "JUNDIAI",
            "SAO PAULO",
            "BRANCA",
            "ETANOL",
            "R$ / litro",
            "4,19",
        ],
    ]
    found = station_prices(rows, ("Sorocaba", "Jundiaí"))
    assert [(p.station, p.city, p.price) for p in found] == [
        ("POSTO A", "SOROCABA", 6.19),
        ("POSTO C LTDA", "JUNDIAI", 4.19),
    ], (
        "same-name city in another state is skipped; empty trade name falls back to the company"
    )
    try:
        station_prices([["outra planilha"]], ("Sorocaba",))
    except SourceError:
        pass
    else:
        raise AssertionError("a sheet without the header must fail")
    print("fuel_data selftest ok")


if __name__ == "__main__":
    selftest()

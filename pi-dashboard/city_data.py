"""Data for the Pi dashboard: a typed city name to its IBGE code and coordinates (Open-Meteo)."""

import json
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
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
from news_data import title_key


IBGE_CITIES_URL = (
    "https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado"
)
IBGE_CITIES_KEEP = timedelta(days=30)  # 5,571 cities; a new one appears once in years
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search?name={name}&count=20&language=pt&countryCode=BR"
STATE_SUFFIX = re.compile(r"(.+?)\s*(?:,|/|\s-\s)\s*([A-Za-z]{2})")


@dataclass(frozen=True)
class City:
    name: str
    state: str  # two letters, SP
    ibge: str
    latitude: float
    longitude: float


def split_state(typed: str) -> tuple[str, str]:
    """'Bom Jesus, PI' -> ('Bom Jesus', 'PI'); a name alone has no state."""
    match = STATE_SUFFIX.fullmatch(typed.strip())
    return (match[1], match[2].upper()) if match else (typed.strip(), "")


def ibge_city(payload, typed: str) -> tuple[str, str, str, str]:
    """(name, state, state name, IBGE code) of the only city IBGE knows by that name."""
    name, state = split_state(typed)
    try:
        found = [
            (
                row["municipio-nome"],
                row["UF-sigla"],
                row["UF-nome"],
                str(row["municipio-id"]),
            )
            for row in payload
            if title_key(row["municipio-nome"]) == title_key(name)
            and state in ("", row["UF-sigla"])
        ]
    except (KeyError, TypeError) as err:
        raise SourceError("formato dos dados do IBGE mudou") from err
    if not found:
        raise SourceError(f"não achei a cidade {typed.strip()!r}")
    if len(found) > 1:
        states = ", ".join(sorted(city[1] for city in found))
        raise SourceError(
            f"{found[0][0]} existe em {states}: escreva '{found[0][0]}, UF'"
        )
    return found[0]


def coordinates(payload: dict, name: str, state_name: str) -> tuple[float, float]:
    for place in payload.get("results") or []:
        if title_key(place.get("name", "")) == title_key(name) and title_key(
            place.get("admin1", "")
        ) == title_key(state_name):
            return float(place["latitude"]), float(place["longitude"])
    raise SourceError(f"não achei {name} ({state_name}) no mapa")


def find_city(
    typed: str,
    fetch: Callable[[str], bytes] = fetch_bytes,
    folder: Path = DOWNLOAD_DIR,
    now: Callable[[], float] = time.time,
) -> City:
    """A city typed by name, as IBGE writes it, with its code and Open-Meteo's coordinates."""
    body = kept_download(IBGE_CITIES_URL, IBGE_CITIES_KEEP, folder, now())
    downloaded = body is None
    if downloaded:
        body = fetch(IBGE_CITIES_URL)
    name, state, state_name, ibge = ibge_city(parse_json(body), typed)
    if downloaded:
        store_download(IBGE_CITIES_URL, body, folder, now())
    latitude, longitude = coordinates(
        fetch_json(GEOCODE_URL.format(name=urllib.parse.quote(name)), fetch),
        name,
        state_name,
    )
    return City(name, state, ibge, latitude, longitude)


def selftest() -> None:
    assert split_state("Bom Jesus, pi") == ("Bom Jesus", "PI")
    assert split_state("Embu-Guaçu") == ("Embu-Guaçu", ""), (
        "a hyphen inside a name is not a state"
    )
    assert split_state(" Campinas / SP ") == ("Campinas", "SP")
    ibge_rows = [
        {
            "municipio-nome": name,
            "UF-sigla": uf,
            "UF-nome": uf_name,
            "municipio-id": code,
        }
        for name, uf, uf_name, code in (
            ("Campinas", "SP", "São Paulo", 3509502),
            ("Bom Jesus", "PI", "Piauí", 2201903),
            ("Bom Jesus", "RS", "Rio Grande do Sul", 4302303),
        )
    ]
    geocoded = {
        "results": [
            {
                "name": "Campinas do Sul",
                "admin1": "Rio Grande do Sul",
                "latitude": -27.4,
                "longitude": -52.5,
            },
            {
                "name": "Campinas",
                "admin1": "São Paulo",
                "latitude": -22.90556,
                "longitude": -47.06083,
            },
        ]
    }
    with tempfile.TemporaryDirectory() as tmp:
        asked = []

        def geo_fetch(url: str) -> bytes:
            asked.append(url)
            return json.dumps(
                ibge_rows if url == IBGE_CITIES_URL else geocoded
            ).encode()

        found = find_city("campinas", geo_fetch, Path(tmp), time.time)
        assert found == City("Campinas", "SP", "3509502", -22.90556, -47.06083), found
        find_city("Campinas, SP", geo_fetch, Path(tmp), time.time)
        assert asked.count(IBGE_CITIES_URL) == 1, (
            "the IBGE list is downloaded once, then kept"
        )
        for typed, message in (
            ("Bom Jesus", "Bom Jesus existe em PI, RS: escreva 'Bom Jesus, UF'"),
            ("Campinas, RJ", "não achei a cidade 'Campinas, RJ'"),
            ("Bom Jesus, PI", "não achei Bom Jesus (Piauí) no mapa"),
        ):
            try:
                find_city(typed, geo_fetch, Path(tmp), time.time)
            except SourceError as err:
                assert str(err) == message, err
            else:
                raise AssertionError(f"found {typed!r}")
    print("city_data selftest ok")


if __name__ == "__main__":
    selftest()

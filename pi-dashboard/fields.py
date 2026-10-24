"""Settings a block takes from carrossel.toml ([blocos.<id>]) and that the settings page edits.

Each block lists its fields; `read` checks the raw TOML value (missing: the default) and
raises ValueError with a sentence in Portuguese, `describe` tells admin.html how to draw the
field. Values come back as plain lists, strings and numbers, so the same value is valid TOML,
valid JSON and what the block receives.
"""

import math
import re
from dataclasses import dataclass

MAX_ITEMS = (
    40  # far above any real list here; stops a broken file from making a huge screen
)


def text(value, where: str, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: precisa de um texto")
    if len(value) > max_length:
        raise ValueError(f"{where}: texto longo demais (máximo {max_length})")
    return value.strip()


def number(value, where: str, low: float, high: float, whole: bool) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{where}: precisa ser um número")
    if whole and value != int(value):
        raise ValueError(f"{where}: precisa ser um número inteiro")
    if not low <= value <= high:
        raise ValueError(
            f"{where}: precisa estar entre {low:g} e {high:g}, veio {value:g}"
        )
    return int(value) if whole else float(value)


def items(value, where: str, low: int, high: int) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{where}: precisa ser uma lista")
    if not low <= len(value) <= min(high, MAX_ITEMS):
        raise ValueError(
            f"{where}: precisa ter de {low} a {min(high, MAX_ITEMS)} itens, tem {len(value)}"
        )
    return value


@dataclass(frozen=True)
class Column:
    """One column of a Table: kind is texto, url, sigla (one letter), numero or escolha."""

    key: str
    label: str
    kind: str = "texto"
    options: tuple[tuple[str, str], ...] = ()
    low: float = -1e9
    high: float = 1e9

    def read(self, value, where: str):
        where = f"{where}, {self.label.lower()}"
        if self.kind == "numero":
            return number(value, where, self.low, self.high, whole=False)
        found = text(value, where)
        if self.kind == "url" and not found.startswith(("http://", "https://")):
            raise ValueError(f"{where}: precisa começar com http:// ou https://")
        if self.kind == "sigla" and len(found) != 1:
            raise ValueError(f"{where}: precisa ser uma letra só")
        if self.kind == "escolha" and found not in dict(self.options):
            raise ValueError(f"{where}: {found!r} não é uma opção")
        return found

    def describe(self) -> dict:
        return {
            "chave": self.key,
            "rotulo": self.label,
            "tipo": self.kind,
            "opcoes": [list(option) for option in self.options],
        }


@dataclass(frozen=True)
class Field:
    """kind: texto, numero (whole numbers), decimal, palavras (list of words), escolhas (some
    of `options`), tabela (rows of `columns`)."""

    key: str
    label: str
    kind: str
    default: object
    help: str = ""
    low: float = 0
    high: float = 1e9
    options: tuple[tuple[str, str], ...] = ()
    columns: tuple[Column, ...] = ()
    unique: str = ""  # tabela: the column whose values must not repeat
    pattern: str = ""  # texto: the whole value must match this regular expression

    def read(self, raw: dict, where: str):
        value = raw.get(self.key, self.default)
        where = f"{where}.{self.key}"
        if self.kind == "texto":
            found = text(value, where)
            if self.pattern and not re.fullmatch(self.pattern, found):
                raise ValueError(
                    f"{where}: formato inválido ({self.help or self.pattern})"
                )
            return found
        if self.kind == "decimal":
            return number(value, where, self.low, self.high, whole=False)
        if self.kind == "numero":
            return number(value, where, self.low, self.high, whole=True)
        if self.kind == "palavras":
            return [
                text(word, where, 60)
                for word in items(value, where, int(self.low), int(self.high))
            ]
        if self.kind == "escolhas":
            chosen = items(value, where, int(self.low), int(self.high))
            unknown = [c for c in chosen if c not in dict(self.options)]
            if unknown:
                raise ValueError(f"{where}: {unknown[0]!r} não é uma opção")
            return [
                option for option, _ in self.options if option in chosen
            ]  # canonical order
        if self.kind == "tabela":
            rows = []
            for number_, row in enumerate(
                items(value, where, int(self.low), int(self.high)), start=1
            ):
                if not isinstance(row, dict):
                    raise ValueError(f"{where}: linha {number_} inválida")
                rows.append(
                    {
                        column.key: column.read(
                            row.get(column.key), f"{where} linha {number_}"
                        )
                        for column in self.columns
                    }
                )
            if self.unique:
                seen = [row[self.unique] for row in rows]
                repeated = next((v for v in seen if seen.count(v) > 1), None)
                if repeated is not None:
                    raise ValueError(f"{where}: {repeated!r} aparece mais de uma vez")
            return rows
        raise ValueError(f"{where}: tipo de campo desconhecido {self.kind}")

    def describe(self) -> dict:
        return {
            "chave": self.key,
            "rotulo": self.label,
            "tipo": self.kind,
            "ajuda": self.help,
            "minimo": self.low,
            "maximo": self.high,
            "opcoes": [list(o) for o in self.options],
            "colunas": [column.describe() for column in self.columns],
        }


@dataclass(frozen=True)
class CityFields:
    """Fields of one city: the page asks only for the name, the settings page fills the rest
    from it (sources.find_city). ibge is empty when the block does not need the code."""

    name: str
    latitude: str
    longitude: str
    ibge: str = ""

    def filled(self) -> tuple[str, ...]:
        return tuple(key for key in (self.latitude, self.longitude, self.ibge) if key)


def read_all(fields: tuple[Field, ...], raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: precisa ser um grupo de campos")
    unknown = sorted(set(raw) - {field.key for field in fields})
    if unknown:
        raise ValueError(f"{where}: campo desconhecido {unknown[0]!r}")
    return {field.key: field.read(raw, where) for field in fields}


def selftest() -> None:
    words = Field("palavras", "Palavras", "palavras", ["Sorocaba"], low=0, high=5)
    games = Field(
        "jogos",
        "Jogos",
        "escolhas",
        ["quina"],
        low=1,
        high=9,
        options=(("megasena", "Mega-Sena"), ("quina", "Quina")),
    )
    count = Field("postos", "Postos", "numero", 3, low=1, high=10)
    feeds = Field(
        "fontes",
        "Fontes",
        "tabela",
        [],
        low=1,
        high=20,
        unique="url",
        columns=(
            Column("sigla", "Sigla", "sigla"),
            Column("url", "Endereço", "url"),
            Column("lat", "Latitude", "numero", low=-90, high=90),
        ),
    )
    code = Field("ibge", "IBGE", "texto", "3550308", help="7 dígitos", pattern=r"\d{7}")
    latitude = Field("lat", "Latitude", "decimal", -23.0, low=-90, high=90)
    assert read_all((code, latitude), {"lat": -22.9}, "b") == {
        "ibge": "3550308",
        "lat": -22.9,
    }
    for raw, message in (
        ({"ibge": "35234"}, "formato inválido (7 dígitos)"),
        ({"lat": -91}, "entre -90 e 90"),
    ):
        try:
            read_all((code, latitude), raw, "b")
        except ValueError as err:
            assert message in str(err), (message, str(err))
        else:
            raise AssertionError(f"accepted {raw}")
    all_fields = (words, games, count, feeds)
    feed_rows = [{"sigla": "G", "url": "https://g1/rss", "lat": -23}]
    values = read_all(
        all_fields, {"fontes": feed_rows, "jogos": ["quina", "megasena"]}, "blocos.x"
    )
    assert values == {
        "palavras": ["Sorocaba"],
        "jogos": ["megasena", "quina"],
        "postos": 3,
        "fontes": [{"sigla": "G", "url": "https://g1/rss", "lat": -23.0}],
    }, values
    assert read_all((words,), {"palavras": ["  Copom "]}, "b")["palavras"] == ["Copom"]
    for raw, message in (
        ({"postos": 0}, "entre 1 e 10"),
        ({"postos": 2.5}, "inteiro"),
        ({"postos": True}, "número"),
        ({"palavras": ["a", ""]}, "precisa de um texto"),
        ({"palavras": "Sorocaba"}, "lista"),
        ({"jogos": []}, "de 1 a 9"),
        ({"jogos": ["loto"]}, "não é uma opção"),
        ({"fontes": [{"sigla": "GG", "url": "https://a", "lat": 0}]}, "uma letra só"),
        ({"fontes": [{"sigla": "G", "url": "ftp://a", "lat": 0}]}, "http"),
        ({"fontes": [{"sigla": "G", "url": "https://a", "lat": 91}]}, "entre -90 e 90"),
        (
            {"fontes": [{"sigla": "G", "url": "https://a", "lat": 0}] * 2},
            "mais de uma vez",
        ),
        ({"fontes": ["x"]}, "linha 1 inválida"),
        ({"cor": "azul"}, "campo desconhecido 'cor'"),
    ):
        try:
            read_all(all_fields, {"fontes": feed_rows, **raw}, "blocos.x")
        except ValueError as err:
            assert message in str(err), (message, str(err))
        else:
            raise AssertionError(f"accepted {raw}")
    assert feeds.describe()["colunas"][0]["tipo"] == "sigla"
    print("fields selftest ok")


if __name__ == "__main__":
    selftest()

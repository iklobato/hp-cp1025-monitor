"""Carousel layout from ~/dashboard/carrossel.toml: which screens, which blocks on each,
how the screen is split, and every timing (seconds on screen, data, redraw, live part).

The file is in Portuguese for the person who edits it; a wrong file fails loudly at start
with the line to fix, instead of drawing something half-configured.
"""

import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import fields
import ui

CONFIG = Path(
    os.environ.get("PI_DASHBOARD_CONFIG", Path.home() / "dashboard" / "carrossel.toml")
)
MODES = ("cheio", "compacto")
SPLITS = ("lado", "empilhado")


class ConfigError(Exception):
    """carrossel.toml cannot be used as written; the message says what to change."""


@dataclass(frozen=True)
class Part:
    block: str
    page: ui.Page
    mode: str
    share: float


@dataclass(frozen=True)
class Screen:
    name: str
    seconds: float
    redraw: float
    live: (
        float | None
    )  # seconds between redraws of the blocks marked live; None: no live part
    split: str
    parts: tuple[Part, ...]

    def boxes(self, box: tuple) -> list[tuple]:
        """Each part's rectangle inside `box`, sized by its share, with the usual gutter between."""
        left, top, right, bottom = box
        total = sum(part.share for part in self.parts)
        gaps = (len(self.parts) - 1) * ui.GUTTER
        span = (right - left if self.split == "lado" else bottom - top) - gaps
        boxes, cursor = [], left if self.split == "lado" else top
        for part in self.parts:
            size = span * part.share / total
            if self.split == "lado":
                boxes.append((round(cursor), top, round(cursor + size), bottom))
            else:
                boxes.append((left, round(cursor), right, round(cursor + size)))
            cursor += size + ui.GUTTER
        return boxes

    def live_parts(self, box: tuple) -> list[tuple[Part, tuple]]:
        if self.live is None:
            return []
        return [
            (part, part_box)
            for part, part_box in zip(self.parts, self.boxes(box))
            if part.page.live
        ]


@dataclass(frozen=True)
class Plan:
    screens: tuple[Screen, ...]
    data_every: dict[str, float]  # block id -> seconds between data refreshes
    settings: dict[
        str, dict
    ]  # block id -> its checked [blocos.<id>] values (defaults filled in)


def positive(value, where: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(
            f"{where}: precisa ser um número maior que zero, veio {value!r}"
        )
    return float(value)


def parse(text: str, blocks: dict[str, ui.Page]) -> Plan:
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"carrossel.toml não é um TOML válido: {err}") from err
    defaults = config.get("padrao", {})
    default_seconds = positive(defaults.get("na_tela", 45), "padrao.na_tela")
    default_redraw = positive(defaults.get("redesenho", 30), "padrao.redesenho")
    data_every = {}
    for block, seconds in config.get("dados", {}).items():
        if block not in blocks:
            raise ConfigError(
                f"dados.{block}: bloco desconhecido; existem: {', '.join(sorted(blocks))}"
            )
        data_every[block] = positive(seconds, f"dados.{block}")
    screens = []
    for number, raw in enumerate(config.get("tela", []), start=1):
        where = f"tela {number} ({raw.get('nome', 'sem nome')})"
        if not raw.get("nome"):
            raise ConfigError(f"{where}: falta o nome")
        split = raw.get("divisao", "lado")
        if split not in SPLITS:
            raise ConfigError(
                f"{where}: divisao precisa ser {' ou '.join(SPLITS)}, veio {split!r}"
            )
        parts = []
        for entry in raw.get("blocos", []):
            block = entry.get("bloco")
            if block not in blocks:
                raise ConfigError(
                    f"{where}: bloco {block!r} não existe; existem: {', '.join(sorted(blocks))}"
                )
            mode = entry.get("modo", "cheio")
            if mode not in MODES:
                raise ConfigError(
                    f"{where}: modo precisa ser {' ou '.join(MODES)}, veio {mode!r}"
                )
            parts.append(
                Part(
                    block,
                    blocks[block],
                    mode,
                    positive(entry.get("tamanho", 1), f"{where}: tamanho"),
                )
            )
        if not parts:
            raise ConfigError(f"{where}: precisa de pelo menos um bloco")
        live = raw.get("ao_vivo")
        screens.append(
            Screen(
                raw["nome"],
                positive(raw.get("na_tela", default_seconds), f"{where}: na_tela"),
                positive(raw.get("redesenho", default_redraw), f"{where}: redesenho"),
                None if live is None else positive(live, f"{where}: ao_vivo"),
                split,
                tuple(parts),
            )
        )
    if not screens:
        raise ConfigError("carrossel.toml não tem nenhuma [[tela]]")
    missing = sorted(
        {part.block for screen in screens for part in screen.parts} - set(data_every)
    )
    if missing:
        raise ConfigError(
            f"falta o intervalo de dados em [dados] para: {', '.join(missing)}"
        )
    return Plan(
        tuple(screens), data_every, block_settings(config.get("blocos", {}), blocks)
    )


def block_settings(raw, blocks: dict[str, ui.Page]) -> dict[str, dict]:
    if not isinstance(raw, dict):
        raise ConfigError("blocos: precisa ser um grupo [blocos.<nome>]")
    unknown = sorted(set(raw) - set(blocks))
    if unknown:
        raise ConfigError(
            f"blocos.{unknown[0]}: bloco desconhecido; existem: {', '.join(sorted(blocks))}"
        )
    try:
        return {
            block: fields.read_all(
                page.settings_fields, raw.get(block, {}), f"blocos.{block}"
            )
            for block, page in blocks.items()
            if page.settings_fields
        }
    except ValueError as err:
        raise ConfigError(str(err)) from err


def load(blocks: dict[str, ui.Page], path: Path = CONFIG) -> Plan:
    try:
        text = path.read_text()
    except OSError as err:
        raise ConfigError(f"não consegui ler {path}: {err}") from err
    return parse(text, blocks)


class Watcher:
    """Notices when carrossel.toml changes on disk (the settings page saves it), so the
    carousel picks up the new plan without a restart."""

    def __init__(self, blocks: dict[str, ui.Page], path: Path = CONFIG) -> None:
        self.blocks, self.path = blocks, path
        self.stamp = self._stamp()

    def _stamp(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def changed(self) -> Plan | None:
        """The new plan when the file changed since the last call, else None; a broken file
        raises ConfigError once and the caller keeps the plan it has."""
        stamp = self._stamp()
        if stamp is None or stamp == self.stamp:
            return None
        self.stamp = stamp
        return load(self.blocks, self.path)


def selftest() -> None:
    class Stub(ui.Page):
        def __init__(self, name: str, live: bool = False):
            self.name, self.live = name, live

    blocks = {
        "clima": Stub("Clima"),
        "pi": Stub("Pi", live=True),
        "impressora": Stub("Impressora"),
    }
    plan = parse(
        """
[padrao]
na_tela = 45
redesenho = 30
[dados]
clima = 900
pi = 1
impressora = 60
[[tela]]
nome = "Clima"
blocos = [{ bloco = "clima" }]
[[tela]]
nome = "Impressora e Pi"
na_tela = 60
ao_vivo = 1
divisao = "lado"
blocos = [{ bloco = "impressora", tamanho = 0.4 }, { bloco = "pi", modo = "compacto", tamanho = 0.6 }]
""",
        blocks,
    )
    first, second = plan.screens
    assert first.seconds == 45 and first.redraw == 30 and first.live is None
    assert (
        second.seconds == 60
        and second.parts[1].mode == "compacto"
        and plan.data_every["pi"] == 1
    )
    boxes = second.boxes((0, 0, 1012, 500))
    assert boxes == [(0, 0, 400, 500), (412, 0, 1012, 500)], boxes
    assert [part.block for part, _ in second.live_parts((0, 0, 1012, 500))] == ["pi"]
    stacked = Screen("x", 1, 1, None, "empilhado", second.parts)
    assert stacked.boxes((0, 0, 100, 512)) == [(0, 0, 100, 200), (0, 212, 100, 512)]
    for bad, message in (
        (
            '[[tela]]\nnome = "A"\nblocos = [{ bloco = "radio" }]',
            "bloco 'radio' não existe",
        ),
        (
            '[dados]\nclima = 0\n[[tela]]\nnome = "A"\nblocos = [{ bloco = "clima" }]',
            "dados.clima",
        ),
        (
            '[[tela]]\nnome = "A"\nblocos = [{ bloco = "clima" }]',
            "falta o intervalo de dados",
        ),
        (
            '[dados]\nclima = 9\n[[tela]]\nnome = "A"\ndivisao = "diagonal"\nblocos = [{ bloco = "clima" }]',
            "divisao",
        ),
        ("nome = ", "não é um TOML válido"),
        ("[dados]\nclima = 9", "nenhuma [[tela]]"),
    ):
        try:
            parse(bad, blocks)
        except ConfigError as err:
            assert message in str(err), (message, str(err))
        else:
            raise AssertionError(f"accepted a broken file: {bad!r}")
    blocks["clima"].settings_fields = (
        fields.Field("cidade", "Cidade", "texto", "Sorocaba"),
    )
    minimal = (
        '[dados]\nclima = 9\n[[tela]]\nnome = "A"\nblocos = [{ bloco = "clima" }]\n'
    )
    assert parse(minimal, blocks).settings == {"clima": {"cidade": "Sorocaba"}}, (
        "missing section: defaults"
    )
    chosen = parse(minimal + '[blocos.clima]\ncidade = "Jundiaí"\n', blocks)
    assert chosen.settings["clima"]["cidade"] == "Jundiaí"
    for bad, message in (
        ("[blocos.radio]\nx = 1\n", "blocos.radio: bloco desconhecido"),
        ('[blocos.clima]\ncidade = ""\n', "blocos.clima.cidade: precisa de um texto"),
        ('[blocos.clima]\ncor = "azul"\n', "campo desconhecido 'cor'"),
    ):
        try:
            parse(minimal + bad, blocks)
        except ConfigError as err:
            assert message in str(err), (message, str(err))
        else:
            raise AssertionError(f"accepted {bad!r}")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "carrossel.toml"
        good = (
            '[dados]\nclima = 9\n[[tela]]\nnome = "A"\nblocos = [{ bloco = "clima" }]\n'
        )
        path.write_text(good)
        watcher = Watcher(blocks, path)
        assert watcher.changed() is None, "nothing changed since it started"
        os.utime(path, ns=(1, 1))
        assert watcher.changed().screens[0].name == "A"
        assert watcher.changed() is None, "a change is reported once"
        path.write_text(good.replace("clima = 9", "clima = 0"))
        os.utime(path, ns=(2, 2))
        try:
            watcher.changed()
        except ConfigError as err:
            assert "dados.clima" in str(err)
        else:
            raise AssertionError("a broken file must be reported")
        assert watcher.changed() is None, (
            "the broken file is reported once, not on every check"
        )
    print("layout selftest ok")

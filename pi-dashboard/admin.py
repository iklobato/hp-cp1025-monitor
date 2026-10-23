"""Settings page for the carousel, on the home network only: http://<pi>:8026.

GET /api/config returns carrossel.toml as JSON plus the blocks that exist; POST /api/config
takes the same JSON back, writes it as TOML, checks it with the same rules the carousel uses
(layout.parse) and only then replaces the file, keeping the previous one as .bak. The
carousel notices the new file at its next screen switch (layout.Watcher).

Anyone outside a private address range gets 403: the Pi also has a public IPv6 address, and
the server listens on IPv4 only besides.
"""

import ipaddress
import json
import math
import os
import re
import tempfile
import threading
import tomllib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fields
import layout
import ui

PORT = 8026
PAGE = Path(__file__).with_name("admin.html")
MAX_BODY = (
    64 * 1024
)  # the whole file is ~2 KB; anything near this is not a settings form
HEADER = (
    "# Carrossel do monitor da Pi. Editado pela página http://<ip-da-pi>:8026 (só na rede de casa).\n"
    "# Tempos em segundos. modo = cheio ou compacto; divisao = lado ou empilhado;\n"
    "# tamanho = a fatia de cada bloco; ao_vivo = redesenho só dos blocos ao vivo.\n"
)
SCREEN_OPTIONAL = ("na_tela", "redesenho", "ao_vivo", "divisao")
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def is_local(address: str) -> bool:
    """Private ranges (home network, loopback, link-local); a public or CGNAT address is not."""
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


def toml_value(value, where: str) -> str:
    if isinstance(value, bool):
        raise layout.ConfigError(f"{where}: valor inválido {value!r}")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise layout.ConfigError(f"{where}: número inválido")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(
            value, ensure_ascii=False
        )  # JSON string escapes are valid TOML
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item, where) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{key_of(key, where)} = {toml_value(item, where)}"
                for key, item in value.items()
            )
            + " }"
        )
    raise layout.ConfigError(f"{where}: valor inválido {value!r}")


def key_of(key, where: str) -> str:
    if not isinstance(key, str) or not BARE_KEY.fullmatch(key):
        raise layout.ConfigError(f"{where}: nome de campo inválido {key!r}")
    return key


def table(data, where: str) -> dict:
    if not isinstance(data, dict):
        raise layout.ConfigError(f"{where}: precisa ser um grupo de campos")
    return data


def to_toml(config: dict) -> str:
    """carrossel.toml text for the page's JSON; only the keys the carousel reads are written."""
    config = table(config, "configuração")
    lines = [HEADER, "[padrao]"]
    defaults = table(config.get("padrao", {}), "padrao")
    lines += [
        f"{key} = {toml_value(defaults[key], f'padrao.{key}')}"
        for key in ("na_tela", "redesenho")
        if key in defaults
    ]
    lines += ["", "[dados]"]
    for block, seconds in table(config.get("dados", {}), "dados").items():
        if not BARE_KEY.fullmatch(block):
            raise layout.ConfigError(f"dados: nome de bloco inválido {block!r}")
        lines.append(f"{block} = {toml_value(seconds, f'dados.{block}')}")
    screens = config.get("tela", [])
    if not isinstance(screens, list):
        raise layout.ConfigError("tela: precisa ser uma lista de telas")
    for number, raw in enumerate(screens, start=1):
        screen, where = table(raw, f"tela {number}"), f"tela {number}"
        lines += [
            "",
            "[[tela]]",
            f"nome = {toml_value(screen.get('nome', ''), f'{where}: nome')}",
        ]
        lines += [
            f"{key} = {toml_value(screen[key], f'{where}: {key}')}"
            for key in SCREEN_OPTIONAL
            if screen.get(key) not in (None, "")
        ]
        parts = screen.get("blocos", [])
        if not isinstance(parts, list):
            raise layout.ConfigError(f"{where}: blocos precisa ser uma lista")
        entries = []
        for raw_part in parts:
            part = table(raw_part, f"{where}: bloco")
            fields = ", ".join(
                f"{key} = {toml_value(part[key], f'{where}: {key}')}"
                for key in ("bloco", "modo", "tamanho")
                if key in part
            )
            entries.append(f"{{ {fields} }}")
        lines.append(f"blocos = [{', '.join(entries)}]")
    for block, values in table(config.get("blocos", {}), "blocos").items():
        where = f"blocos.{block}"
        lines += ["", f"[blocos.{key_of(block, where)}]"]
        lines += [
            f"{key_of(key, where)} = {toml_value(value, f'{where}.{key}')}"
            for key, value in table(values, where).items()
        ]
    return "\n".join(lines) + "\n"


def save(config: dict, path: Path, blocks: dict[str, ui.Page]) -> layout.Plan:
    """Write the new file only if the carousel would accept it; the old one stays as .bak."""
    text = to_toml(config)
    plan = layout.parse(text, blocks)
    if path.exists():
        path.with_suffix(".toml.bak").write_text(path.read_text())
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".tmp", delete=False
    ) as temporary:
        temporary.write(text)
    # NamedTemporaryFile is 0600; keep the mode the file had (0644 for a new one).
    os.chmod(temporary.name, path.stat().st_mode & 0o777 if path.exists() else 0o644)
    Path(temporary.name).replace(path)
    return plan


def current(path: Path, blocks: dict[str, ui.Page]) -> dict:
    """The file as it is, plus every block's settings with defaults filled in, and how the
    page draws each field."""
    text = path.read_text()
    ordered = sorted(blocks.items(), key=lambda item: item[1].name)
    return {
        "config": tomllib.loads(text),
        "blocos": [
            {"id": block, "nome": page.name, "ao_vivo": page.live}
            for block, page in ordered
        ],
        "valores": layout.parse(text, blocks).settings,
        "parametros": {
            block: [field.describe() for field in page.settings_fields]
            for block, page in ordered
            if page.settings_fields
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "painel"

    def do_GET(self) -> None:
        if not self.allowed():
            return
        if self.path == "/":
            self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/favicon.ico":
            self.reply(204, b"", "image/x-icon")
        elif self.path == "/api/config":
            try:
                self.reply_json(
                    200, current(self.server.config_path, self.server.blocks)
                )
            except (OSError, tomllib.TOMLDecodeError, layout.ConfigError) as err:
                self.reply_json(
                    500, {"ok": False, "mensagem": f"não consegui ler o arquivo: {err}"}
                )
        else:
            self.reply_json(404, {"ok": False, "mensagem": "não existe"})

    def do_POST(self) -> None:
        if not self.allowed():
            return
        if self.path != "/api/config":
            self.reply_json(404, {"ok": False, "mensagem": "não existe"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.reply_json(
                413, {"ok": False, "mensagem": "configuração grande demais"}
            )
            return
        try:
            config = json.loads(self.rfile.read(length))
            with self.server.save_lock:
                plan = save(config, self.server.config_path, self.server.blocks)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.reply_json(
                400, {"ok": False, "mensagem": "a página mandou dados ilegíveis"}
            )
            return
        except layout.ConfigError as err:
            self.reply_json(400, {"ok": False, "mensagem": str(err)})
            return
        self.reply_json(
            200,
            {
                "ok": True,
                "mensagem": f"salvo: {len(plan.screens)} telas, valem a partir da próxima troca de tela",
            },
        )

    def allowed(self) -> bool:
        if is_local(self.client_address[0]):
            return True
        self.reply_json(403, {"ok": False, "mensagem": "só na rede de casa"})
        return False

    def reply_json(self, status: int, payload: dict) -> None:
        self.reply(
            status,
            json.dumps(payload, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
        )

    def reply(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        """Every page load in the journal would bury the real problems; saves are logged by run()."""


def serve(
    blocks: dict[str, ui.Page], path: Path, port: int, host: str = "0.0.0.0"
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.blocks, server.config_path, server.save_lock = blocks, path, threading.Lock()
    threading.Thread(target=server.serve_forever, daemon=True, name="admin").start()
    return server


def selftest() -> None:
    class Stub(ui.Page):
        def __init__(self, name: str, live: bool = False):
            self.name, self.live = name, live

    blocks = {"clima": Stub("Clima"), "pi": Stub("Raspberry Pi", live=True)}
    assert is_local("192.168.1.10") and is_local("127.0.0.1") and is_local("10.0.0.2")
    assert not is_local("8.8.8.8") and not is_local("100.64.0.1"), (
        "CGNAT is not the home network"
    )
    assert not is_local("2606:4700::1") and not is_local("lixo")
    blocks["clima"].settings_fields = (
        fields.Field("palavras", "Palavras", "palavras", ["Sorocaba"], low=0, high=5),
        fields.Field(
            "fontes",
            "Fontes",
            "tabela",
            [],
            low=0,
            high=5,
            columns=(fields.Column("nome", "Nome"), fields.Column("url", "URL", "url")),
        ),
    )
    config = {
        "padrao": {"na_tela": 45, "redesenho": 30},
        "dados": {"clima": 300, "pi": 1},
        "blocos": {
            "clima": {
                "palavras": ["São Paulo", 'aspas "x"'],
                "fontes": [{"nome": "g1", "url": "https://g1/rss"}],
            }
        },
        "tela": [
            {
                "nome": 'Tela "1"',
                "blocos": [{"bloco": "clima", "modo": "cheio", "tamanho": 1}],
            },
            {
                "nome": "Pi",
                "ao_vivo": 1,
                "divisao": "empilhado",
                "na_tela": "",
                "blocos": [
                    {"bloco": "pi", "modo": "compacto", "tamanho": 0.54},
                    {"bloco": "clima"},
                ],
            },
        ],
    }
    text = to_toml(config)
    assert tomllib.loads(text)["tela"][0]["nome"] == 'Tela "1"', (
        "quotes in a name survive"
    )
    assert "na_tela" not in tomllib.loads(text)["tela"][1], (
        "an empty field means: use the default"
    )
    plan = layout.parse(text, blocks)
    assert plan.settings["clima"] == config["blocos"]["clima"], (
        "lists and tables survive the round trip"
    )
    assert [s.name for s in plan.screens] == ['Tela "1"', "Pi"] and plan.screens[
        1
    ].parts[0].share == 0.54
    for bad, message in (
        ({"dados": {"a b": 1}}, "nome de bloco"),
        ({"tela": "x"}, "lista de telas"),
        ({"padrao": {"na_tela": True}}, "valor inválido"),
        ({"padrao": {"na_tela": float("inf")}}, "número inválido"),
        ({"blocos": {"clima": {"a b": 1}}}, "nome de campo inválido"),
        ({"blocos": {"clima": {"x": [None]}}}, "valor inválido"),
    ):
        try:
            to_toml(bad)
        except layout.ConfigError as err:
            assert message in str(err), (message, str(err))
        else:
            raise AssertionError(f"wrote a broken config: {bad}")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "carrossel.toml"
        path.write_text(text)
        path.chmod(0o644)
        server = serve(blocks, path, 0, host="127.0.0.1")
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(url: str, body: dict | None = None) -> tuple[int, dict]:
            data = None if body is None else json.dumps(body).encode()
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, data=data), timeout=5
                ) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        status, payload = call(f"{base}/api/config")
        assert status == 200 and payload["config"]["dados"]["pi"] == 1
        assert payload["valores"]["clima"]["palavras"] == ["São Paulo", 'aspas "x"']
        assert [f["chave"] for f in payload["parametros"]["clima"]] == [
            "palavras",
            "fontes",
        ]
        assert "pi" not in payload["parametros"], (
            "a block without settings has no section"
        )
        assert [b["id"] for b in payload["blocos"]] == ["clima", "pi"] and payload[
            "blocos"
        ][1]["ao_vivo"]
        broken = dict(config, dados={"clima": 0, "pi": 1})
        status, payload = call(f"{base}/api/config", broken)
        assert status == 400 and "dados.clima" in payload["mensagem"], payload
        assert (
            path.read_text() == text and not path.with_suffix(".toml.bak").exists()
        ), "a rejected save leaves the running file alone"
        changed = dict(config, padrao={"na_tela": 60, "redesenho": 30})
        status, payload = call(f"{base}/api/config", changed)
        assert status == 200 and payload["ok"], payload
        assert tomllib.loads(path.read_text())["padrao"]["na_tela"] == 60
        assert path.with_suffix(".toml.bak").read_text() == text, (
            "the previous file is kept"
        )
        assert path.stat().st_mode & 0o777 == 0o644, (
            "saving keeps the file readable as before"
        )
        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            assert b"<title>" in response.read()
        server.shutdown()
    print("admin selftest ok")


if __name__ == "__main__":
    selftest()

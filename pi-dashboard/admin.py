"""Settings page for the carousel, on the home network only: http://<pi>:8026.

GET /api/config returns carrossel.toml as JSON plus the blocks that exist; POST /api/config
takes the same JSON back, writes it as TOML, checks it with the same rules the carousel uses
(layout.parse) and only then replaces the file, keeping the previous one as .bak. The
carousel notices the new file at its next screen switch (layout.Watcher). A city whose name
changed gets its coordinates (and IBGE code) looked up before the check (sources.find_city).

Anyone outside a private address range gets 403: the Pi also has a public IPv6 address, and
the server listens on IPv4 only besides.
"""

import ipaddress
import json
import math
import os
import re
import socket
import tempfile
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fields
import layout
import sources
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
# A settings save is one ~2 KB request; a client that stops sending must not keep a thread.
REQUEST_TIMEOUT_SECONDS = 30
SCREEN_OPTIONAL = ("na_tela", "redesenho", "ao_vivo", "divisao")
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def known_host(host_header: str) -> bool:
    """The address typed in the browser: an IP, or this Pi's own name. A site of someone else's
    pointed at the Pi (DNS rebinding) arrives with its own name and is refused."""
    name = urllib.parse.urlsplit(f"//{host_header}").hostname or ""
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        # gethostname() is "impressora-pi" on the Pi but already ends in .local on a Mac.
        own = socket.gethostname().lower().removesuffix(".local")
        return name in {"localhost", own, f"{own}.local"}


def body_length(header: str | None) -> int | None:
    """Content-Length as a size, or None when it is not one (a negative one read until the
    client hung up)."""
    try:
        length = int(header or 0)
    except ValueError:
        return None
    return length if length >= 0 else None


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


def saved_settings(path: Path, blocks: dict[str, ui.Page]) -> dict:
    try:
        return layout.parse(path.read_text(), blocks).settings
    except (OSError, tomllib.TOMLDecodeError, layout.ConfigError):
        return {}


def locate(
    config: dict, path: Path, blocks: dict[str, ui.Page], find
) -> tuple[dict, list[str]]:
    """The config with coordinates for every city whose name changed, and the cities found."""
    settings = {
        block: dict(values) if isinstance(values, dict) else values
        for block, values in table(config.get("blocos", {}), "blocos").items()
    }
    before, found = saved_settings(path, blocks), []
    for block, page in blocks.items():
        values = settings.get(block)
        for city in page.cities:
            if not isinstance(values, dict) or not isinstance(
                values.get(city.name), str
            ):
                continue  # missing: the default; not text: layout.parse explains
            typed = values[city.name]
            unchanged = typed == before.get(block, {}).get(city.name)
            if unchanged and all(key in values for key in city.filled()):
                continue
            try:
                place = find(typed)
            except sources.SourceError as err:
                raise layout.ConfigError(f"blocos.{block}.{city.name}: {err}") from err
            values |= {
                city.name: place.name,
                city.latitude: place.latitude,
                city.longitude: place.longitude,
            }
            if city.ibge:
                values[city.ibge] = place.ibge
            found.append(f"{place.name} ({place.state})")
    return {**config, "blocos": settings}, found


def save(
    config: dict, path: Path, blocks: dict[str, ui.Page], find=sources.find_city
) -> tuple[layout.Plan, list[str]]:
    """Write the new file only if the carousel would accept it; the old one stays as .bak."""
    config, found = locate(table(config, "configuração"), path, blocks, find)
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
    return plan, found


def filled_keys(page: ui.Page) -> set[str]:
    return {key for city in page.cities for key in city.filled()}


def current(path: Path, blocks: dict[str, ui.Page]) -> dict:
    """The file as it is, plus every block's settings with defaults filled in, and how the
    page draws each field (fields filled from a city name are not drawn)."""
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
            block: [
                field.describe()
                for field in page.settings_fields
                if field.key not in filled_keys(page)
            ]
            for block, page in ordered
            if page.settings_fields
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "painel"
    timeout = REQUEST_TIMEOUT_SECONDS

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
        # A JSON body cannot be sent cross-site without a CORS preflight, which this server
        # never answers; a form from another page arrives as text/plain or form-encoded.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.reply_json(415, {"ok": False, "mensagem": "só aceito JSON"})
            return
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self.reply_json(403, {"ok": False, "mensagem": "pedido de outra página"})
            return
        length = body_length(self.headers.get("Content-Length"))
        if length is None:
            self.reply_json(400, {"ok": False, "mensagem": "tamanho inválido"})
            return
        if length > MAX_BODY:
            self.reply_json(
                413, {"ok": False, "mensagem": "configuração grande demais"}
            )
            return
        try:
            config = json.loads(self.rfile.read(length))
            with self.server.save_lock:
                plan, found = save(
                    config,
                    self.server.config_path,
                    self.server.blocks,
                    self.server.find_city,
                )
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.reply_json(
                400, {"ok": False, "mensagem": "a página mandou dados ilegíveis"}
            )
            return
        except layout.ConfigError as err:
            self.reply_json(400, {"ok": False, "mensagem": str(err)})
            return
        message = (
            f"salvo: {len(plan.screens)} telas, valem a partir da próxima troca de tela"
        )
        if found:
            message += f"; cidade achada: {', '.join(found)}"
        self.reply_json(200, {"ok": True, "mensagem": message})

    def allowed(self) -> bool:
        if not is_local(self.client_address[0]):
            self.reply_json(403, {"ok": False, "mensagem": "só na rede de casa"})
            return False
        if not known_host(self.headers.get("Host", "")):
            self.reply_json(403, {"ok": False, "mensagem": "endereço não reconhecido"})
            return False
        return True

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
    blocks: dict[str, ui.Page],
    path: Path,
    port: int,
    host: str = "0.0.0.0",
    find_city=sources.find_city,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.blocks, server.config_path, server.save_lock = blocks, path, threading.Lock()
    server.find_city = find_city
    threading.Thread(target=server.serve_forever, daemon=True, name="admin").start()
    return server


def selftest() -> None:
    class Stub(ui.Page):
        def __init__(self, name: str, live: bool = False):
            self.name, self.live = name, live

    blocks = {
        "clima": Stub("Clima"),
        "pi": Stub("Raspberry Pi", live=True),
        "tempo": Stub("Tempo"),
    }
    blocks["tempo"].settings_fields = (
        fields.Field("cidade", "Cidade", "texto", "Sorocaba"),
        fields.Field("lat", "Latitude", "decimal", -23.5, low=-90, high=90),
        fields.Field("lon", "Longitude", "decimal", -47.4, low=-180, high=180),
        fields.Field("ibge", "IBGE", "texto", "3552205"),
    )
    blocks["tempo"].cities = (fields.CityFields("cidade", "lat", "lon", "ibge"),)
    lookups = []

    def find_city(typed: str) -> sources.City:
        lookups.append(typed)
        if typed == "Atlântida":
            raise sources.SourceError("não achei a cidade 'Atlântida'")
        return sources.City("Sorocaba", "SP", "3552205", -23.5, -47.5)

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
        server = serve(blocks, path, 0, host="127.0.0.1", find_city=find_city)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(
            url: str, body: dict | None = None, headers: dict | None = None
        ) -> tuple[int, dict]:
            data = None if body is None else json.dumps(body).encode()
            sent = {"Content-Type": "application/json"} if data else {}
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(
                        url, data=data, headers=sent | (headers or {})
                    ),
                    timeout=5,
                ) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        before = path.read_text()
        for headers, code, why in (
            ({"Content-Type": "text/plain"}, 415, "a plain form from another site"),
            ({"Origin": "http://evil.example"}, 403, "a page on another origin"),
            ({"Host": "evil.example:8026"}, 403, "DNS rebinding: a foreign host name"),
        ):
            status, _ = call(f"{base}/api/config", config, headers)
            assert status == code, (why, status)
        assert call(f"{base}/api/config", None, {"Host": "evil.example"})[0] == 403, (
            "DNS rebinding cannot read the config either"
        )
        assert path.read_text() == before, "no refused request touched the file"
        assert (
            call(
                f"{base}/api/config",
                None,
                {"Host": f"{socket.gethostname().removesuffix('.local')}.local"},
            )[0]
            == 200
        ), "the Pi's own .local name is accepted"
        raw = socket.create_connection(("127.0.0.1", server.server_address[1]), 5)
        raw.sendall(
            b"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nContent-Length: -1\r\n\r\n{}"
        )
        assert raw.recv(64).startswith(b"HTTP/1.0 400"), (
            "a negative length is refused, not read until the client hangs up"
        )
        raw.close()

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
        assert [b["id"] for b in payload["blocos"]] == [
            "clima",
            "pi",
            "tempo",
        ] and payload["blocos"][1]["ao_vivo"]
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
        assert [
            f["chave"] for f in call(f"{base}/api/config")[1]["parametros"]["tempo"]
        ] == ["cidade"], (
            "coordinates and code come from the name, so the page does not ask for them"
        )
        moved = dict(
            changed,
            blocos={"tempo": {"cidade": "sorocaba", "lat": 1, "lon": 2, "ibge": "1"}},
        )
        status, payload = call(f"{base}/api/config", moved)
        assert (
            status == 200 and "cidade achada: Sorocaba (SP)" in payload["mensagem"]
        ), payload
        assert tomllib.loads(path.read_text())["blocos"]["tempo"] == {
            "cidade": "Sorocaba",
            "lat": -23.5,
            "lon": -47.5,
            "ibge": "3552205",
        }, "a new name brings its own coordinates and code"
        again = dict(
            changed,
            blocos={
                "tempo": {
                    "cidade": "Sorocaba",
                    "lat": -23.0,
                    "lon": -46.8,
                    "ibge": "3523404",
                }
            },
        )
        status, payload = call(f"{base}/api/config", again)
        assert (
            status == 200
            and lookups == ["sorocaba"]
            and "cidade achada" not in payload["mensagem"]
        ), "an unchanged name is not looked up again"
        saved = path.read_text()
        status, payload = call(
            f"{base}/api/config",
            dict(changed, blocos={"tempo": {"cidade": "Atlântida"}}),
        )
        assert (
            status == 400 and "blocos.tempo.cidade: não achei" in payload["mensagem"]
        ), payload
        assert path.read_text() == saved, (
            "a city not found leaves the running file alone"
        )
        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            assert b"<title>" in response.read()
        server.shutdown()
    print("admin selftest ok")


if __name__ == "__main__":
    selftest()

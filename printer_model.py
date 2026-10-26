"""Reads the HP LaserJet CP1025 over USB (LEDM + printer class) into a Report."""

import os
import re
import shlex
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum

import hpstatus
import macos_usb

QUEUE_NAME = "HP_CP1025"
OK_STATES = {"ok"}
CHIP_FAULT_STATES = {"missingMemory", "defectiveMemory"}
LABEL_TO_COLOR = {
    "K": "Black",
    "C": "Cyan",
    "M": "Magenta",
    "Y": "Yellow",
    "OPC": "OPC",
}
COLOR_NAMES_PT = {
    "Black": "Preto",
    "Cyan": "Ciano",
    "Magenta": "Magenta",
    "Yellow": "Amarelo",
    "OPC": "Tambor (OPC)",
}
# Every value below comes from the printer's own capability lists
# (/DevMgmt/ProductStatusCap.xml and /DevMgmt/ConsumableConfigCap.xml).
STATUS_CATEGORIES_PT = {
    "ready": "pronta",
    "processing": "processando",
    "initializing": "inicializando",
    "inPowerSave": "economia de energia",
    "calibrating": "calibrando",
    "coolDownMode": "resfriando",
    "rotatingCarousel": "girando carrossel",
    "cancelJob": "cancelando trabalho",
    "trayEmptyOrOpen": "bandeja vazia ou aberta",
    "closeDoorOrCover": "porta ou tampa aberta",
    "jamInPrinter": "papel atolado",
    "printerMispick": "falha ao puxar papel",
    "outputBinFull": "bandeja de saída cheia",
    "mediaPrompt": "aguardando papel",
    "manuallyFeed": "alimentação manual",
    "manualDuplex": "duplex manual",
    "misprint": "erro de impressão",
    "insufficientMemory": "memória insuficiente",
    "printerError": "erro da impressora",
    "cartridgeLow": "toner baixo",
    "cartridgeVeryLow": "toner muito baixo",
    "cartridgeMissing": "cartucho ausente",
    "replaceCartridgeOut": "cartucho vazio, trocar",
    "missingMemory": "chip do cartucho não lido",
    "defectiveMemory": "chip do cartucho com defeito",
    "incorrectCartridge": "cartucho incorreto",
    "incompatibleConsumable": "suprimento incompatível",
    "nonHPSupplyDetected": "suprimento não-HP",
    "usedConsumable": "suprimento usado/recondicionado",
    "tonerSealInPlace": "lacre do toner não removido",
    "unknown": "desconhecido",
}
# The printer labels these Error yet keeps printing (docs/FINDINGS.md, 2026-10-22), so they
# lower the verdict to a warning instead of a permanent red.
SUPPLY_ORIGIN_ALERTS = {
    "incompatibleConsumable",
    "nonHPSupplyDetected",
    "usedConsumable",
}
CONSUMABLE_STATES_PT = {
    "ok": "ok",
    "used": "usado/recondicionado",
    "unknown": "desconhecido",
    "missing": "ausente",
    "missingMemory": "chip não lido",
    "defectiveMemory": "chip com defeito",
    "misinstalled": "mal instalado",
    "geniuneHPUnsupported": "HP original não suportado",
}
# HP 10.100X family: supply memory/communication error; X = 0 black, 1 cyan, 2 magenta, 3 yellow.
SUPPLY_EVENT_PREFIX = "10.100"
SUPPLY_EVENT_COLORS = {"0": "Black", "1": "Cyan", "2": "Magenta", "3": "Yellow"}
LEDM_PATHS = {
    "status": "/DevMgmt/ProductStatusDyn.xml",
    "consumables": "/DevMgmt/ConsumableConfigDyn.xml",
    "usage": "/DevMgmt/ProductUsageDyn.xml",
    "config": "/DevMgmt/ProductConfigDyn.xml",
    "logs": "/DevMgmt/ProductLogsDyn.xml",
    "events": "/EventMgmt/EventTable.xml",
    "media": "/DevMgmt/MediaHandlingDyn.xml",
    "print": "/DevMgmt/PrintConfigDyn.xml",
}
DISCOVERY_PATH = "/DevMgmt/DiscoveryTree.xml"
# The resources outside LEDM_PATHS are mostly capability lists that never change; reading all
# 26 takes about 0.4 s, and while the USB device is held CUPS may not see the printer.
EXTRA_REFRESH = timedelta(minutes=10)


class Severity(IntEnum):
    """Overall verdict; the value is also the process exit code (0 ok, 1 warning, 2 error)."""

    OK = 0
    WARNING = 1
    ERROR = 2


def local(tag):
    return tag.rsplit("}", 1)[-1]


def strip_namespaces(root):
    for element in root.iter():
        element.tag = local(element.tag)
    return root


def parse(xml_text, expected_root):
    root = strip_namespaces(ET.fromstring(xml_text.strip()))
    if root.tag != expected_root:
        raise ValueError(f"expected <{expected_root}>, got <{root.tag}>")
    return root


def flatten(element, path=""):
    """Every attribute and text under element as (path, value); repeated siblings get [n]."""
    for name, value in element.attrib.items():
        yield f"{path} @{local(name)}".strip(), value
    value = (element.text or "").strip()
    if value:
        yield path or element.tag, value
    repeats = Counter(child.tag for child in element)
    seen = Counter()
    for child in element:
        seen[child.tag] += 1
        name = (
            child.tag if repeats[child.tag] == 1 else f"{child.tag}[{seen[child.tag]}]"
        )
        yield from flatten(child, f"{path}/{name}" if path else name)


@dataclass
class LedmResource:
    """One LEDM document as the advanced section shows it: every field, flattened."""

    read_at: datetime
    status: str
    rows: list = field(default_factory=list)


def read_resource(ledm, path, read_at):
    try:
        response = ledm.fetch(path)
    except hpstatus.UsbError as error:
        return LedmResource(read_at, f"erro USB: {error}")
    if response.status.split(" ")[1:2] != ["200"]:
        return LedmResource(read_at, response.status)
    try:
        root = strip_namespaces(ET.fromstring(response.body.strip()))
    except ET.ParseError as error:
        return LedmResource(read_at, f"XML inválido: {error}")
    return LedmResource(read_at, response.status, list(flatten(root)))


def text(element, path, default=""):
    found = element.find(path) if element is not None else None
    return found.text.strip() if found is not None and found.text else default


def as_int(value):
    return int(value) if value.lstrip("-").isdigit() else None


def capacity_impressions(max_capacity, unit):
    """'6' + '100ImpressionsAt5PercentCoverage' -> 600; '10KImpressions...' scales by 1000."""
    match = re.match(r"(\d+)(K?)Impressions", unit)
    count = as_int(max_capacity)
    if not match or not count:
        return None
    scale = int(match.group(1)) * (1000 if match.group(2) else 1)
    return count * scale


def years_since(iso_date, today):
    try:
        then = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return round((today - then).days / 365.25, 1)


@dataclass
class Consumable:
    color: str
    product_number: str = ""
    family: str = ""
    kind: str = ""
    state: str = ""
    state_action: str = ""
    quantity_state: str = ""
    percent: int | None = None
    raw_percent: int | None = None
    rated_yield: int | None = None
    pages_remaining: int | None = None
    impressions: int | None = None
    capacity: int | None = None
    brand: str = ""
    serial: str = ""
    installed: str = ""
    last_used: str = ""
    manufactured: str = ""
    chip_tag: str = ""
    model_info: str = ""
    previous: dict = field(default_factory=dict)

    @property
    def name(self):
        return COLOR_NAMES_PT[self.color]

    @property
    def chip_readable(self):
        return self.chip_tag not in ("", "0000/0000")

    @property
    def chip_ok(self):
        """The printer's own verdict wins: a tag can be present while it reports the chip unreadable."""
        if self.state in CHIP_FAULT_STATES:
            return False
        return self.chip_readable or self.state in OK_STATES

    @property
    def is_toner(self):
        return self.kind != "imageDrum"

    def needs_attention(self, warn_percent):
        return self.state not in OK_STATES or (
            self.percent is not None and self.percent <= warn_percent
        )

    def advice(self, warn_percent):
        if self.state == "missingMemory":
            return "chip não lido: limpe os contatos e reencaixe"
        if self.state in {"misinstalled", "incorrect"}:
            return "mal encaixado: tire e reencaixe"
        if self.state not in OK_STATES:
            return f"{CONSUMABLE_STATES_PT.get(self.state, self.state)}: verifique ou troque"
        if self.percent is not None and self.percent <= warn_percent:
            return f"{self.percent}%: compre {self.product_number}"
        return ""


@dataclass
class Report:
    device_id: str
    port: dict
    queue: str
    read_at: datetime
    usb: dict = field(default_factory=dict)
    endpoints: list = field(default_factory=list)
    topology: dict = field(default_factory=dict)
    latency_ms: dict = field(default_factory=dict)
    wire_bytes: dict = field(default_factory=dict)
    revisions: dict = field(default_factory=dict)
    http_server: str = ""
    identity: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    status_category: str = ""
    alerts: list = field(default_factory=list)
    carousel: str = ""
    low_threshold: str = ""
    very_low_action: str = ""
    consumables: list = field(default_factory=list)
    counters: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    device_events: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    resources: dict = field(default_factory=dict)

    @property
    def queue_paused(self):
        return "disabled" in self.queue

    def changes_since(self, previous):
        """Human-readable differences between two readings, for the live feed."""
        changes = []
        if previous.status_category != self.status_category:
            changes.append(
                f"estado: {STATUS_CATEGORIES_PT.get(previous.status_category, previous.status_category)}"
                f" → {STATUS_CATEGORIES_PT.get(self.status_category, self.status_category)}"
            )
        old_alerts = {a[1] for a in previous.alerts}
        new_alerts = {a[1] for a in self.alerts}
        changes += [
            f"novo alerta: {STATUS_CATEGORIES_PT.get(a, a)}"
            for a in sorted(new_alerts - old_alerts)
        ]
        changes += [
            f"alerta resolvido: {STATUS_CATEGORIES_PT.get(a, a)}"
            for a in sorted(old_alerts - new_alerts)
        ]
        old_total, new_total = (
            previous.counters.get("total"),
            self.counters.get("total"),
        )
        if old_total is not None and new_total is not None and new_total != old_total:
            changes.append(
                f"+{new_total - old_total} página(s) impressa(s), total {new_total}"
            )
        before = {c.color: c for c in previous.consumables}
        for item in self.consumables:
            old = before.get(item.color)
            if old is None:
                continue
            if old.state != item.state:
                changes.append(
                    f"{item.name}: {CONSUMABLE_STATES_PT.get(old.state, old.state)}"
                    f" → {CONSUMABLE_STATES_PT.get(item.state, item.state)}"
                )
            if old.percent != item.percent:
                changes.append(f"{item.name}: nível {old.percent}% → {item.percent}%")
        port_words = {
            "paper_empty": "sem papel",
            "error": "erro na porta USB",
            "selected": "USB online",
        }
        for key, word in port_words.items():
            if previous.port.get(key) != self.port.get(key):
                changes.append(f"{word}: {'sim' if self.port.get(key) else 'não'}")
        if previous.queue_paused != self.queue_paused:
            changes.append(
                "fila do Mac " + ("pausada" if self.queue_paused else "reativada")
            )
        return changes

    def severity(self, warn_percent):
        if (
            self.errors
            or self.port.get("error")
            or any(
                a[0] == "Error" and a[1] not in SUPPLY_ORIGIN_ALERTS
                for a in self.alerts
            )
        ):
            return Severity.ERROR
        if any(c.state not in OK_STATES for c in self.consumables):
            return Severity.ERROR
        if self.alerts or any(
            c.needs_attention(warn_percent) for c in self.consumables
        ):
            return Severity.WARNING
        return Severity.OK


def read_consumables(config_root, usage_root):
    by_color = {}
    for info in config_root.iter("ConsumableInfo"):
        color = LABEL_TO_COLOR.get(text(info, "ConsumableLabelCode"))
        if color is None:
            continue
        by_color[color] = Consumable(
            color=color,
            product_number=text(info, "ProductNumber"),
            family=text(info, "ConsumableFamilyName"),
            kind=text(info, "ConsumableTypeEnum"),
            state=text(info, "ConsumableLifeState/ConsumableState"),
            state_action=text(info, "ConsumableLifeState/ConsumableStateAction"),
            quantity_state=text(info, "ConsumableLifeState/MeasuredQuantityState"),
            percent=as_int(text(info, "ConsumablePercentageLevelRemaining")),
            rated_yield=as_int(text(info, "ConsumableEstimatedPages/ValueInt")) or None,
            capacity=capacity_impressions(
                text(info, "Capacity/MaxCapacity"), text(info, "Capacity/Unit")
            ),
            brand=text(info, "ConsumableLifeState/Brand")
            or text(info, "Manufacturer/Name"),
            serial=text(info, "SerialNumber"),
            installed=text(info, "Installation/Date"),
            last_used=text(info, "ConsumableLastUsedDate"),
            manufactured=text(info, "Manufacturer/Date"),
            chip_tag=f"{text(info, 'CartridgeMemoryTagData1')}/"
            f"{text(info, 'CartridgeMemoryTagData2')}".strip("/"),
            model_info=text(info, "CartridgeModelInformation"),
        )
    if usage_root is not None:
        for usage in usage_root.iter("Consumable"):
            color = text(usage, "MarkerColor") or "OPC"
            item = by_color.get(color)
            if item is None:
                continue
            item.impressions = as_int(text(usage, "TotalImpressions"))
            item.pages_remaining = as_int(text(usage, "EstimatedPagesRemaining"))
            item.raw_percent = as_int(
                text(usage, "ConsumableRawPercentageLevelRemaining")
            )
            previous = usage.find("PreviousCartridgeData")
            if previous is not None:
                item.previous = {
                    "impressions": text(previous, "TotalImpressions"),
                    "installed": text(previous, "Installation/Date"),
                    "last_used": text(previous, "ConsumableLastUsedDate"),
                }
    return list(by_color.values())


def read_identity(config_root):
    info = config_root.find("ProductInformation")
    boards = {
        text(b, "BoardTypeEnum"): text(b, "ModelNumber")
        for b in config_root.iter("BoardInfo")
    }
    return {
        "Modelo": text(info, "MakeAndModel"),
        "Código do produto": text(info, "ProductNumber"),
        "Serial": text(info, "SerialNumber"),
        "UUID": text(info, "UUID"),
        "Service ID": text(info, "ServiceID"),
        "Firmware (data)": text(info, "Version/Date"),
        "Placa do sistema": boards.get("systemControl", ""),
        "Placa do motor": boards.get("markingEngineControl", ""),
        "Memória livre": f"{text(config_root, 'Memory/AvailableMemory')} de "
        f"{text(config_root, 'Memory/TotalMemory')} MB",
        "Senha do painel": text(info, "PasswordStatus"),
    }


def read_settings(config_root, media_root, print_root):
    settings = config_root.find("ProductSettings")
    result = {
        "País": text(settings, "CountryAndRegionName"),
        "Idioma": text(settings, "ProductLanguage/DeviceLanguage"),
        "Dorme após": text(settings, "AutoOffAutoOn/AutoOffDelay/AutoOffDelayTime"),
        "Desliga sozinha após": text(settings, "AutoOffTime"),
        "Calibração ao ligar": text(settings, "PowerOnCalibration"),
        "Calibração periódica": text(settings, "EngineCalibrationTimer"),
        "Duplex": text(settings, "Duplex"),
        "Smart Install": text(settings, "SmartInstall"),
    }
    if media_root is not None:
        info = media_root.find("MediaHandlingInfo")
        result |= {
            "Bandejas entrada/saída": f"{text(info, 'NumOfInputTrays')}/"
            f"{text(info, 'NumOfOutputBins')}",
            "Tipo de papel": text(media_root, "InputTray/MediaType"),
            "Sem papel": f"{text(info, 'MediaOutActionType')} "
            f"({text(info, 'MediaOutTime')} s)",
        }
    if print_root is not None:
        result |= {
            "Resolução": text(print_root, "ResolutionSetting"),
            "Bits por pixel": text(print_root, "ColorBitsPerPixel"),
            "Auto-continuar": text(print_root, "AutoContinue"),
        }
    return result


def read_counters(usage_root):
    unit = usage_root.find("PrinterSubunit")
    return {
        "total": as_int(text(unit, "TotalImpressions")),
        "mono": as_int(text(unit, "MonochromeImpressions")),
        "color": as_int(text(unit, "ColorImpressions")),
        "jams": as_int(text(unit, "JamEvents")),
        "mispicks": as_int(text(unit, "MispickEvents")),
    }


def event_hint(code):
    if code.startswith(SUPPLY_EVENT_PREFIX) and code[-1] in SUPPLY_EVENT_COLORS:
        color = COLOR_NAMES_PT[SUPPLY_EVENT_COLORS[code[-1]]]
        return f"falha no chip do cartucho {color} (provável)"
    return "sem descrição conhecida"


def read_events(logs_root):
    events = [
        (
            as_int(text(e, "SequenceNumber")) or 0,
            text(e, "EventCode", "-"),
            text(e, "TotalImpressions", "-"),
        )
        for e in logs_root.iter("Event")
    ]
    return sorted(events, reverse=True)


def read_device_events(table_root):
    return [
        (text(e, "UnqualifiedEventCategory"), text(e, "AgingStamp"))
        for e in table_root.iter("Event")
    ]


def read_alerts(status_root):
    alerts = []
    for alert in status_root.iter("Alert"):
        colors = [c.text for c in alert.iter("AlertDetailsMarkerColor") if c.text]
        alerts.append(
            (
                text(alert, "Severity"),
                text(alert, "ProductStatusAlertID"),
                ", ".join(COLOR_NAMES_PT.get(c, c) for c in colors),
            )
        )
    return alerts


def cups_queue_state():
    # os.system with shell redirection instead of subprocess (house rule).
    with tempfile.NamedTemporaryFile("r", suffix=".txt") as out:
        command = f"lpstat -p {shlex.quote(QUEUE_NAME)} > {shlex.quote(out.name)} 2>&1"
        os.system(command)
        return " ".join(out.read().split()) or "sem resposta do lpstat"


def apply_roots(report, roots):
    """Fill a Report from the parsed LEDM documents that could be read."""
    if "config" in roots:
        report.identity = read_identity(roots["config"])
        report.settings = read_settings(
            roots["config"], roots.get("media"), roots.get("print")
        )
    if "status" in roots:
        report.status_category = text(roots["status"], "Status/StatusCategory")
        report.alerts = read_alerts(roots["status"])
    if "consumables" in roots:
        root = roots["consumables"]
        report.consumables = read_consumables(root, roots.get("usage"))
        report.carousel = text(root, "ProductConsumableInfo/CarouselPosition")
        report.low_threshold = text(
            root, "ProductConsumableInfo/ConsumableLowThreshold"
        )
        report.very_low_action = text(
            root,
            "ProductConsumableInfo/AtVeryLowSettings/ConsumableSetting/ConsumableStateAction",
        )
    if "usage" in roots:
        report.counters = read_counters(roots["usage"])
    if "logs" in roots:
        report.events = read_events(roots["logs"])
    if "events" in roots:
        report.device_events = read_device_events(roots["events"])


class PrinterReader:
    """One full reading of the printer. Every I/O source is injected so tests need no hardware."""

    def __init__(
        self,
        connect=hpstatus.UsbPrinter.open,
        topology=macos_usb.usb_topology,
        queue_state=cups_queue_state,
        clock=datetime.now,
        extra_refresh=EXTRA_REFRESH,
    ):
        self._connect = connect
        self._topology = topology
        self._queue_state = queue_state
        self._clock = clock
        self._extra_refresh = extra_refresh
        self._extra = {}
        self._extra_read_at = None

    def read(self):
        with self._connect() as usb:
            report = Report(
                device_id=usb.device_id(),
                port=usb.port_status(),
                queue=self._queue_state(),
                read_at=self._clock(),
                usb=usb.link_info(),
                endpoints=usb.endpoint_map(),
                topology=self._topology(),
            )
            ledm = hpstatus.LedmClient(usb)
            roots = self._fetch_roots(ledm, report)
            if self._extra_due(report.read_at):
                self._refresh_extra(ledm, report.read_at)
        apply_roots(report, roots)
        live = {
            LEDM_PATHS[key]: LedmResource(
                report.read_at, "HTTP/1.1 200 OK", list(flatten(root))
            )
            for key, root in roots.items()
        }
        report.resources = live | self._extra
        return report

    def _extra_due(self, now):
        return (
            self._extra_read_at is None
            or now - self._extra_read_at >= self._extra_refresh
        )

    def _refresh_extra(self, ledm, now):
        """Every resource the printer lists that a normal reading skips. Their failures stay
        in their own entry, not in report.errors, so they never change the verdict."""
        tree = read_resource(ledm, DISCOVERY_PATH, now)
        if not tree.rows:
            return  # keep the last copy; retried on the next reading
        extra = {DISCOVERY_PATH: tree}
        core = set(LEDM_PATHS.values())
        for name, uri in tree.rows:
            listed = name.split("/")[-1].split("[")[0] == "ResourceURI"
            if listed and uri not in extra and uri not in core:
                extra[uri] = read_resource(ledm, uri, now)
        self._extra, self._extra_read_at = extra, now

    @staticmethod
    def _fetch_roots(ledm, report):
        roots = {}
        for key, path in LEDM_PATHS.items():
            expected = path.rsplit("/", 1)[-1].removesuffix(".xml")
            started = time.monotonic()
            try:
                response = ledm.fetch(path)
                roots[key] = parse(response.body, expected)
                report.wire_bytes[path] = response.wire_bytes
                report.http_server = response.server or report.http_server
                report.revisions[expected] = text(roots[key], "Version/Revision")
            except (hpstatus.UsbError, ValueError, ET.ParseError) as error:
                report.errors.append(f"{path}: {error}")
            report.latency_ms[path] = round((time.monotonic() - started) * 1000)
        return roots

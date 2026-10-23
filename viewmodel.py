"""Everything a person reads, as plain data: Portuguese wording, formatting and the rows each
panel shows. Shared by the rich terminal (dashboard.py) and the web page (web_dashboard.py),
so both always say the same thing. No rendering library is imported here.
"""

import re
from collections import Counter
from datetime import datetime

from history import level_series
from printer_model import (
    CONSUMABLE_STATES_PT,
    OK_STATES,
    QUEUE_NAME,
    Severity,
    STATUS_CATEGORIES_PT,
    event_hint,
    years_since,
)

SEVERITY_LABELS = {
    Severity.OK: "✔ PRONTA",
    Severity.WARNING: "▲ ATENÇÃO",
    Severity.ERROR: "✖ COM PROBLEMAS",
}
VALUE_WORDS = {
    "brazil": "Brasil",
    "en": "inglês",
    "notSet": "sem senha",
    "enabled": "ligado",
    "disabled": "desligado",
    "on": "ligado",
    "off": "desligado",
    "plain": "papel comum",
    "waitForPaperToBeLoaded": "espera colocar papel",
    "imageREt2400": "ImageREt 2400",
    "acknowledge": "só avisa",
    "stop": "para de imprimir",
    "continue": "continua",
    "True": "sim",
    "False": "não",
}
TIME_UNITS = {
    "minutes": "min",
    "minute": "min",
    "hours": "h",
    "hour": "h",
    "seconds": "s",
}
CUPS_DATE = "%a %b %d %H:%M:%S %Y"
INTERFACE_CLASSES = {
    (7, 1, 1): "impressora 1-via",
    (7, 1, 2): "impressora 2-vias",
    (7, 1, 4): "IPP-over-USB",
    (255, 4, 1): "HP LEDM",
}
ENDPOINT_ROLES = {
    ((7, 1, 2), "OUT", "bulk"): "dados ZJS",
    ((7, 1, 2), "IN", "bulk"): "status de impressão",
    ((255, 4, 1), "OUT", "bulk"): "pedido HTTP",
    ((255, 4, 1), "IN", "bulk"): "resposta HTTP",
    ((255, 4, 1), "IN", "interrupt"): "eventos",
}
LEDM_SHORT = {
    "ProductStatusDyn": "status",
    "ConsumableConfigDyn": "suprim.",
    "ProductUsageDyn": "uso",
    "ProductConfigDyn": "config",
    "ProductLogsDyn": "logs",
    "EventTable": "eventos",
    "MediaHandlingDyn": "mídia",
    "PrintConfigDyn": "impressão",
}
WEB_TREND_POINTS = 30


def fmt(value, suffix=""):
    return "–" if value in (None, "") else f"{value}{suffix}"


def number(value):
    return "–" if value is None else f"{value:,}".replace(",", ".")


def humanize(value):
    """Turn firmware tokens like '15_minutes' or 'notSet' into plain Portuguese."""
    raw = str(value)
    if raw in VALUE_WORDS:
        return VALUE_WORDS[raw]
    match = re.fullmatch(r"(\d+)_?([a-z]+)", raw)
    if match and match.group(2) in TIME_UNITS:
        return f"{match.group(1)} {TIME_UNITS[match.group(2)]}"
    words = [VALUE_WORDS.get(part, part) for part in raw.split(" ")]
    return " ".join(words)


def short_date(iso_date, pattern="%d/%m/%Y"):
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime(pattern)
    except ValueError:
        return fmt(iso_date)


def humanize_queue(lpstat_line):
    """'printer X disabled since <date> - <reason>' -> 'pausada desde 18/10 21:06 · <reason>'."""
    match = re.search(r"disabled since (.+?) -\s*(.*)$", lpstat_line)
    if match:
        try:
            since = datetime.strptime(match.group(1).strip(), CUPS_DATE).strftime(
                "%d/%m %H:%M"
            )
        except ValueError:
            since = match.group(1).strip()
        reason = match.group(2).strip() or "sem motivo informado"
        return f"pausada desde {since} · motivo: {reason}"
    if "is idle" in lpstat_line:
        return "ativa, ociosa"
    if "now printing" in lpstat_line:
        return "ativa, imprimindo"
    return lpstat_line


def translate_state(state):
    return CONSUMABLE_STATES_PT.get(state, state or "–")


def translate_category(category):
    return STATUS_CATEGORIES_PT.get(category, category or "–")


def whole_years(years):
    return None if years is None else int(years)


def as_int_or_none(value):
    return int(value) if str(value).isdigit() else None


def port_bits(raw):
    """IEEE 1284 status byte: bit5 paper empty, bit4 selected, bit3 nERROR (1 = no error)."""
    value = int(raw, 16) if raw else 0
    flags = [
        "online" if value & 0x10 else "offline",
        "sem erro" if value & 0x08 else "ERRO",
        "SEM PAPEL" if value & 0x20 else "com papel",
    ]
    return f"{raw} = {value >> 4:04b} {value & 0xF:04b} → " + ", ".join(flags)


def port_badges(report):
    """(label, ok) pairs for the status badges, in display order."""
    port = report.port
    return [
        ("USB online" if port["selected"] else "USB offline", port["selected"]),
        (
            "sem erro na porta" if not port["error"] else "erro na porta",
            not port["error"],
        ),
        (
            "papel ok" if not port["paper_empty"] else "sem papel",
            not port["paper_empty"],
        ),
        (
            "fila pausada" if report.queue_paused else "fila ativa",
            not report.queue_paused,
        ),
    ]


def status_rows(report):
    return {
        "Estado": translate_category(report.status_category),
        "Carrossel": f"parado no cartucho {fmt(report.carousel)}",
        "Fila CUPS": humanize_queue(report.queue),
    }


def pace_text(prediction):
    if not prediction.pages_per_day:
        return ""
    return (
        f"{prediction.pages_per_day:.1f} pág./dia ({number(prediction.window_pages)} em "
        f"{prediction.window_days:.0f} dias)"
    )


def counter_rows(report):
    c = report.counters
    return {
        "Páginas no total": number(c.get("total")),
        "Preto e branco": number(c.get("mono")),
        "Coloridas": number(c.get("color")),
        "Atolamentos": number(c.get("jams")),
        "Falhas ao puxar papel": number(c.get("mispicks")),
    }


def color_percent(counters):
    total = (counters.get("mono") or 0) + (counters.get("color") or 0)
    return round(100 * (counters.get("color") or 0) / total) if total else None


def short_hop(description):
    name = description.split(" (")[0]
    return "raiz XHCI" if "XHCI" in name else name


def usb_bus_rows(report):
    usb, topo = report.usb, report.topology
    hops = topo.get("chain", [])
    chain = " ▸ ".join(short_hop(hop) for hop in hops) or "–"
    available = topo.get("bus_power_available_ma")
    return {
        "Caminho": f"Mac ▸ {chain} ▸ porta {fmt(topo.get('port'))}",
        "Hub": hops[-1] if hops else "–",
        "Endereço": f"{fmt(topo.get('address'))} · location {fmt(topo.get('location'))}",
        "Velocidade": f"{fmt(usb.get('speed'))} · USB {fmt(usb.get('usb_version'))}",
        "IDs": f"{fmt(usb.get('vendor_product'))} · bcdDevice {fmt(usb.get('device_release'))}",
        "Serial USB": fmt(usb.get("serial")),
        "Energia": f"pede {fmt(usb.get('max_power_ma'))} de {fmt(available)} mA · "
        f"fonte própria: {humanize(usb.get('self_powered'))}",
        "Power state": f"{fmt(topo.get('power_state'))} (macOS) · "
        f"remote wakeup: {humanize(usb.get('remote_wakeup'))}",
        "Configuração": f"{fmt(usb.get('configuration'))} · {fmt(usb.get('interfaces'))} interfaces · "
        f"{fmt(usb.get('total_descriptor_bytes'))} B · EP0 {fmt(usb.get('ep0_max_packet'))} B",
        "Status 1284": port_bits(report.port.get("raw")),
    }


def endpoint_rows(report):
    rows = []
    for iface in report.endpoints:
        klass = tuple(iface["class"])
        for ep in iface["endpoints"]:
            rows.append(
                {
                    "If": str(iface["number"]),
                    "Classe": INTERFACE_CLASSES.get(klass, "/".join(map(str, klass))),
                    "EP": f"0x{ep['address']:02x}",
                    "Dir": ep["direction"],
                    "Tipo": ep["type"],
                    "Pacote": f"{ep['max_packet']} B",
                    "Interv.": str(ep["interval"]) if ep["interval"] else "–",
                    "Uso": ENDPOINT_ROLES.get((klass, ep["direction"], ep["type"]), ""),
                }
            )
    return rows


def device_id_field(device_id, key):
    for part in device_id.split(";"):
        name, _, value = part.partition(":")
        if name.strip() == key:
            return value.strip()
    return ""


def revision_summary(revisions):
    """'29 (5 recursos) · suprim. 38 · uso 66 · config 77': the shared revision once, outliers by name."""
    numbers = {name: rev.removeprefix("SVN.") for name, rev in revisions.items()}
    if not numbers:
        return ""
    common, count = Counter(numbers.values()).most_common(1)[0]
    outliers = [
        f"{LEDM_SHORT.get(name, name)} {rev}"
        for name, rev in numbers.items()
        if rev != common
    ]
    return " · ".join(
        [f"{common} ({count} recurso{'s' if count > 1 else ''})", *outliers]
    )


def controller_rows(report):
    latencies = list(report.latency_ms.values())
    total_bytes = sum(report.wire_bytes.values())
    total_ms = sum(latencies) or 1
    identity = report.identity
    languages = device_id_field(report.device_id, "CMD").replace(",", " ")
    return {
        "Controlador": f"{fmt(report.http_server)} (servidor LEDM; Mrvl = Marvell)",
        "Placas": f"sistema {fmt(identity.get('Placa do sistema'))} · motor {fmt(identity.get('Placa do motor'))}",
        "Memória": f"{fmt(identity.get('Memória livre'))} livres",
        "Firmware": f"{short_date(identity.get('Firmware (data)', ''))} · "
        f"FWVER {fmt(device_id_field(report.device_id, 'FWVER'))}",
        "Linguagens": fmt(languages),
        "LEDM em": f"USB {fmt(device_id_field(report.device_id, 'LEDMDIS'))} (classe#sub#proto)",
        "IEEE 1284": f"{fmt(device_id_field(report.device_id, 'CLS'))} · {fmt(device_id_field(report.device_id, 'MFG'))}",
        "Rev. LEDM": fmt(revision_summary(report.revisions)),
        "Tráfego LEDM": f"{len(latencies)} pedidos · {total_bytes / 1024:.1f} KB · {total_bytes / total_ms:.0f} KB/s",
        "Latência": f"média {sum(latencies) / max(len(latencies), 1):.0f} ms · pior {max(latencies, default=0)} ms",
        "Eventos": " · ".join(f"{cat} ({stamp})" for cat, stamp in report.device_events)
        or "–",
    }


def humanized_rows(rows):
    return {key: humanize(fmt(value)) for key, value in rows.items()}


def action_items(report, warn_percent):
    """What the user should do now, as (subject, advice, consumable color or None)."""
    items = [
        (c.name, c.advice(warn_percent), c.color)
        for c in report.consumables
        if c.needs_attention(warn_percent)
    ]
    if report.port["paper_empty"]:
        items.append(("Papel", "coloque papel na bandeja", None))
    if report.port["error"]:
        items.append(("Porta USB", "reporta erro: desligue e ligue a impressora", None))
    if report.queue_paused:
        items.append(("Fila pausada", f"depois rode cupsenable {QUEUE_NAME}", None))
    items += [("Falha de leitura", error, None) for error in report.errors]
    return items


def next_to_run_out(report, prediction):
    toners = sorted(
        (c for c in report.consumables if c.is_toner and c.pages_remaining),
        key=lambda c: c.pages_remaining,
    )
    if not toners:
        return ""
    nxt = toners[0]
    when = prediction.empty_date(nxt, report.read_at)
    return (
        f"Próximo a acabar: {nxt.name} ≈{number(nxt.pages_remaining)} pág. ({nxt.product_number})"
        + (f", em {when:%d/%m/%Y}" if when else "")
    )


def previous_cartridge_notes(report):
    return [
        f"↳ {c.name} anterior: {number(as_int_or_none(c.previous['impressions']))} pág. · "
        f"instalado {short_date(c.previous['installed'])} · último uso {short_date(c.previous['last_used'])}"
        for c in report.consumables
        if c.previous.get("impressions") not in (None, "", "0")
    ]


def consumable_view(c, report, warn_percent, entries, prediction):
    empty = prediction.empty_date(c, report.read_at)
    age = whole_years(years_since(c.manufactured, report.read_at.date()))
    return {
        "color": c.color,
        "name": c.name,
        "code": c.product_number,
        "state": c.state,
        "state_text": translate_state(c.state),
        "ok": c.state in OK_STATES,
        "low": c.state in OK_STATES and c.needs_attention(warn_percent),
        "percent": c.percent,
        "raw_text": fmt(c.raw_percent, "%"),
        "pages_text": number(c.pages_remaining),
        "impressions_text": number(c.impressions),
        "rated_yield_text": number(c.rated_yield or c.capacity),
        "brand": fmt(c.brand),
        "serial": fmt(c.serial),
        "manufactured": short_date(c.manufactured, "%m/%Y"),
        "age_text": fmt(age, " anos"),
        "installed": short_date(c.installed),
        "last_used": short_date(c.last_used),
        "chip": c.chip_tag or "–",
        "chip_ok": c.chip_ok,
        "model": fmt(c.model_info),
        "advice": c.advice(warn_percent),
        "trend": level_series(entries, c.color)[-WEB_TREND_POINTS:],
        "empty_date": f"{empty:%d/%m/%Y}" if empty else "–",
    }


def build_payload(reading, warn_percent):
    """The web page's whole state: every string pre-formatted, the page only lays it out."""
    report, entries, prediction = reading.report, reading.entries, reading.prediction
    severity = report.severity(warn_percent)
    return {
        "read_at": report.read_at.isoformat(timespec="seconds"),
        "reads": reading.number,
        "read_ms": round(reading.read_ms),
        "warn_percent": warn_percent,
        "severity": severity,
        "verdict": SEVERITY_LABELS[severity],
        "status_rows": status_rows(report),
        "port_badges": [
            {"label": label, "ok": ok} for label, ok in port_badges(report)
        ],
        "alerts": [
            {
                "severity": sev,
                "id": alert_id,
                "text": translate_category(alert_id),
                "colors": colors,
            }
            for sev, alert_id, colors in report.alerts
        ],
        "actions": [
            {"subject": subject, "advice": advice, "color": color}
            for subject, advice, color in action_items(report, warn_percent)
        ],
        "next_out": next_to_run_out(report, prediction),
        "consumables": [
            consumable_view(c, report, warn_percent, entries, prediction)
            for c in report.consumables
        ],
        "counter_rows": counter_rows(report),
        "pace": pace_text(prediction) or "sem histórico ainda",
        "color_percent": color_percent(report.counters),
        "events": [
            {"seq": seq, "code": code, "page": page, "hint": event_hint(code)}
            for seq, code, page in report.events
        ],
        "changes": [
            {"at": f"{at:%H:%M:%S}", "text": text}
            for at, text in reversed(reading.changes)
        ],
        "usb_bus": usb_bus_rows(report),
        "controller": controller_rows(report),
        "endpoints": endpoint_rows(report),
        "identity": humanized_rows(report.identity),
        "settings": humanized_rows(report.settings),
        "latency_ms": report.latency_ms,
        "errors": report.errors,
    }

"""Rich rendering of a printer Report.

Readability rules applied (clig.dev / PatternFly CLI handbook / no-color.org):
- most important first: verdict, then what to do, then levels; details only with --full
- grey for structure, color only for meaning, and never color alone (always a symbol/word)
- human values (dates dd/mm/aaaa, "15 min", thousands separator), no emoji that breaks alignment
"""

import re
from collections import Counter
from datetime import datetime

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from history import level_series, sparkline
from printer_model import (
    CONSUMABLE_STATES_PT,
    OK_STATES,
    QUEUE_NAME,
    SEVERITY_ERROR,
    SEVERITY_OK,
    SEVERITY_WARNING,
    STATUS_CATEGORIES_PT,
    event_hint,
    years_since,
)

TANK_HEIGHT = 8
TANK_WIDTH = 6
BAR_WIDTH = 10
RATIO_BAR_WIDTH = 30
TREND_POINTS = 12
# Below this many columns the full supplies table would truncate its cells.
NARROW_WIDTH = 110
# Key/value panels never wrap, so a pair needs ~60 columns each before it may sit side by side.
WIDE_WIDTH = 120
FRAME = "grey50"
LABEL = "grey62"
ZEBRA = ["", "on grey11"]
COLOR_STYLES = {
    "Black": "bright_white",
    "Cyan": "cyan",
    "Magenta": "magenta",
    "Yellow": "yellow",
    "OPC": "green",
}
SEVERITY_LOOK = {
    SEVERITY_OK: ("✔ PRONTA", "bold black on green", "green"),
    SEVERITY_WARNING: ("▲ ATENÇÃO", "bold black on yellow", "yellow"),
    SEVERITY_ERROR: ("✖ COM PROBLEMAS", "bold white on red", "red"),
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


def badge(label, ok):
    mark = "✔" if ok else "✖"
    return Text(
        f" {mark} {label} ", style="bold black on green" if ok else "bold white on red"
    )


def translate_state(state):
    return CONSUMABLE_STATES_PT.get(state, state or "–")


def translate_category(category):
    return STATUS_CATEGORIES_PT.get(category, category or "–")


def section(title):
    return Rule(Text(title, style="bold"), style=FRAME, align="left")


def kv_grid(rows, value_justify="left", value_style=""):
    """Label/value grid that never wraps: long values end in an ellipsis instead of a new line."""
    grid = Table.grid(padding=(0, 2), expand=True)
    grid.add_column(style=LABEL, no_wrap=True)
    grid.add_column(
        justify=value_justify,
        style=value_style,
        no_wrap=True,
        overflow="ellipsis",
        ratio=1,
    )
    for key, value in rows.items():
        grid.add_row(key, value if isinstance(value, Text) else str(value))
    return grid


def one_line(*parts):
    return Text.assemble(*parts, no_wrap=True, overflow="ellipsis")


def neutral_panel(body, title):
    return Panel(body, title=title, title_align="left", border_style=FRAME)


def tank(consumable, warn_percent):
    """Vertical gauge drawn with block characters, filled from the bottom."""
    style = COLOR_STYLES[consumable.color]
    percent = consumable.percent
    filled = 0 if percent is None else round(TANK_HEIGHT * percent / 100)
    lines = Text(justify="center")
    lines.append("╭" + "─" * TANK_WIDTH + "╮\n", style=FRAME)
    for row in range(TANK_HEIGHT, 0, -1):
        lines.append("│", style=FRAME)
        if percent is None:
            cell = (
                " ?? ".center(TANK_WIDTH)
                if row == TANK_HEIGHT // 2
                else " " * TANK_WIDTH
            )
            lines.append(cell, style="bold red")
        elif row <= filled:
            lines.append("█" * TANK_WIDTH, style=style)
        else:
            lines.append("░" * TANK_WIDTH, style="grey23")
        lines.append("│\n", style=FRAME)
    lines.append("╰" + "─" * TANK_WIDTH + "╯\n", style=FRAME)
    lines.append(f"{consumable.name}\n", style=f"bold {style}")
    if consumable.state not in OK_STATES:
        lines.append(f"✖ {translate_state(consumable.state)}", style="bold red")
    else:
        low = consumable.needs_attention(warn_percent)
        lines.append(
            f"{'▲ ' if low else ''}{percent}%", style="bold yellow" if low else "bold"
        )
        lines.append(f"\n≈{number(consumable.pages_remaining)} pág.", style=LABEL)
    return lines


def tanks_panel(report, warn_percent):
    grid = Table.grid(padding=(0, 2))
    for _ in report.consumables:
        grid.add_column(justify="center")
    grid.add_row(*(tank(c, warn_percent) for c in report.consumables))
    return neutral_panel(Align.center(grid), "Níveis")


def header(report, warn_percent):
    label, style, border = SEVERITY_LOOK[report.severity(warn_percent)]
    title = Text.assemble(
        ("HP LaserJet CP1025", "bold"),
        "   ",
        (f" {label} ", style),
        "   ",
        (f"leitura de {report.read_at:%d/%m/%Y às %H:%M:%S}", LABEL),
    )
    return Panel(Align.center(title), box=box.HEAVY, border_style=border)


def status_panel(report):
    port = report.port
    badges = Text.assemble(
        badge("USB online" if port["selected"] else "USB offline", port["selected"]),
        " ",
        badge(
            "sem erro na porta" if not port["error"] else "erro na porta",
            not port["error"],
        ),
        " ",
        badge(
            "papel ok" if not port["paper_empty"] else "sem papel",
            not port["paper_empty"],
        ),
        " ",
        badge(
            "fila pausada" if report.queue_paused else "fila ativa",
            not report.queue_paused,
        ),
    )
    rows = kv_grid(
        {
            "Estado": Text(translate_category(report.status_category), style="bold"),
            "Carrossel": f"parado no cartucho {fmt(report.carousel)}",
            "Fila do Mac": humanize_queue(report.queue),
        }
    )
    alerts = [
        one_line(
            (
                "✖ " if severity == "Error" else "▲ ",
                "bold red" if severity == "Error" else "bold yellow",
            ),
            (translate_category(alert_id), "bold"),
            (f" · {colors}" if colors else "", "bold"),
            (f"  ({alert_id})" if alert_id != report.status_category else "", LABEL),
        )
        for severity, alert_id, colors in report.alerts
    ] or [Text("✔ nenhum alerta ativo", style="green")]
    border = "red" if report.alerts else FRAME
    return Panel(
        Group(badges, Text(), rows, Text(), *alerts),
        title="Status",
        title_align="left",
        border_style=border,
    )


def level_bar(percent, style):
    if percent is None:
        return Text("sem leitura", style=f"{LABEL} italic")
    filled = round(BAR_WIDTH * percent / 100)
    bar = Text("█" * filled, style=style)
    bar.append("░" * (BAR_WIDTH - filled), style="grey30")
    return bar.append(f" {percent:>3}%", style="bold")


def state_text(consumable, warn_percent):
    if consumable.state not in OK_STATES:
        return Text(f"✖ {translate_state(consumable.state)}", style="bold red")
    if consumable.needs_attention(warn_percent):
        return Text("▲ baixo", style="bold yellow")
    return Text("✔ ok", style="green")


SUPPLY_COLUMNS = [
    ("Item", "left", True),
    ("Código", "left", False),
    ("Estado", "left", True),
    ("Nível", "left", True),
    ("Restam", "right", True),
    ("Tendência", "left", False),
    ("Fim previsto", "left", False),
]


def supplies_table(report, warn_percent, entries, prediction, width):
    """Narrow terminals keep only the columns marked essential instead of truncating cells."""
    narrow = width < NARROW_WIDTH
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style=f"bold {LABEL}",
        border_style=FRAME,
        row_styles=ZEBRA,
        expand=True,
        caption="Restam: páginas estimadas pela própria impressora"
        + (
            ""
            if narrow
            else " · Tendência: nível nas últimas leituras · Fim previsto: precisa de histórico"
        ),
        caption_justify="left",
        caption_style=LABEL,
    )
    shown = [
        (name, justify)
        for name, justify, essential in SUPPLY_COLUMNS
        if essential or not narrow
    ]
    for name, justify in shown:
        table.add_column(name, justify=justify, no_wrap=True)
    for c in report.consumables:
        style = COLOR_STYLES[c.color]
        trend = sparkline(level_series(entries, c.color)[-TREND_POINTS:])
        empty = prediction.empty_date(c, report.read_at)
        cells = {
            "Item": Text(f"● {c.name}", style=f"bold {style}"),
            "Código": fmt(c.product_number),
            "Estado": state_text(c, warn_percent),
            "Nível": level_bar(c.percent, style),
            "Restam": number(c.pages_remaining),
            "Tendência": Text(trend or "–", style=style),
            "Fim previsto": Text(
                f"{empty:%d/%m/%Y}" if empty else "–", style="" if empty else LABEL
            ),
        }
        table.add_row(*(cells[name] for name, _ in shown))
    return table


def whole_years(years):
    return None if years is None else int(years)


def compact_table(columns):
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style=f"bold {LABEL}",
        border_style=FRAME,
        row_styles=ZEBRA,
        pad_edge=False,
    )
    for name, justify in columns:
        table.add_column(name, justify=justify, no_wrap=True)
    return table


def details_table(report):
    today = report.read_at.date()
    origin = compact_table(
        [
            ("Item", "left"),
            ("Marca", "left"),
            ("Serial", "left"),
            ("Fabricado", "left"),
            ("Idade", "right"),
            ("Chip", "left"),
        ]
    )
    usage = compact_table(
        [
            ("Item", "left"),
            ("Impressas", "right"),
            ("Rend.", "right"),
            ("Bruto", "right"),
            ("Instalado", "left"),
            ("Último uso", "left"),
            ("Mod.", "left"),
        ]
    )
    for c in report.consumables:
        chip_ok = c.chip_ok
        name = Text(c.name, style=COLOR_STYLES[c.color])
        origin.add_row(
            name,
            fmt(c.brand),
            fmt(c.serial),
            short_date(c.manufactured, "%m/%Y"),
            fmt(whole_years(years_since(c.manufactured, today)), " anos"),
            Text(
                ("✔ " if chip_ok else "✖ ") + (c.chip_tag or "–"),
                style="green" if chip_ok else "bold red",
            ),
        )
        usage.add_row(
            name,
            number(c.impressions),
            number(c.rated_yield or c.capacity),
            fmt(c.raw_percent, "%"),
            short_date(c.installed),
            short_date(c.last_used),
            fmt(c.model_info),
        )
    notes = [
        one_line(
            (
                f"↳ {c.name} anterior: {number(as_int_or_none(c.previous['impressions']))} pág. · "
                f"instalado {short_date(c.previous['installed'])} · último uso {short_date(c.previous['last_used'])}",
                LABEL,
            )
        )
        for c in report.consumables
        if c.previous.get("impressions") not in (None, "", "0")
    ]
    notes += [
        one_line(
            (
                f"A impressora avisa 'baixo' em {fmt(report.low_threshold)}% · "
                f"em nível muito baixo ela {humanize(report.very_low_action)}",
                LABEL,
            )
        ),
        one_line(
            (
                "Impressas = páginas com este cartucho · Rend. = rendimento de fábrica (ISO)",
                LABEL,
            )
        ),
        one_line(("Bruto = leitura crua do sensor · Mod. = id gravado no chip", LABEL)),
    ]
    tables = Columns([origin, usage], padding=(0, 4))
    return neutral_panel(Group(tables, *notes), "Cartuchos (dados do chip)")


def as_int_or_none(value):
    return int(value) if str(value).isdigit() else None


def ratio_bar(mono, color):
    total = (mono or 0) + (color or 0)
    if not total:
        return Text("–")
    color_cells = round(RATIO_BAR_WIDTH * color / total)
    bar = Text("█" * (RATIO_BAR_WIDTH - color_cells), style="grey70")
    bar.append("█" * color_cells, style="magenta")
    return bar.append(
        f"  P&B {100 - 100 * color / total:.0f}% · cor {100 * color / total:.0f}%",
        style="bold",
    )


def counters_panel(report, prediction):
    c = report.counters
    grid = kv_grid(
        {
            "Páginas no total": number(c.get("total")),
            "Preto e branco": number(c.get("mono")),
            "Coloridas": number(c.get("color")),
            "Atolamentos": number(c.get("jams")),
            "Falhas ao puxar papel": number(c.get("mispicks")),
            "Ritmo": (
                f"{prediction.pages_per_day:.1f} pág./dia ({number(prediction.window_pages)} em "
                f"{prediction.window_days:.0f} dias)"
                if prediction.pages_per_day
                else Text("sem histórico ainda", style=LABEL)
            ),
        },
        value_justify="right",
        value_style="bold",
    )
    return neutral_panel(
        Group(grid, Text(), ratio_bar(c.get("mono"), c.get("color"))), "Contadores"
    )


def port_bits(raw):
    """IEEE 1284 status byte: bit5 paper empty, bit4 selected, bit3 nERROR (1 = no error)."""
    value = int(raw, 16) if raw else 0
    flags = [
        "online" if value & 0x10 else "offline",
        "sem erro" if value & 0x08 else "ERRO",
        "SEM PAPEL" if value & 0x20 else "com papel",
    ]
    return f"{raw} = {value >> 4:04b} {value & 0xF:04b} → " + ", ".join(flags)


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


def usb_bus_panel(report):
    return neutral_panel(kv_grid(usb_bus_rows(report)), "Barramento USB")


ENDPOINT_COLUMNS = [
    ("If", "right", True),
    ("Classe", "left", False),
    ("EP", "left", True),
    ("Dir", "left", True),
    ("Tipo", "left", True),
    ("Pacote", "right", True),
    ("Interv.", "right", False),
    ("Uso", "left", True),
]


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


def endpoints_panel(report, width):
    narrow = width < NARROW_WIDTH
    shown = [
        (name, justify)
        for name, justify, essential in ENDPOINT_COLUMNS
        if essential or not narrow
    ]
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style=f"bold {LABEL}",
        border_style=FRAME,
        row_styles=ZEBRA,
        pad_edge=False,
        expand=True,
    )
    for name, justify in shown:
        table.add_column(name, justify=justify, no_wrap=True)
    for row in endpoint_rows(report):
        table.add_row(*(row[name] for name, _ in shown))
    return neutral_panel(table, "Interfaces e endpoints USB")


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


def controller_panel(report):
    return neutral_panel(kv_grid(controller_rows(report)), "Controlador e firmware")


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


def device_id_field(device_id, key):
    for part in device_id.split(";"):
        name, _, value = part.partition(":")
        if name.strip() == key:
            return value.strip()
    return ""


def events_panel(report):
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style=f"bold {LABEL}",
        border_style=FRAME,
        row_styles=ZEBRA,
        pad_edge=False,
        expand=True,
    )
    table.add_column("#", justify="right")
    table.add_column("Código", no_wrap=True)
    table.add_column("Na página", justify="right")
    table.add_column("Significado")
    for seq, code, pages in report.events:
        hint = event_hint(code)
        known = "cartucho" in hint
        table.add_row(
            str(seq),
            Text(code, style="bold red" if known else ""),
            pages,
            Text(hint, style="" if known else LABEL),
        )
    return neutral_panel(
        table, f"Log de eventos da impressora ({len(report.events)} de no máximo 10)"
    )


def key_value_panel(title, rows):
    return neutral_panel(
        kv_grid({key: humanize(fmt(value)) for key, value in rows.items()}), title
    )


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


def diagnosis_panel(report, warn_percent, prediction):
    actions = [
        one_line(
            ("➜ ", "bold"),
            (f"{subject}: ", f"bold {COLOR_STYLES.get(color, '')}"),
            advice,
        )
        for subject, advice, color in action_items(report, warn_percent)
    ]
    upcoming = next_to_run_out(report, prediction)
    if upcoming:
        actions += [Text(), Text(upcoming, style=LABEL)]
    severity = report.severity(warn_percent)
    body = Group(*actions) if actions else Text("✔ Nada a fazer", style="green")
    return Panel(
        body,
        title=f"O que fazer agora · aviso em ≤{warn_percent}%",
        title_align="left",
        border_style=SEVERITY_LOOK[severity][2],
    )


def side_by_side(width, left, right):
    """Two no-wrap panels only share a row when each still gets enough columns; else stack."""
    if width >= WIDE_WIDTH:
        return Columns([left, right], expand=True, equal=True)
    return Group(left, right)


def render(report, warn_percent, console, entries, prediction, full=False):
    """One-shot output: verdict and actions first, levels next, details only when asked."""
    console.print(header(report, warn_percent))
    console.print(diagnosis_panel(report, warn_percent, prediction))
    console.print(
        side_by_side(
            console.width, tanks_panel(report, warn_percent), status_panel(report)
        )
    )
    console.print(section("Suprimentos"))
    console.print(
        supplies_table(report, warn_percent, entries, prediction, console.width)
    )
    if not full:
        console.print(
            Text(
                "Mais detalhes (chip, contadores, log, hardware USB/controlador): --full",
                style=LABEL,
            )
        )
        return
    console.print(section("Detalhes"))
    console.print(details_table(report))
    console.print(
        side_by_side(
            console.width,
            counters_panel(report, prediction),
            key_value_panel("Configuração", report.settings),
        )
    )
    console.print(events_panel(report))
    console.print(section("Hardware"))
    console.print(
        side_by_side(console.width, usb_bus_panel(report), controller_panel(report))
    )
    console.print(endpoints_panel(report, console.width))
    console.print(key_value_panel("Identificação", report.identity))


def changes_panel(changes):
    """Newest first; each entry is (time, text)."""
    lines = [
        Text.assemble((f"{stamp:%H:%M:%S}  ", LABEL), (message, "bold"))
        for stamp, message in reversed(changes)
    ] or [Text("nenhuma mudança desde que o monitor abriu", style=LABEL)]
    return Panel(
        Group(*lines), title="Mudanças ao vivo", title_align="left", border_style="cyan"
    )


def live_layout(report, warn_percent, entries, prediction, footer, changes=None):
    """Full-screen layout for --watch and --live; `changes` adds the live change feed."""
    layout = Layout()
    layout.split_column(
        Layout(header(report, warn_percent), name="header", size=3),
        Layout(name="top", size=TANK_HEIGHT + 7),
        Layout(
            supplies_table(report, warn_percent, entries, prediction, NARROW_WIDTH),
            name="supplies",
            size=len(report.consumables) + 5,
        ),
        Layout(name="bottom"),
        Layout(Align.center(footer), name="footer", size=1),
    )
    layout["top"].split_row(
        Layout(tanks_panel(report, warn_percent), ratio=3),
        Layout(status_panel(report), ratio=2),
    )
    actions = [Layout(diagnosis_panel(report, warn_percent, prediction), ratio=3)]
    if changes is not None:
        actions.insert(0, Layout(changes_panel(changes), ratio=2))
    layout["bottom"].split_column(Layout(name="actions"), Layout(name="hardware"))
    layout["actions"].split_row(*actions)
    layout["hardware"].split_row(
        Layout(counters_panel(report, prediction), ratio=2),
        Layout(usb_bus_panel(report), ratio=3),
    )
    return layout

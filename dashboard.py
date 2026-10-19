"""Rich rendering of a printer Report. Wording and row contents come from viewmodel.py.

Readability rules applied (clig.dev / PatternFly CLI handbook / no-color.org):
- most important first: verdict, then what to do, then levels; details only with --full
- grey for structure, color only for meaning, and never color alone (always a symbol/word)
- human values (dates dd/mm/aaaa, "15 min", thousands separator), no emoji that breaks alignment
"""

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
    OK_STATES,
    Severity,
    event_hint,
    years_since,
)
from viewmodel import (
    SEVERITY_LABELS,
    action_items,
    controller_rows,
    counter_rows,
    endpoint_rows,
    fmt,
    humanize,
    humanized_rows,
    next_to_run_out,
    number,
    pace_text,
    port_badges,
    previous_cartridge_notes,
    short_date,
    status_rows,
    translate_category,
    translate_state,
    usb_bus_rows,
    whole_years,
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
SEVERITY_STYLES = {
    Severity.OK: ("bold black on green", "green"),
    Severity.WARNING: ("bold black on yellow", "yellow"),
    Severity.ERROR: ("bold white on red", "red"),
}
SUPPLY_COLUMNS = [
    ("Item", "left", True),
    ("Código", "left", False),
    ("Estado", "left", True),
    ("Nível", "left", True),
    ("Restam", "right", True),
    ("Tendência", "left", False),
    ("Fim previsto", "left", False),
]
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


def badge(label, ok):
    mark = "✔" if ok else "✖"
    return Text(
        f" {mark} {label} ", style="bold black on green" if ok else "bold white on red"
    )


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


def table(columns, expand=True):
    grid = Table(
        box=box.SIMPLE_HEAD,
        header_style=f"bold {LABEL}",
        border_style=FRAME,
        row_styles=ZEBRA,
        pad_edge=False,
        expand=expand,
    )
    for name, justify in columns:
        grid.add_column(name, justify=justify, no_wrap=True)
    return grid


def visible_columns(columns, width):
    """Narrow terminals keep only the columns marked essential instead of truncating cells."""
    narrow = width < NARROW_WIDTH
    return [
        (name, justify)
        for name, justify, essential in columns
        if essential or not narrow
    ]


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
    severity = report.severity(warn_percent)
    style, border = SEVERITY_STYLES[severity]
    title = Text.assemble(
        ("HP LaserJet CP1025", "bold"),
        "   ",
        (f" {SEVERITY_LABELS[severity]} ", style),
        "   ",
        (f"leitura de {report.read_at:%d/%m/%Y às %H:%M:%S}", LABEL),
    )
    return Panel(Align.center(title), box=box.HEAVY, border_style=border)


def status_panel(report):
    badges = Text(" ").join(badge(label, ok) for label, ok in port_badges(report))
    rows = status_rows(report)
    rows["Estado"] = Text(rows["Estado"], style="bold")
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
        Group(badges, Text(), kv_grid(rows), Text(), *alerts),
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


def supplies_table(report, warn_percent, entries, prediction, width):
    shown = visible_columns(SUPPLY_COLUMNS, width)
    grid = table(shown)
    grid.pad_edge = True
    grid.caption = "Restam: páginas estimadas pela própria impressora" + (
        ""
        if len(shown) < len(SUPPLY_COLUMNS)
        else " · Tendência: nível nas últimas leituras · Fim previsto: precisa de histórico"
    )
    grid.caption_justify = "left"
    grid.caption_style = LABEL
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
        grid.add_row(*(cells[name] for name, _ in shown))
    return grid


def details_table(report):
    today = report.read_at.date()
    origin = table(
        [
            ("Item", "left"),
            ("Marca", "left"),
            ("Serial", "left"),
            ("Fabricado", "left"),
            ("Idade", "right"),
            ("Chip", "left"),
        ],
        expand=False,
    )
    usage = table(
        [
            ("Item", "left"),
            ("Impressas", "right"),
            ("Rend.", "right"),
            ("Bruto", "right"),
            ("Instalado", "left"),
            ("Último uso", "left"),
            ("Mod.", "left"),
        ],
        expand=False,
    )
    for c in report.consumables:
        name = Text(c.name, style=COLOR_STYLES[c.color])
        origin.add_row(
            name,
            fmt(c.brand),
            fmt(c.serial),
            short_date(c.manufactured, "%m/%Y"),
            fmt(whole_years(years_since(c.manufactured, today)), " anos"),
            Text(
                ("✔ " if c.chip_ok else "✖ ") + (c.chip_tag or "–"),
                style="green" if c.chip_ok else "bold red",
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
    notes = [one_line((note, LABEL)) for note in previous_cartridge_notes(report)]
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
    rows = counter_rows(report)
    rows["Ritmo"] = pace_text(prediction) or Text("sem histórico ainda", style=LABEL)
    grid = kv_grid(rows, value_justify="right", value_style="bold")
    c = report.counters
    return neutral_panel(
        Group(grid, Text(), ratio_bar(c.get("mono"), c.get("color"))), "Contadores"
    )


def usb_bus_panel(report):
    return neutral_panel(kv_grid(usb_bus_rows(report)), "Barramento USB")


def endpoints_panel(report, width):
    shown = visible_columns(ENDPOINT_COLUMNS, width)
    grid = table(shown)
    for row in endpoint_rows(report):
        grid.add_row(*(row[name] for name, _ in shown))
    return neutral_panel(grid, "Interfaces e endpoints USB")


def controller_panel(report):
    return neutral_panel(kv_grid(controller_rows(report)), "Controlador e firmware")


def events_panel(report):
    grid = table(
        [
            ("#", "right"),
            ("Código", "left"),
            ("Na página", "right"),
            ("Significado", "left"),
        ]
    )
    for seq, code, pages in report.events:
        hint = event_hint(code)
        known = "cartucho" in hint
        grid.add_row(
            str(seq),
            Text(code, style="bold red" if known else ""),
            pages,
            Text(hint, style="" if known else LABEL),
        )
    return neutral_panel(
        grid, f"Log de eventos da impressora ({len(report.events)} de no máximo 10)"
    )


def key_value_panel(title, rows):
    return neutral_panel(kv_grid(humanized_rows(rows)), title)


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
    _, border = SEVERITY_STYLES[report.severity(warn_percent)]
    body = Group(*actions) if actions else Text("✔ Nada a fazer", style="green")
    return Panel(
        body,
        title=f"O que fazer agora · aviso em ≤{warn_percent}%",
        title_align="left",
        border_style=border,
    )


def side_by_side(width, left, right):
    """Two no-wrap panels only share a row when each still gets enough columns; else stack."""
    if width >= WIDE_WIDTH:
        return Columns([left, right], expand=True, equal=True)
    return Group(left, right)


def render(reading, warn_percent, console, full=False):
    """One-shot output: verdict and actions first, levels next, details only when asked."""
    report, entries, prediction = reading.report, reading.entries, reading.prediction
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


def live_layout(reading, warn_percent, footer, changes=None):
    """Full-screen layout for --watch and --live; `changes` adds the live change feed."""
    report, entries, prediction = reading.report, reading.entries, reading.prediction
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

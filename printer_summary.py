"""Status summary and live monitor for the HP LaserJet CP1025 (USB/LEDM + CUPS queue).

Usage:
  python3 printer_summary.py                 one-shot visual summary (verdict, actions, levels)
  python3 printer_summary.py --full          plus chip data, counters, USB link, event log, settings
  python3 printer_summary.py --watch 30      full-screen monitor, refresh every 30 s
  python3 printer_summary.py --live          real time: reads every ~1 s, feed of every change
  python3 printer_summary.py --web           real-time web page on http://127.0.0.1:8025
  python3 printer_summary.py --json          machine-readable report
Exit code: 0 ok, 1 warning (low toner/alerts), 2 error (blocking problem or unreadable).
"""

import argparse
import dataclasses
import json
import sys
import time
from collections import deque
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.text import Text

import history
import web_dashboard
from dashboard import live_layout, render
from hpstatus import UsbError
from printer_model import SEVERITY_ERROR, collect

DEFAULT_WARN_PERCENT = 20
MIN_WATCH_SECONDS = 5
# A full LEDM read measured ~205 ms; 1 s cadence keeps the USB link busy ~20% of the time.
LIVE_MIN_SECONDS = 1.0
LIVE_DUTY_FACTOR = 4
CHANGE_FEED_SIZE = 12


def read_and_record():
    report = collect()
    entries = history.record(report)
    return report, entries, history.forecast(entries)


def as_json(report, warn_percent, prediction):
    payload = dataclasses.asdict(report)
    payload["severity"] = report.severity(warn_percent)
    payload["pages_per_day"] = prediction.pages_per_day
    payload["empty_dates"] = {
        c.color: str(prediction.empty_date(c, report.read_at) or "")
        for c in report.consumables
    }
    return json.dumps(payload, default=str, ensure_ascii=False, indent=2)


def watch_footer(report, interval):
    return Text(
        f"Ctrl+C para sair · atualiza a cada {interval}s · última leitura {report.read_at:%H:%M:%S}",
        style="grey62",
    )


def live_footer(report, reads, read_ms, interval):
    return Text.assemble(
        ("● AO VIVO", "bold red"),
        (
            f"  ·  leitura nº {reads} às {report.read_at:%H:%M:%S}  ·  {read_ms:.0f} ms"
            f"  ·  próxima em {interval:.1f}s  ·  Ctrl+C para sair",
            "grey62",
        ),
    )


def failed_footer(error):
    return Text(
        f"leitura falhou em {datetime.now():%H:%M:%S}: {error} · mostrando a última leitura boa",
        style="bold yellow",
    )


def live_interval(read_seconds):
    """Keep the USB link idle most of the time: wait at least LIVE_DUTY_FACTOR x the read time."""
    return max(LIVE_MIN_SECONDS, read_seconds * LIVE_DUTY_FACTOR)


def monitor(console, warn_percent, interval=None):
    """Full-screen loop. interval=None means --live: adaptive cadence plus the change feed."""
    live_mode = interval is None
    changes = deque(maxlen=CHANGE_FEED_SIZE) if live_mode else None
    started = time.monotonic()
    report, entries, prediction = read_and_record()
    read_seconds = time.monotonic() - started
    reads = 1
    wait = live_interval(read_seconds) if live_mode else interval

    def frame(footer):
        return live_layout(report, warn_percent, entries, prediction, footer, changes)

    def footer():
        if live_mode:
            return live_footer(report, reads, read_seconds * 1000, wait)
        return watch_footer(report, interval)

    with Live(
        frame(footer()), console=console, screen=True, refresh_per_second=4
    ) as live:
        while True:
            time.sleep(wait)
            previous = report
            started = time.monotonic()
            try:
                report, entries, prediction = read_and_record()
            except (UsbError, RuntimeError) as error:
                # Keep showing the last good reading; the printer may be busy printing.
                live.update(frame(failed_footer(error)))
                continue
            read_seconds = time.monotonic() - started
            reads += 1
            if live_mode:
                wait = live_interval(read_seconds)
                changes.extend(
                    (report.read_at, change)
                    for change in report.changes_since(previous)
                )
            live.update(frame(footer()))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[2:]),
    )
    parser.add_argument(
        "--warn",
        type=int,
        default=DEFAULT_WARN_PERCENT,
        help="aviso de troca com nivel <= este %%",
    )
    parser.add_argument(
        "--watch", type=int, metavar="SEGUNDOS", help="modo monitor em tela cheia"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="tempo real: le a cada ~1 s e mostra cada mudanca",
    )
    parser.add_argument("--json", action="store_true", help="saida em JSON")
    parser.add_argument(
        "--web",
        action="store_true",
        help="pagina web ao vivo em http://127.0.0.1:PORTA",
    )
    parser.add_argument(
        "--port", type=int, default=web_dashboard.DEFAULT_PORT, help="porta do --web"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="mostra tambem chip, contadores, USB, log e configuracao",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="sem cores (tambem respeita NO_COLOR)"
    )
    args = parser.parse_args()
    if sum(map(bool, (args.live, args.watch, args.web))) > 1:
        parser.error("escolha so um: --live, --watch ou --web")
    if args.watch is not None and args.watch < MIN_WATCH_SECONDS:
        parser.error(f"--watch precisa de pelo menos {MIN_WATCH_SECONDS} segundos")

    console = Console(no_color=args.no_color or None)
    try:
        if args.web:
            web_dashboard.serve(args.warn, args.port)
        if args.live:
            monitor(console, args.warn)
        if args.watch:
            monitor(console, args.warn, args.watch)
        with console.status("Lendo a impressora pela USB..."):
            report, entries, prediction = read_and_record()
    except KeyboardInterrupt:
        return 0
    except (UsbError, RuntimeError) as error:
        console.print(
            Text(f"✖ Não consegui ler a impressora: {error}", style="bold red")
        )
        return SEVERITY_ERROR

    if args.json:
        print(as_json(report, args.warn, prediction))
    else:
        render(report, args.warn, console, entries, prediction, full=args.full)
    return report.severity(args.warn)


if __name__ == "__main__":
    sys.exit(main())

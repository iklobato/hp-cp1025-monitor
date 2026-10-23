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
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.text import Text

import web_dashboard
from dashboard import live_layout, render
from hpstatus import UsbError
from poller import Poller
from printer_model import Severity

DEFAULT_WARN_PERCENT = 20
MIN_WATCH_SECONDS = 5
LIVE_FEED_SIZE = 12


def as_json(reading, warn_percent):
    report, prediction = reading.report, reading.prediction
    payload = dataclasses.asdict(report)
    payload["severity"] = report.severity(warn_percent)
    payload["pages_per_day"] = prediction.pages_per_day
    payload["empty_dates"] = {
        c.color: str(prediction.empty_date(c, report.read_at) or "")
        for c in report.consumables
    }
    return json.dumps(payload, default=str, ensure_ascii=False, indent=2)


def watch_footer(reading, seconds):
    return Text(
        f"Ctrl+C para sair · atualiza a cada {seconds}s · "
        f"última leitura {reading.report.read_at:%H:%M:%S}",
        style="grey62",
    )


def live_footer(reading):
    return Text.assemble(
        ("● AO VIVO", "bold red"),
        (
            f"  ·  leitura nº {reading.number} às {reading.report.read_at:%H:%M:%S}"
            f"  ·  {reading.read_ms:.0f} ms  ·  próxima em {reading.next_in:.1f}s  ·  Ctrl+C para sair",
            "grey62",
        ),
    )


def failed_footer(error):
    return Text(
        f"leitura falhou em {datetime.now():%H:%M:%S}: {error} · mostrando a última leitura boa",
        style="bold yellow",
    )


def fullscreen(console, poller, frame, pause):
    """Redraw `frame(reading, error)` after every poll; a failed poll keeps the last reading."""
    reading = poller.tick()
    with Live(
        frame(reading, None), console=console, screen=True, refresh_per_second=4
    ) as live:
        while True:
            time.sleep(pause(reading))
            try:
                reading = poller.tick()
            except (UsbError, RuntimeError) as error:
                live.update(frame(reading, error))
                continue
            live.update(frame(reading, None))


def live(console, warn_percent):
    """Adaptive ~1 s cadence plus the change feed."""

    def frame(reading, error):
        footer = failed_footer(error) if error else live_footer(reading)
        return live_layout(reading, warn_percent, footer, changes=reading.changes)

    fullscreen(console, Poller(feed_size=LIVE_FEED_SIZE), frame, lambda r: r.next_in)


def watch(console, warn_percent, seconds):
    """Fixed interval, no change feed."""

    def frame(reading, error):
        footer = failed_footer(error) if error else watch_footer(reading, seconds)
        return live_layout(reading, warn_percent, footer)

    fullscreen(console, Poller(), frame, lambda _: seconds)


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
        "--host",
        default=web_dashboard.HOST,
        help="endereco do --web (0.0.0.0 abre para a rede local)",
    )
    parser.add_argument(
        "--every",
        type=float,
        default=web_dashboard.MIN_INTERVAL_SECONDS,
        metavar="SEGUNDOS",
        help="intervalo minimo entre leituras do --web",
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
            web_dashboard.serve(args.warn, args.port, args.host, args.every)
        if args.live:
            live(console, args.warn)
        if args.watch:
            watch(console, args.warn, args.watch)
        with console.status("Lendo a impressora pela USB..."):
            reading = Poller().tick()
    except KeyboardInterrupt:
        return 0
    except (UsbError, RuntimeError) as error:
        console.print(
            Text(f"✖ Não consegui ler a impressora: {error}", style="bold red")
        )
        return Severity.ERROR

    if args.json:
        print(as_json(reading, args.warn))
    else:
        render(reading, args.warn, console, full=args.full)
    return reading.report.severity(args.warn)


if __name__ == "__main__":
    sys.exit(main())

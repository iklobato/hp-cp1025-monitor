"""Data for the Pi dashboard: the printer and Pi status on this machine."""

import json
import re
import os
import shlex
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from net import SourceError, fetch_bytes, parse_json


@dataclass(frozen=True)
class PrinterReading:
    read_at: datetime
    total_pages: int
    color_pages: int
    status: str
    alerts: tuple[str, ...]
    supplies: dict


def last_printer_reading(history: Path) -> PrinterReading | None:
    """Newest line of the printer monitor's history; it is written only when the printer answers."""
    if not history.exists():
        return None
    lines = [line for line in history.read_text().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        row = json.loads(lines[-1])
        return PrinterReading(
            datetime.fromisoformat(row["read_at"]).astimezone(),
            int(row["total_pages"]),
            int(row["color_pages"]),
            str(row["status"]),
            tuple(row.get("alerts", [])),
            dict(row["supplies"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SourceError("historico invalido") from err


@dataclass(frozen=True)
class PrinterStatus:
    payload: dict  # the monitor's full reading (viewmodel.build_payload); {} before the first
    feed_error: str  # empty while the monitor reads the printer fine
    from_disk: bool  # the monitor has nothing yet (printer off since it started): last saved copy


def printer_status(
    url: str, saved: Path, save: bool, fetch: Callable[[str], bytes] = fetch_bytes
) -> PrinterStatus:
    """The monitor's /api/status. A full reading is copied to `saved` when `save` is set, so the
    page still shows the printer after a reboot with the printer switched off."""
    data = parse_json(fetch(url))
    feed_error = str(data.get("feed_error", ""))
    if "verdict" in data:
        if save:
            saved.parent.mkdir(parents=True, exist_ok=True)
            temporary = saved.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False))
            temporary.replace(saved)
        return PrinterStatus(data, feed_error, False)
    try:
        return PrinterStatus(json.loads(saved.read_text()), feed_error, True)
    except (OSError, ValueError):
        return PrinterStatus({}, feed_error, False)


def run_command(command: str, scratch: Path) -> tuple[int, str]:
    """Run a fixed shell command, output captured through a file (no subprocess, by house rule)."""
    scratch.parent.mkdir(parents=True, exist_ok=True)
    status = os.system(f"{command} > {shlex.quote(str(scratch))} 2>&1")
    return status, scratch.read_text(errors="replace") if scratch.exists() else ""


def print_queue(scratch: Path) -> tuple[str, ...]:
    """Pending jobs, one `lpstat -o` line each."""
    status, output = run_command("lpstat -o", scratch)
    if status != 0 and output.strip():
        raise SourceError(output.strip().splitlines()[0])
    return tuple(line for line in output.splitlines() if line.strip())


@dataclass(frozen=True)
class Job:
    name: str
    user: str
    finished: datetime


def jobs_from_lpstat(output: str) -> tuple[Job, ...]:
    """`LC_ALL=C lpstat -W completed -o` lines: job, user, bytes, then `Thu Oct 22 22:35:01 2026` (local time)."""
    jobs = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            finished = datetime.strptime(
                " ".join(parts[4:8]), "%b %d %H:%M:%S %Y"
            ).astimezone()
        except ValueError:
            continue
        jobs.append(Job(parts[0], parts[1], finished))
    return tuple(sorted(jobs, key=lambda job: job.finished, reverse=True))


def completed_jobs(scratch: Path) -> tuple[Job, ...]:
    status, output = run_command("LC_ALL=C lpstat -W completed -o", scratch)
    if status != 0 and output.strip():
        raise SourceError(output.strip().splitlines()[0])
    return jobs_from_lpstat(output)


def daily_pages(entries: list[dict]) -> tuple[tuple[date, int], ...]:
    """Pages printed per day, from the printer counter at each reading (first day has no before)."""
    last_total: dict[date, int] = {}
    for entry in entries:
        if entry.get("total_pages") is None:
            continue
        day = datetime.fromisoformat(entry["read_at"]).date()
        last_total[day] = max(last_total.get(day, 0), int(entry["total_pages"]))
    days = sorted(last_total)
    return tuple(
        (day, last_total[day] - last_total[before])
        for before, day in zip(days, days[1:])
    )


# vcgencmd get_throttled bits: 0-3 happening now, 16-19 happened since boot.
THROTTLE_NOW = {
    0x1: "fonte fraca agora",
    0x2: "processador limitado",
    0x4: "freando por calor",
    0x8: "no limite de calor",
}
THROTTLE_SINCE_BOOT = {
    0x10000: "fonte fraca",
    0x20000: "processador limitado",
    0x40000: "freou por calor",
    0x80000: "chegou no limite de calor",
}


def throttle_notes(bits: int) -> tuple[list[str], list[str]]:
    return (
        [text for bit, text in THROTTLE_NOW.items() if bits & bit],
        [text for bit, text in THROTTLE_SINCE_BOOT.items() if bits & bit],
    )


def throttle_bits(scratch: Path) -> int:
    status, output = run_command("vcgencmd get_throttled", scratch)
    try:
        return int(output.strip().split("=", 1)[1], 16)
    except (IndexError, ValueError) as err:
        raise SourceError("vcgencmd sem resposta") from err


def services_state(names: tuple[str, ...], scratch: Path) -> dict[str, str]:
    """`systemctl is-active` prints one state per unit, in order; it exits non-zero when any is down."""
    _, output = run_command(
        "systemctl is-active " + " ".join(shlex.quote(name) for name in names), scratch
    )
    states = output.split()
    if len(states) != len(names):
        raise SourceError("systemctl sem resposta")
    return dict(zip(names, states))


@dataclass(frozen=True)
class Network:
    address: str
    ping_ms: float | None  # None: no answer from the internet


def network_from_output(addresses: str, ping: str) -> Network:
    ipv4 = next((a for a in addresses.split() if a.count(".") == 3), "")
    match = re.search(r"= [\d.]+/([\d.]+)/", ping)
    return Network(ipv4, float(match.group(1)) if match else None)


def network(scratch: Path) -> Network:
    _, addresses = run_command("hostname -I", scratch)
    _, ping = run_command("ping -c 1 -W 2 1.1.1.1", scratch)
    return network_from_output(addresses, ping)


def journal_errors(scratch: Path) -> int:
    """Lines at priority err or worse in the last hour (the user is in group adm, so it sees all)."""
    status, output = run_command(
        "journalctl -p err --since -1h -q --no-pager -o cat", scratch
    )
    if status != 0:
        raise SourceError("journalctl sem resposta")
    return sum(1 for line in output.splitlines() if line.strip())


def failed_units(scratch: Path) -> tuple[str, ...]:
    status, output = run_command("systemctl --failed --no-legend --plain", scratch)
    if status != 0:
        raise SourceError("systemctl sem resposta")
    return tuple(line.split()[0] for line in output.splitlines() if line.strip())


def core_volts(scratch: Path) -> float:
    _, output = run_command("vcgencmd measure_volts core", scratch)
    match = re.search(r"volt=([\d.]+)V", output)
    if match is None:
        raise SourceError("vcgencmd sem resposta")
    return float(match.group(1))


def pending_updates(scratch: Path) -> int:
    status, output = run_command("apt list --upgradable", scratch)
    if status != 0:
        raise SourceError("apt sem resposta")
    return sum(1 for line in output.splitlines() if "[upgradable from:" in line)


@dataclass(frozen=True)
class PiStats:
    temperature: float
    load: float
    cpus: int
    memory_used: float
    memory_total_mb: int
    disk_used: float
    uptime: timedelta


def pi_stats(
    proc: Path = Path("/proc"),
    thermal: Path = Path("/sys/class/thermal/thermal_zone0/temp"),
    root: str = "/",
) -> PiStats:
    meminfo = dict(
        (key, int(value.split()[0]))
        for key, value in (
            line.split(":", 1) for line in (proc / "meminfo").read_text().splitlines()
        )
    )
    disk = os.statvfs(root)
    return PiStats(
        temperature=int(thermal.read_text()) / 1000,
        load=float((proc / "loadavg").read_text().split()[0]),
        cpus=os.cpu_count() or 1,
        memory_used=1 - meminfo["MemAvailable"] / meminfo["MemTotal"],
        memory_total_mb=meminfo["MemTotal"] // 1024,
        disk_used=1 - disk.f_bavail / disk.f_blocks,
        uptime=timedelta(seconds=float((proc / "uptime").read_text().split()[0])),
    )


def selftest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        history = Path(tmp) / "history.jsonl"
        assert last_printer_reading(history) is None
        history.write_text(
            '{"read_at": "2026-10-22T10:00:00", "total_pages": 1, "color_pages": 0, "status": "x", "supplies": {}}\n'
            '{"read_at": "2026-10-22T22:50:06", "total_pages": 1328, "color_pages": 455, "status": "inPowerSave", '
            '"alerts": ["incompatibleConsumable"], "supplies": {"Cyan": {"percent": 50, "pages_remaining": 350}}}\n'
        )
        reading = last_printer_reading(history)
        assert reading.total_pages == 1328 and reading.supplies["Cyan"]["percent"] == 50
        assert reading.alerts == ("incompatibleConsumable",)
        saved = Path(tmp) / "printer.json"
        empty = printer_status(
            "u",
            saved,
            True,
            lambda url: b'{"loading": true, "feed_error": "printer not found"}',
        )
        assert empty == PrinterStatus({}, "printer not found", False), (
            "nothing saved yet: no reading at all"
        )
        printer_status("u", saved, False, lambda url: b'{"verdict": "ok", "reads": 1}')
        assert not saved.exists(), "the caller decides how often the SD card is written"
        live = printer_status(
            "u", saved, True, lambda url: b'{"verdict": "ok", "reads": 2}'
        )
        assert live == PrinterStatus({"verdict": "ok", "reads": 2}, "", False)
        off = printer_status(
            "u", saved, True, lambda url: b'{"loading": true, "feed_error": "x"}'
        )
        assert off == PrinterStatus({"verdict": "ok", "reads": 2}, "x", True), (
            "printer off: last saved reading"
        )

        lpstat = (
            "HP_CP1025-14            21121210G        20480   Thu Oct 22 21:36:37 2026\n"
            "HP_CP1025-16            iklo            110592   Thu Oct 22 22:35:01 2026\n"
            "linha estranha\n"
        )
        jobs = jobs_from_lpstat(lpstat)
        assert [job.name for job in jobs] == ["HP_CP1025-16", "HP_CP1025-14"], (
            "newest first"
        )
        assert jobs[0].finished.hour == 22 and jobs[0].finished.tzinfo is not None
        printed = daily_pages(
            [
                {"read_at": "2026-10-20T08:00:00", "total_pages": 1300},
                {"read_at": "2026-10-21T09:00:00", "total_pages": 1310},
                {"read_at": "2026-10-21T18:00:00", "total_pages": 1320},
                {"read_at": "2026-10-22T22:50:06", "total_pages": 1328},
                {"read_at": "2026-10-22T23:00:00", "total_pages": None},
            ]
        )
        assert printed == ((date(2026, 10, 21), 20), (date(2026, 10, 22), 8))
        assert throttle_notes(0x0) == ([], []) and throttle_notes(0x50005) == (
            ["fonte fraca agora", "freando por calor"],
            ["fonte fraca", "freou por calor"],
        )
        net = network_from_output(
            "192.168.0.10 fd00::1 2606:4700::1",
            "rtt min/avg/max/mdev = 26.731/34.470/39.332/5.532 ms",
        )
        assert net == Network("192.168.0.10", 34.47)
        assert network_from_output("", "100% packet loss").ping_ms is None
        proc = Path(tmp) / "proc"
        proc.mkdir()
        (proc / "meminfo").write_text(
            "MemTotal: 926208 kB\nMemAvailable: 463104 kB\nCached: 1 kB\n"
        )
        (proc / "loadavg").write_text("0.42 0.30 0.20 1/200 999\n")
        (proc / "uptime").write_text("3600.5 100.0\n")
        thermal = Path(tmp) / "temp"
        thermal.write_text("49388\n")
        stats = pi_stats(proc, thermal, tmp)
        assert (
            stats.temperature == 49.388
            and stats.load == 0.42
            and stats.memory_used == 0.5
        )
        assert stats.memory_total_mb == 904 and stats.uptime == timedelta(
            seconds=3600.5
        )
        assert 0 <= stats.disk_used <= 1
    print("system_data selftest ok")


if __name__ == "__main__":
    selftest()

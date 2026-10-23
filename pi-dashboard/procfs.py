"""Low-level Pi readings straight from /proc and /sys: per-core CPU, clock, memory breakdown,
disk and network rates, top processes, sockets, USB devices and OS identity.

Counters in /proc only ever grow, so rates come from two snapshots: `sample()` takes one,
`rates(previous, current)` turns a pair into per-second values. Parsers take the file text,
so the selftest feeds them fixed strings instead of a live system.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

PROC = Path("/proc")
SYS = Path("/sys")
TICKS = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
SECTOR_BYTES = (
    512  # /proc/diskstats always counts 512-byte sectors, whatever the device
)
DISK = "mmcblk0"
TCP_LISTEN, TCP_ESTABLISHED = "0A", "01"
PORT_NAMES = {
    22: "ssh",
    53: "dns",
    631: "cups",
    5353: "mdns",
    8025: "monitor",
}
TOP_PROCESSES = 8
# The Pi 3's own chips report no product name over USB; the printer is named for clarity.
USB_NAMES = {
    "0424:9514": "hub USB interno",
    "0424:ec00": "placa de rede Ethernet",
    "03f0:112a": "impressora HP LaserJet CP1025",
}
INTERPRETERS = ("python", "perl", "node", "sh", "bash")


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    ticks: int  # user + system CPU time since start
    rss_kb: int


@dataclass(frozen=True)
class Counters:
    """Cumulative counters at one instant; meaningless alone, rates come from two."""

    at: float  # time.monotonic()
    cores: tuple[tuple[int, int], ...]  # (busy, total) jiffies per core
    switches: int
    interrupts: int
    disk: tuple[int, int, int, int]  # reads, sectors read, writes, sectors written
    network: tuple[int, int]  # bytes received, bytes sent (every interface but lo)
    processes: tuple[Process, ...]


@dataclass(frozen=True)
class Busy:
    pid: int
    name: str
    cpu: float  # share of one core, like top
    rss_kb: int


@dataclass(frozen=True)
class Rates:
    cores: tuple[float, ...]  # 0..1 each
    switches: float
    interrupts: float
    read_bytes: float
    write_bytes: float
    read_ops: float
    write_ops: float
    down_bytes: float
    up_bytes: float
    top: tuple[Busy, ...]


@dataclass(frozen=True)
class Memory:
    """kB, as /proc/meminfo counts them."""

    total: int
    free: int
    buffers: int
    cached: int
    swap_total: int
    swap_free: int

    @property
    def used(self) -> int:
        return self.total - self.free - self.buffers - self.cached


@dataclass(frozen=True)
class Gauges:
    """Values that are already 'now' in /proc, no second snapshot needed."""

    memory: Memory
    load: tuple[float, float, float]
    clock_mhz: tuple[int, int, int] | None  # current, min, max of core 0
    wifi_dbm: float | None
    established: int
    listening: tuple[int, ...]  # TCP ports
    udp_ports: tuple[int, ...]


@dataclass(frozen=True)
class System:
    kernel: str
    distro: str
    booted: datetime
    usb: tuple[str, ...]


def cpu_counters(stat: str) -> tuple[tuple[tuple[int, int], ...], int, int]:
    cores, switches, interrupts = [], 0, 0
    for line in stat.splitlines():
        name, _, rest = line.partition(" ")
        values = [int(v) for v in rest.split()]
        if name.startswith("cpu") and name != "cpu":
            idle = values[3] + values[4]  # idle + iowait
            total = sum(values[:8])  # guest time is already inside user/nice
            cores.append((total - idle, total))
        elif name == "ctxt":
            switches = values[0]
        elif name == "intr":
            interrupts = values[0]
    return tuple(cores), switches, interrupts


def disk_counters(diskstats: str, device: str = DISK) -> tuple[int, int, int, int]:
    for line in diskstats.splitlines():
        fields = line.split()
        if len(fields) > 9 and fields[2] == device:
            return int(fields[3]), int(fields[5]), int(fields[7]), int(fields[9])
    return 0, 0, 0, 0


def network_counters(dev: str) -> tuple[int, int]:
    received = sent = 0
    for line in dev.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        fields = rest.split()
        received, sent = received + int(fields[0]), sent + int(fields[8])
    return received, sent


def process_from_stat(stat: str, rss_pages_kb: int = PAGE_KB) -> Process:
    """One /proc/<pid>/stat line; the name sits in parentheses and may hold spaces."""
    head, _, rest = stat.rpartition(")")
    pid, _, name = head.partition(" (")
    fields = rest.split()  # fields[0] is the state, field 3 of the man page
    return Process(
        int(pid),
        name,
        int(fields[11]) + int(fields[12]),
        int(fields[21]) * rss_pages_kb,
    )


def processes(proc: Path = PROC) -> tuple[Process, ...]:
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            found.append(process_from_stat((entry / "stat").read_text()))
        except (OSError, ValueError, IndexError):  # the process ended while being read
            continue
    return tuple(found)


def sample(now: float, proc: Path = PROC) -> Counters:
    cores, switches, interrupts = cpu_counters((proc / "stat").read_text())
    return Counters(
        now,
        cores,
        switches,
        interrupts,
        disk_counters((proc / "diskstats").read_text()),
        network_counters((proc / "net" / "dev").read_text()),
        processes(proc),
    )


def rates(previous: Counters, current: Counters, ticks: int = TICKS) -> Rates:
    seconds = max(current.at - previous.at, 1e-3)

    def per_second(new: int, old: int) -> float:
        return max(new - old, 0) / seconds

    cores = tuple(
        (busy - old_busy) / (total - old_total) if total > old_total else 0.0
        for (busy, total), (old_busy, old_total) in zip(current.cores, previous.cores)
    )
    before = {p.pid: p.ticks for p in previous.processes}
    busy = sorted(
        (
            Busy(p.pid, p.name, per_second(p.ticks, before[p.pid]) / ticks, p.rss_kb)
            for p in current.processes
            if p.pid in before
        ),
        key=lambda b: (b.cpu, b.rss_kb),
        reverse=True,
    )
    reads, read_sectors, writes, write_sectors = current.disk
    old_reads, old_read_sectors, old_writes, old_write_sectors = previous.disk
    return Rates(
        cores,
        per_second(current.switches, previous.switches),
        per_second(current.interrupts, previous.interrupts),
        per_second(read_sectors, old_read_sectors) * SECTOR_BYTES,
        per_second(write_sectors, old_write_sectors) * SECTOR_BYTES,
        per_second(reads, old_reads),
        per_second(writes, old_writes),
        per_second(current.network[0], previous.network[0]),
        per_second(current.network[1], previous.network[1]),
        tuple(busy[:TOP_PROCESSES]),
    )


def memory(meminfo: str) -> Memory:
    values = {
        key: int(value.split()[0])
        for key, _, value in (line.partition(":") for line in meminfo.splitlines())
        if value
    }
    return Memory(
        values["MemTotal"],
        values["MemFree"],
        values["Buffers"],
        values["Cached"] + values.get("SReclaimable", 0),
        values["SwapTotal"],
        values["SwapFree"],
    )


def wifi_dbm(wireless: str) -> float | None:
    """Signal level of the first interface in /proc/net/wireless, None without Wi-Fi."""
    for line in wireless.splitlines()[2:]:
        fields = line.split()
        if len(fields) > 3:
            return float(fields[3].rstrip("."))
    return None


def sockets(tables: list[str]) -> tuple[int, tuple[int, ...]]:
    """(established connections, listening ports) over /proc/net/tcp and tcp6."""
    established, listening = 0, set()
    for table in tables:
        for line in table.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4:
                continue
            if fields[3] == TCP_ESTABLISHED:
                established += 1
            elif fields[3] == TCP_LISTEN:
                listening.add(int(fields[1].rsplit(":", 1)[1], 16))
    return established, tuple(sorted(listening))


def udp_ports(tables: list[str]) -> tuple[int, ...]:
    ports = set()
    for table in tables:
        for line in table.splitlines()[1:]:
            fields = line.split()
            if (
                len(fields) > 2 and fields[2].rsplit(":", 1)[1] == "0000"
            ):  # nothing on the far side
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return tuple(sorted(ports))


def read_or_empty(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def clock_mhz(sys: Path = SYS) -> tuple[int, int, int] | None:
    folder = sys / "devices" / "system" / "cpu" / "cpu0" / "cpufreq"
    try:
        return tuple(
            int((folder / f"scaling_{kind}_freq").read_text()) // 1000
            for kind in ("cur", "min", "max")
        )
    except (OSError, ValueError):
        return None


def gauges(proc: Path = PROC, sys: Path = SYS) -> Gauges:
    net = proc / "net"
    established, listening = sockets(
        [read_or_empty(net / "tcp"), read_or_empty(net / "tcp6")]
    )
    one, five, fifteen = (float(v) for v in (proc / "loadavg").read_text().split()[:3])
    return Gauges(
        memory((proc / "meminfo").read_text()),
        (one, five, fifteen),
        clock_mhz(sys),
        wifi_dbm(read_or_empty(net / "wireless")),
        established,
        listening,
        udp_ports([read_or_empty(net / "udp"), read_or_empty(net / "udp6")]),
    )


def usb_devices(sys: Path = SYS) -> tuple[str, ...]:
    """Every USB device but the root hubs: known ones by name, others as 'Maker Product (vid:pid)'."""
    names = []
    for device in sorted((sys / "bus" / "usb" / "devices").glob("*-*")):
        if ":" in device.name:  # an interface of a device, not a device
            continue
        vendor, product = (
            read_or_empty(device / "idVendor").strip(),
            read_or_empty(device / "idProduct").strip(),
        )
        label = " ".join(
            part
            for part in (
                read_or_empty(device / "manufacturer").strip(),
                read_or_empty(device / "product").strip(),
            )
            if part
        )
        ids = f"{vendor}:{product}"
        names.append(USB_NAMES.get(ids) or f"{label or 'sem nome'} ({ids})")
    return tuple(names)


def command_label(name: str, cmdline: str) -> str:
    """'python3' alone says nothing; 'python3 screen.py' says which program it is."""
    args = [arg for arg in cmdline.split("\0") if arg]
    if name.startswith(INTERPRETERS) and len(args) > 1:
        script = next((arg for arg in args[1:] if not arg.startswith("-")), "")
        return f"{name} {Path(script).name}" if script else name
    return name


def named(top: tuple[Busy, ...], proc: Path = PROC) -> tuple[Busy, ...]:
    return tuple(
        Busy(
            b.pid,
            command_label(b.name, read_or_empty(proc / str(b.pid) / "cmdline")),
            b.cpu,
            b.rss_kb,
        )
        for b in top
    )


def distro(os_release: str) -> str:
    for line in os_release.splitlines():
        key, _, value = line.partition("=")
        if key == "PRETTY_NAME":
            return value.strip('"')
    return "?"


def system(
    now: datetime, proc: Path = PROC, sys: Path = SYS, etc: Path = Path("/etc")
) -> System:
    uptime = float((proc / "uptime").read_text().split()[0])
    return System(
        os.uname().release,
        distro(read_or_empty(etc / "os-release")),
        now - timedelta(seconds=uptime),
        usb_devices(sys),
    )


def port_label(port: int) -> str:
    name = PORT_NAMES.get(port)
    return f"{port} {name}" if name else str(port)


def selftest() -> None:
    stat_a = "cpu  1 1 1 1 1 0 0 0 0 0\ncpu0 100 0 50 800 50 0 0 0 0 0\ncpu1 10 0 0 90 0 0 0 0 0 0\nintr 5000 1 2\nctxt 9000\n"
    stat_b = "cpu  1 1 1 1 1 0 0 0 0 0\ncpu0 150 0 100 900 50 0 0 0 0 0\ncpu1 10 0 0 190 0 0 0 0 0 0\nintr 5600 1 2\nctxt 9400\n"
    cores, switches, interrupts = cpu_counters(stat_a)
    assert cores == ((150, 1000), (10, 100)) and switches == 9000 and interrupts == 5000
    disk = " 179       0 mmcblk0 12470 3379 1052014 67654 13985 15096 1030314 1079747 0 102668\n 179       1 mmcblk0p1 342 17 27332 2205 3 0 10 10 0 1592"
    assert disk_counters(disk) == (
        12470,
        1052014,
        13985,
        1030314,
    ), "the whole card, not a partition"
    dev = (
        "Inter-|   Receive\n face |bytes    packets\n"
        "    lo: 900 9 0 0 0 0 0 0 900 9 0 0 0 0 0 0\n"
        " wlan0: 1000 10 0 0 0 0 0 0 400 4 0 0 0 0 0 0\n"
        "  eth0: 24 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
    )
    assert network_counters(dev) == (
        1024,
        400,
    ), "loopback traffic is not internet traffic"
    line = "812 (python3 screen) S 1 812 812 0 -1 4194560 8 0 0 0 250 50 0 0 20 0 5 0 900 300000 9000 4294967295"
    assert process_from_stat(line, 4) == Process(812, "python3 screen", 300, 36000)
    old = Counters(
        10.0,
        cores,
        switches,
        interrupts,
        (100, 2000, 50, 4000),
        (1000, 400),
        (Process(1, "init", 10, 100), Process(812, "py", 300, 9000)),
    )
    new_cores, new_switches, new_interrupts = cpu_counters(stat_b)
    new = Counters(
        12.0,
        new_cores,
        new_switches,
        new_interrupts,
        (110, 4048, 70, 8096),
        (5096, 2448),
        (
            Process(812, "py", 400, 9100),
            Process(1, "init", 10, 100),
            Process(999, "new", 50, 1),
        ),
    )
    result = rates(old, new, ticks=100)
    assert result.cores == (0.5, 0.0), result.cores
    assert result.switches == 200 and result.interrupts == 300
    assert (
        result.read_bytes == 1024 * SECTOR_BYTES
        and result.write_bytes == 2048 * SECTOR_BYTES
    )
    assert result.read_ops == 5 and result.write_ops == 10
    assert result.down_bytes == 2048 and result.up_bytes == 1024
    assert [b.name for b in result.top] == [
        "py",
        "init",
    ], "a process born between samples has no rate yet"
    assert result.top[0].cpu == 0.5
    meminfo = "MemTotal: 926000 kB\nMemFree: 100000 kB\nBuffers: 20000 kB\nCached: 300000 kB\nSReclaimable: 6000 kB\nSwapTotal: 512000 kB\nSwapFree: 500000 kB\n"
    mem = memory(meminfo)
    assert mem.cached == 306000 and mem.used == 500000
    wireless = "Inter-| sta-|   Quality\n face | tus | link level noise\n wlan0: 0000   70.  -20.  -256        0      0      0    176      0        0\n"
    assert wifi_dbm(wireless) == -20 and wifi_dbm(wireless.split("\n wlan0")[0]) is None
    tcp = (
        "  sl  local_address rem_address   st\n"
        "   0: 00000000:0016 00000000:0000 0A 00000000:00000000\n"
        "   1: 0100007F:1F51 00000000:0000 0A 00000000:00000000\n"
        "   2: B101A8C0:0016 6501A8C0:D2F0 01 00000000:00000000\n"
    )
    assert sockets([tcp, tcp]) == (
        2,
        (22, 8017),
    ), "ports listed once across tcp and tcp6"
    udp = "  sl  local_address rem_address   st\n   0: 00000000:14E9 00000000:0000 07\n   1: B101A8C0:9C40 08080808:0035 01\n"
    assert udp_ports([udp]) == (5353,)
    assert (
        distro('NAME="Debian"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
        == "Debian GNU/Linux 13 (trixie)"
    )
    assert port_label(22) == "22 ssh" and port_label(4000) == "4000"
    assert (
        command_label(
            "python3", "/usr/bin/python3\0-u\0/home/iklo/dashboard/screen.py\0run\0"
        )
        == "python3 screen.py"
    )
    assert (
        command_label("python3", "") == "python3"
        and command_label("cupsd", "/usr/sbin/cupsd\0-l\0") == "cupsd"
    )
    if PROC.joinpath("stat").exists():  # on the Pi: one real pass end to end
        first = sample(0.0)
        second = sample(1.0)
        live = rates(first, second)
        assert len(live.cores) == os.cpu_count() and all(
            0 <= c <= 1 for c in live.cores
        )
        assert gauges().memory.total > 0
        assert system(datetime.now()).kernel
    print("procfs selftest ok")


if __name__ == "__main__":
    selftest()

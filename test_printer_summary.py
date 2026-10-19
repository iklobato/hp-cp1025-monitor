import io
import json
import tempfile
from contextlib import contextmanager, nullcontext
import threading
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from rich.console import Console

import history
import hpstatus
from hpstatus import UsbError
import macos_usb
import web_dashboard
from dashboard import live_layout, render
from viewmodel import (
    build_payload,
    humanize,
    humanize_queue,
    port_bits,
    revision_summary,
    short_hop,
)
from printer_model import (
    LEDM_PATHS,
    SEVERITY_ERROR,
    SEVERITY_OK,
    SEVERITY_WARNING,
    Consumable,
    PrinterReader,
    Report,
    capacity_impressions,
    event_hint,
    parse,
    read_consumables,
    years_since,
)
from poller import MIN_INTERVAL_SECONDS, Poller, Reading, interval_after
from printer_summary import as_json

NS = 'xmlns:dd="http://www.hp.com/schemas/imaging/con/dictionaries/1.0/"'

CONFIG = f"""<ccdyn:ConsumableConfigDyn xmlns:ccdyn="urn:c" {NS}>
<ccdyn:ConsumableInfo><dd:ConsumableLabelCode>C</dd:ConsumableLabelCode>
<dd:ConsumableLifeState><dd:ConsumableState>missingMemory</dd:ConsumableState></dd:ConsumableLifeState>
<dd:ProductNumber>CE311A</dd:ProductNumber></ccdyn:ConsumableInfo>
<ccdyn:ConsumableInfo><dd:ConsumableLabelCode>M</dd:ConsumableLabelCode>
<dd:ConsumableLifeState><dd:ConsumableState>ok</dd:ConsumableState></dd:ConsumableLifeState>
<dd:ConsumablePercentageLevelRemaining>15</dd:ConsumablePercentageLevelRemaining>
<dd:ProductNumber>CE313A</dd:ProductNumber></ccdyn:ConsumableInfo>
</ccdyn:ConsumableConfigDyn>"""

USAGE = f"""<pudyn:ProductUsageDyn xmlns:pudyn="urn:u" {NS}>
<pudyn:ConsumableSubunit><pudyn:Consumable><dd:MarkerColor>Magenta</dd:MarkerColor>
<dd:TotalImpressions>447</dd:TotalImpressions>
<dd:EstimatedPagesRemaining>300</dd:EstimatedPagesRemaining>
<dd:ConsumableRawPercentageLevelRemaining>42</dd:ConsumableRawPercentageLevelRemaining>
</pudyn:Consumable></pudyn:ConsumableSubunit></pudyn:ProductUsageDyn>"""

PORT_OK = {"raw": "0x18", "selected": True, "error": False, "paper_empty": False}


def reading_of(report, changes=(), number=1, read_ms=1.0):
    return Reading(
        report=report,
        entries=[],
        prediction=history.Forecast(None, None, 0, 0),
        changes=tuple(changes),
        number=number,
        read_ms=read_ms,
        next_in=MIN_INTERVAL_SECONDS,
    )


def sample_report(alerts=(), read_at=datetime(2026, 10, 18, 12, 0), total=10, color=4):
    return Report(
        device_id="MDL:test",
        port=PORT_OK,
        queue="printer x disabled",
        read_at=read_at,
        alerts=list(alerts),
        consumables=read_consumables(
            parse(CONFIG, "ConsumableConfigDyn"), parse(USAGE, "ProductUsageDyn")
        ),
        counters={
            "total": total,
            "mono": total - color,
            "color": color,
            "jams": 0,
            "mispicks": 0,
        },
        events=[(1, "10.1001", "10")],
    )


def test_merges_config_and_usage_by_color():
    items = {c.color: c for c in sample_report().consumables}
    assert items["Magenta"].percent == 15
    assert items["Magenta"].impressions == 447
    assert items["Magenta"].pages_remaining == 300
    assert items["Magenta"].raw_percent == 42
    assert items["Cyan"].impressions is None


def test_low_level_and_bad_chip_need_attention_but_healthy_does_not():
    assert Consumable("Magenta", state="ok", percent=15).needs_attention(20)
    assert "CE313A" in Consumable(
        "Magenta", product_number="CE313A", state="ok", percent=15
    ).advice(20)
    assert "chip" in Consumable("Cyan", state="missingMemory").advice(20)
    assert not Consumable("Magenta", state="ok", percent=40).needs_attention(20)


def test_severity_error_for_bad_chip_warning_for_low_only_ok_when_healthy():
    assert sample_report().severity(20) == SEVERITY_ERROR
    low_only = sample_report()
    low_only.consumables = [Consumable("Magenta", state="ok", percent=15)]
    assert low_only.severity(20) == SEVERITY_WARNING
    healthy = sample_report()
    healthy.consumables = [Consumable("Magenta", state="ok", percent=60)]
    assert healthy.severity(20) == SEVERITY_OK


def test_parse_rejects_stale_response_for_another_resource():
    try:
        parse(USAGE, "ConsumableConfigDyn")
    except ValueError:
        return
    raise AssertionError("stale response accepted")


def test_ledm_paths_match_their_root_elements():
    assert LEDM_PATHS["usage"].endswith("ProductUsageDyn.xml")


def test_capacity_scales_unit_prefix():
    assert capacity_impressions("6", "100ImpressionsAt5PercentCoverage") == 600
    assert capacity_impressions("2", "10KImpressionsAt5PercentCoverage") == 20000
    assert capacity_impressions("0", "100ImpressionsAt5PercentCoverage") is None


def test_event_hint_names_cartridge_only_for_supply_family():
    assert "Amarelo" in event_hint("10.1003")
    assert "Ciano" in event_hint("10.1001")
    assert event_hint("99.0B00") == "sem descrição conhecida"


def test_years_since_handles_missing_date():
    today = date(2026, 10, 18)
    assert years_since("2016-10-18", today) == 10.0
    assert years_since("", today) is None


def test_history_appends_only_on_change_and_forecasts_by_color_rate():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "h.jsonl"
        start = datetime(2026, 10, 1)
        history.record(sample_report(read_at=start, total=100, color=40), path)
        history.record(
            sample_report(read_at=start + timedelta(hours=1), total=100, color=40), path
        )
        entries = history.record(
            sample_report(read_at=start + timedelta(days=10), total=200, color=60), path
        )
        assert len(entries) == 2
        prediction = history.forecast(entries)
        assert prediction.pages_per_day == 10
        assert prediction.color_pages_per_day == 2
        magenta = Consumable("Magenta", pages_remaining=300)
        black = Consumable("Black", pages_remaining=300)
        assert prediction.days_left(magenta) == 150
        assert prediction.days_left(black) == 30


def test_forecast_needs_a_real_window():
    entries = [
        {"read_at": "2026-10-18T10:00:00", "total_pages": 10},
        {"read_at": "2026-10-18T11:00:00", "total_pages": 12},
    ]
    assert history.forecast(entries).pages_per_day is None


def test_sparkline_maps_levels_and_gaps():
    assert history.sparkline([0, 100, None]) == "▁█·"
    assert history.sparkline([None]) == ""


def test_render_full_report_without_printer():
    console = Console(file=io.StringIO(), width=110, record=True)
    render(
        reading_of(sample_report()),
        20,
        console,
    )
    out = console.export_text()
    assert "COM PROBLEMAS" in out
    assert "chip não lido" in out
    assert "compre CE313A" in out
    assert "cupsenable" in out


def test_default_view_hides_details_and_full_shows_them():
    short = Console(file=io.StringIO(), width=110, record=True)
    render(
        reading_of(sample_report()),
        20,
        short,
    )
    full = Console(file=io.StringIO(), width=110, record=True)
    render(reading_of(sample_report()), 20, full, full=True)
    assert "Log de eventos" not in short.export_text()
    assert "Log de eventos" in full.export_text()


def test_no_cell_is_truncated_at_80_or_110_columns():
    for width in (80, 110):
        console = Console(file=io.StringIO(), width=width, record=True)
        render(
            reading_of(sample_report()),
            20,
            console,
            full=True,
        )
        assert "…" not in console.export_text(), width


def test_humanize_firmware_tokens_and_cups_queue():
    assert humanize("15_minutes") == "15 min"
    assert humanize("48hours") == "48 h"
    assert humanize("brazil") == "Brasil"
    assert humanize("waitForPaperToBeLoaded (15 s)") == "espera colocar papel (15 s)"
    line = "printer X disabled since Sun Oct 18 21:06:19 2026 - Unable to send data to printer."
    assert (
        humanize_queue(line)
        == "pausada desde 18/10 21:06 · motivo: Unable to send data to printer."
    )
    assert humanize_queue("printer X is idle.  enabled since today") == "ativa, ociosa"


def test_live_layout_renders_at_terminal_size():
    console = Console(file=io.StringIO(), width=140, height=50, record=True)
    console.print(live_layout(reading_of(sample_report()), 20, "rodape"))
    out = console.export_text()
    assert "Níveis" in out and "O que fazer agora" in out and "rodape" in out


def test_changes_since_reports_pages_levels_states_alerts_and_queue():
    before = sample_report(total=100, color=40)
    after = sample_report(alerts=[("Error", "jamInPrinter", "")], total=103, color=40)
    after.queue = "printer x is idle."
    magenta = next(c for c in after.consumables if c.color == "Magenta")
    magenta.percent = 14
    cyan = next(c for c in after.consumables if c.color == "Cyan")
    cyan.state = "ok"
    changes = after.changes_since(before)
    assert "+3 página(s) impressa(s), total 103" in changes
    assert "Magenta: nível 15% → 14%" in changes
    assert "Ciano: chip não lido → ok" in changes
    assert "novo alerta: papel atolado" in changes
    assert "fila do Mac reativada" in changes


def test_changes_since_is_empty_for_identical_readings():
    assert sample_report().changes_since(sample_report()) == []


class ScriptedReader:
    """Returns the queued reports in order; a queued exception is raised instead."""

    def __init__(self, *results):
        self.results = list(results)

    def read(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class SteppingClock:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def test_poller_feeds_changes_counts_readings_and_paces_by_read_time():
    first = sample_report(total=100)
    second = sample_report(total=102)
    recorded = []
    poller = Poller(
        reader=ScriptedReader(first, second),
        record=lambda report: recorded.append(history.snapshot(report))
        or list(recorded),
        clock=SteppingClock(0.5),
    )
    one = poller.tick()
    two = poller.tick()
    assert (one.number, two.number) == (1, 2)
    assert one.changes == ()
    assert two.changes == ((second.read_at, "+2 página(s) impressa(s), total 102"),)
    assert two.read_ms == 500 and two.next_in == interval_after(0.5) == 2.0
    assert [e["total_pages"] for e in two.entries] == [100, 102]


def test_poller_failure_propagates_and_keeps_last_good_reading_as_diff_base():
    poller = Poller(
        reader=ScriptedReader(
            sample_report(total=100),
            UsbError("bulk write", -7),
            sample_report(total=101),
        ),
        record=lambda report: [],
    )
    poller.tick()
    try:
        poller.tick()
        raise AssertionError("USB failure swallowed")
    except UsbError:
        pass
    third = poller.tick()
    assert third.number == 2
    assert [text for _, text in third.changes] == [
        "+1 página(s) impressa(s), total 101"
    ]


def test_interval_never_below_minimum_and_backs_off_on_slow_reads():
    assert interval_after(0.2) == MIN_INTERVAL_SECONDS
    assert interval_after(0.5) == 2.0


def test_live_layout_shows_change_feed_only_in_live_mode():
    feed = [(datetime(2026, 10, 18, 22, 0, 1), "+1 página(s) impressa(s), total 11")]
    live = Console(file=io.StringIO(), width=150, height=50, record=True)
    live.print(live_layout(reading_of(sample_report()), 20, "rodape", feed))
    watch = Console(file=io.StringIO(), width=150, height=50, record=True)
    watch.print(live_layout(reading_of(sample_report()), 20, "rodape"))
    live_text = live.export_text()
    assert "Mudanças ao vivo" in live_text and "22:00:01" in live_text
    assert "Mudanças ao vivo" not in watch.export_text()


def test_port_bits_decodes_ieee1284_status_byte():
    assert port_bits("0x18") == "0x18 = 0001 1000 → online, sem erro, com papel"
    assert port_bits("0x30").endswith("online, ERRO, SEM PAPEL")


def test_revision_summary_names_only_outliers():
    revisions = {
        "ProductStatusDyn": "SVN.29",
        "ProductLogsDyn": "SVN.29",
        "ProductUsageDyn": "SVN.66",
    }
    assert revision_summary(revisions) == "29 (2 recursos) · uso 66"
    assert revision_summary({}) == ""


CONFIG_DESCRIPTOR = bytes(
    [9, 2, 39, 0, 2, 1, 0, 0xC0, 49]
    + [9, 4, 0, 0, 1, 7, 1, 2, 0]
    + [7, 5, 0x01, 2, 0x00, 0x02, 0]
    + [9, 4, 1, 0, 3, 255, 4, 1, 0]
    + [7, 5, 0x02, 2, 0x00, 0x02, 0]
    + [7, 5, 0x82, 2, 0x00, 0x02, 0]
    + [7, 5, 0x83, 3, 8, 0, 8]
)


def test_endpoint_map_parses_all_endpoints_including_interrupt():
    interfaces = hpstatus.parse_endpoint_map(CONFIG_DESCRIPTOR)
    assert [i["class"] for i in interfaces] == [(7, 1, 2), (255, 4, 1)]
    interrupt = interfaces[1]["endpoints"][2]
    assert interrupt == {
        "address": 0x83,
        "direction": "IN",
        "type": "interrupt",
        "max_packet": 8,
        "interval": 8,
    }
    assert interfaces[0]["endpoints"][0]["max_packet"] == 512


class FakeUsbPrinter:
    """In-memory stand-in for UsbPrinter: queued IN packets, recorded OUT writes."""

    def __init__(self, packets):
        self.packets = list(packets)
        self.writes = []
        self.claims = []

    def endpoint_map(self):
        return hpstatus.parse_endpoint_map(CONFIG_DESCRIPTOR)

    @contextmanager
    def claimed(self, interface, alt=0):
        self.claims.append(interface)
        yield

    def bulk_write(self, endpoint, payload):
        self.writes.append((endpoint, payload))

    def bulk_read(self, endpoint, timeout_ms):
        return self.packets.pop(0) if self.packets else None


class LedmFakePrinter(FakeUsbPrinter):
    """Answers each LEDM GET with the document registered for that path (404 otherwise)."""

    def __init__(self, documents):
        super().__init__([])
        self.documents = documents

    def device_id(self):
        return "MFG:Hewlett-Packard;CMD:ZJS;"

    def port_status(self):
        return PORT_OK

    def link_info(self):
        return {"speed": "high (480 Mb/s)"}

    def bulk_write(self, endpoint, payload):
        path = payload.split(b" ")[1].decode()
        body = self.documents.get(path)
        if body is None:
            self.packets = [b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"]
        else:
            data = body.encode()
            self.packets = [
                b"HTTP/1.1 200 OK\r\nServer: Mrvl-R1_0\r\nContent-Length: "
                + str(len(data)).encode()
                + b"\r\n\r\n"
                + data
            ]


STATUS = f"""<psdyn:ProductStatusDyn xmlns:psdyn="urn:s" {NS}>
<dd:Version><dd:Revision>SVN.29</dd:Revision></dd:Version>
<psdyn:Status><pscat:StatusCategory xmlns:pscat="urn:p">missingMemory</pscat:StatusCategory></psdyn:Status>
<psdyn:AlertTable><psdyn:Alert><ad:ProductStatusAlertID xmlns:ad="urn:a">missingMemory</ad:ProductStatusAlertID>
<ad:Severity xmlns:ad="urn:a">Error</ad:Severity></psdyn:Alert></psdyn:AlertTable>
</psdyn:ProductStatusDyn>"""


def test_printer_reader_builds_report_from_injected_sources_without_hardware():
    printer = LedmFakePrinter(
        {
            LEDM_PATHS["status"]: STATUS,
            LEDM_PATHS["consumables"]: CONFIG,
            LEDM_PATHS["usage"]: USAGE,
        }
    )
    reader = PrinterReader(
        connect=lambda: nullcontext(printer),
        topology=lambda: {"port": 4},
        queue_state=lambda: "printer x is idle.",
        clock=lambda: datetime(2026, 10, 18, 12, 0),
    )
    report = reader.read()
    assert report.status_category == "missingMemory"
    assert report.alerts == [("Error", "missingMemory", "")]
    assert {c.color: c.percent for c in report.consumables} == {
        "Cyan": None,
        "Magenta": 15,
    }
    assert report.topology == {"port": 4} and report.queue == "printer x is idle."
    assert (
        report.http_server == "Mrvl-R1_0"
        and report.revisions["ProductStatusDyn"] == "SVN.29"
    )
    missing = {LEDM_PATHS[k] for k in ("config", "logs", "events", "media", "print")}
    assert {error.split(":")[0] for error in report.errors} == missing


def test_ledm_client_drains_stale_bytes_and_reads_zero_padded_chunked_reply():
    reply = (
        b"HTTP/1.1 200 OK\r\nServer: Mrvl-R1_0\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"0005\r\n<a/>\n\r\n0000\r\n\r\n"
    )
    stale = [b"leftover of an older response", None]
    printer = FakeUsbPrinter([*stale, b"", reply[:40], b"", reply[40:]])
    response = hpstatus.LedmClient(printer).fetch("/DevMgmt/X.xml")
    assert printer.claims == [1]
    assert printer.writes == [
        (
            0x02,
            b"GET /DevMgmt/X.xml HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    ]
    assert response.body == "<a/>\n"
    assert response.server == "Mrvl-R1_0"
    assert response.status == "HTTP/1.1 200 OK"
    assert response.wire_bytes == len(reply)


def test_response_complete_accepts_zero_padded_last_chunk_only_at_the_end():
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    assert hpstatus.response_complete(head + b"0003\r\nabc\r\n0000\r\n\r\n")
    assert hpstatus.response_complete(head + b"0003\r\nabc\r\n0\r\n\r\n")
    assert not hpstatus.response_complete(head + b"0003\r\nabc\r\n")
    assert not hpstatus.response_complete(b"HTTP/1.1 200 OK\r\n")


def test_macos_topology_finds_printer_behind_hub_and_scales_bus_power():
    printer = {
        "idVendor": hpstatus.VENDOR_ID,
        "idProduct": hpstatus.PRODUCT_ID,
        "USB Address": 25,
        "PortNum": 4,
        "locationID": 0x14440000,
        "Device Speed": 2,
        "Bus Power Available": 250,
        "IOPowerManagement": {"CurrentPowerState": 3, "MaxPowerState": 4},
    }
    hub = {
        "USB Product Name": "USB 2.0 Hub",
        "idVendor": 0x1A40,
        "idProduct": 0x0101,
        "Device Speed": 2,
        "IORegistryEntryChildren": [printer],
    }
    tree = {"IORegistryEntryName": "Root", "IORegistryEntryChildren": [hub]}
    topo = macos_usb.topology(tree)
    assert topo["chain"] == ["USB 2.0 Hub (1a40:0101) · high 480 Mb/s"]
    assert topo["bus_power_available_ma"] == 500
    assert topo["location"] == "0x14440000" and topo["power_state"] == "3/4"
    assert macos_usb.topology({"IORegistryEntryName": "Root"}) == {}
    assert (
        short_hop("AppleUSBXHCI Root Hub Simulation (05ac:8007) · super 5 Gb/s")
        == "raiz XHCI"
    )


def hardware_report():
    report = sample_report()
    report.device_id = "MFG:Hewlett-Packard;CMD:ZJS,URF,PCLm,PJL,ACL,HTTP;CLS:PRINTER;FWVER:20121108;LEDMDIS:USB#ff#04#01;"
    report.http_server = "Mrvl-R1_0"
    report.usb = {
        "vendor_product": "03f0:112a",
        "usb_version": "2.00",
        "device_release": "0.01",
        "speed": "high (480 Mb/s)",
        "serial": "TESTSERIAL01",
        "max_power_ma": 98,
        "self_powered": True,
        "remote_wakeup": False,
        "configuration": 1,
        "interfaces": 2,
        "total_descriptor_bytes": 62,
        "ep0_max_packet": 64,
    }
    report.topology = {
        "chain": [
            "AppleUSBXHCI Root Hub Simulation (05ac:8007) · super 5 Gb/s",
            "USB 2.0 Hub (1a40:0101) · high 480 Mb/s",
        ],
        "address": 25,
        "port": 4,
        "location": "0x14440000",
        "bus_power_available_ma": 500,
        "power_state": "3/4",
    }
    report.revisions = {
        "ProductStatusDyn": "SVN.29",
        "ProductUsageDyn": "SVN.66",
        "ProductConfigDyn": "SVN.77",
    }
    report.latency_ms = {"/a": 20, "/b": 60}
    report.wire_bytes = {"/a": 2000, "/b": 7000}
    report.identity = {
        "Placa do sistema": "YL20BR1",
        "Placa do motor": "40",
        "Memória livre": "6 de 8 MB",
        "Firmware (data)": "2012-11-08",
    }
    report.device_events = [("AlertTableChanged", "176-29"), ("PowerUpEvent", "176-1")]
    return report


def test_hardware_panels_never_truncate_at_common_widths():
    for width in (80, 100, 120):
        console = Console(file=io.StringIO(), width=width, record=True)
        render(
            reading_of(hardware_report()),
            20,
            console,
            full=True,
        )
        out = console.export_text()
        assert "…" not in out, width
        assert "Mac ▸ raiz XHCI ▸ USB 2.0 Hub ▸ porta 4" in out
        assert "pede 98 de 500 mA" in out


def test_long_legend_lines_stay_on_one_line_at_80_columns():
    console = Console(file=io.StringIO(), width=80, record=True)
    render(reading_of(sample_report()), 20, console, full=True)
    lines = console.export_text().splitlines()
    assert any(
        "Bruto = leitura crua do sensor · Mod. = id gravado no chip" in line
        for line in lines
    )


def test_chip_ok_trusts_printer_state_over_present_tag():
    assert not Consumable("Yellow", state="missingMemory", chip_tag="DC7C/CF07").chip_ok
    assert Consumable("Cyan", state="ok", chip_tag="9383/B925").chip_ok
    assert not Consumable("Black", state="misinstalled", chip_tag="0000/0000").chip_ok


def test_web_payload_carries_actions_tanks_and_hardware():
    feed = [(datetime(2026, 10, 18, 22, 0, 1), "+1 página(s) impressa(s), total 11")]
    payload = build_payload(
        reading_of(hardware_report(), changes=feed, number=3, read_ms=250.4), 20
    )
    json.dumps(payload, ensure_ascii=False)
    assert (
        payload["severity"] == SEVERITY_ERROR
        and payload["reads"] == 3
        and payload["read_ms"] == 250
    )
    assert {
        "subject": "Ciano",
        "advice": "chip não lido: limpe os contatos e reencaixe",
        "color": "Cyan",
    } in payload["actions"]
    magenta = next(c for c in payload["consumables"] if c["color"] == "Magenta")
    assert magenta["low"] and magenta["percent"] == 15
    assert payload["usb_bus"]["Caminho"] == "Mac ▸ raiz XHCI ▸ USB 2.0 Hub ▸ porta 4"
    assert payload["changes"] == [
        {"at": "22:00:01", "text": "+1 página(s) impressa(s), total 11"}
    ]
    assert magenta["pages_text"] == "300" and magenta["impressions_text"] == "447"
    assert payload["counter_rows"]["Páginas no total"] == "10"
    assert payload["pace"] == "sem histórico ainda"
    assert {"label": "fila pausada", "ok": False} in payload["port_badges"]


def test_terminal_and_web_show_the_same_status_counter_and_badge_text():
    report = hardware_report()
    payload = build_payload(reading_of(report), 20)
    console = Console(file=io.StringIO(), width=150, record=True)
    render(reading_of(report), 20, console, full=True)
    terminal = console.export_text()
    shared = [
        *payload["status_rows"].values(),
        *payload["counter_rows"].values(),
        *(b["label"] for b in payload["port_badges"]),
        *payload["usb_bus"].values(),
        payload["verdict"],
    ]
    missing = [text for text in shared if text not in terminal]
    assert not missing, missing


def test_web_server_serves_page_status_and_sse_stream():
    feed = web_dashboard.PrinterFeed(20)
    feed.publish(json.dumps({"reads": 7}))
    server = ThreadingHTTPServer(("127.0.0.1", 0), web_dashboard.make_handler(feed))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert b"HP LaserJet CP1025" in urlopen(base + "/", timeout=5).read()
        assert json.loads(urlopen(base + "/api/status", timeout=5).read())["reads"] == 7
        stream = urlopen(base + "/events", timeout=5)
        assert (
            json.loads(stream.readline().decode().removeprefix("data: "))["reads"] == 7
        )
        feed.publish(json.dumps({"reads": 8}))
        stream.readline()
        assert (
            json.loads(stream.readline().decode().removeprefix("data: "))["reads"] == 8
        )
        try:
            urlopen(base + "/nao-existe", timeout=5)
            raise AssertionError("unknown path served")
        except HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_json_output_is_valid_and_carries_severity():
    payload = json.loads(as_json(reading_of(sample_report()), 20))
    assert payload["severity"] == SEVERITY_ERROR
    assert payload["consumables"][1]["product_number"] == "CE313A"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")

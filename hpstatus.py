"""USB transport for the HP LaserJet CP1025 (libusb-1.0 via ctypes).

UsbPrinter owns the device handle (descriptors, IEEE 1284 printer-class requests, raw bulk
transfers). LedmClient speaks HP's LEDM protocol, HTTP/1.1 over the vendor USB interface.

Usage: python3 hpstatus.py [LEDM_PATH ...]
       python3 hpstatus.py --put LEDM_PATH XML_FILE
Without paths it prints the IEEE 1284 device id, the printer-class port status and the
USB interface/endpoint map. Each LEDM_PATH (e.g. /DevMgmt/ConsumableConfigDyn.xml) is
fetched over LEDM and printed. --put sends XML_FILE as the new value of LEDM_PATH; the
matching ...Cap.xml lists which fields are readWrite and the values they take.
"""

import ctypes
import ctypes.util
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

VENDOR_ID = 0x03F0
PRODUCT_ID = 0x112A
TIMEOUT_MS = 5000
DRAIN_TIMEOUT_MS = 300
READ_CHUNK = 16384
MAX_EMPTY_READS = 5
CONFIG_DESCRIPTOR_MAX = 512
# This firmware zero-pads chunk sizes, so the last chunk is "0000", not "0".
CHUNKED_END = re.compile(rb"(^|\r\n)0+\r\n\r\n$")
LEDM_CLASS = (0xFF, 0x04)
ENDPOINT_TYPES = ("control", "isochronous", "bulk", "interrupt")
USB_SPEEDS = {
    1: "low (1.5 Mb/s)",
    2: "full (12 Mb/s)",
    3: "high (480 Mb/s)",
    4: "super (5 Gb/s)",
}

lib = ctypes.CDLL(
    ctypes.util.find_library("usb-1.0") or "/usr/local/lib/libusb-1.0.0.dylib"
)
lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
lib.libusb_open_device_with_vid_pid.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint16,
    ctypes.c_uint16,
]
lib.libusb_control_transfer.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint8,
    ctypes.c_uint8,
    ctypes.c_uint16,
    ctypes.c_uint16,
    ctypes.c_char_p,
    ctypes.c_uint16,
    ctypes.c_uint,
]
lib.libusb_bulk_transfer.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint8,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_uint,
]
lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.libusb_set_interface_alt_setting.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
]
lib.libusb_close.argtypes = [ctypes.c_void_p]
lib.libusb_get_device.restype = ctypes.c_void_p
lib.libusb_get_device.argtypes = [ctypes.c_void_p]
lib.libusb_get_device_speed.argtypes = [ctypes.c_void_p]
lib.libusb_get_string_descriptor_ascii.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint8,
    ctypes.c_char_p,
    ctypes.c_int,
]
lib.libusb_error_name.restype = ctypes.c_char_p
lib.libusb_error_name.argtypes = [ctypes.c_int]


class UsbError(RuntimeError):
    def __init__(self, what, code):
        super().__init__(f"{what}: {lib.libusb_error_name(code).decode()}")


def bcd(value):
    return f"{value >> 8:x}.{value & 0xFF:02x}"


def parse_endpoint_map(config_descriptor):
    """Every interface in a raw configuration descriptor, with all of its endpoints."""
    result, pos = [], 0
    raw = config_descriptor
    while pos + 1 < len(raw):
        length, kind = raw[pos], raw[pos + 1]
        if length == 0:
            break
        if kind == 4:
            result.append(
                {
                    "number": raw[pos + 2],
                    "alt": raw[pos + 3],
                    "class": (raw[pos + 5], raw[pos + 6], raw[pos + 7]),
                    "endpoints": [],
                }
            )
        elif kind == 5 and result:
            address = raw[pos + 2]
            result[-1]["endpoints"].append(
                {
                    "address": address,
                    "direction": "IN" if address & 0x80 else "OUT",
                    "type": ENDPOINT_TYPES[raw[pos + 3] & 0x03],
                    "max_packet": int.from_bytes(raw[pos + 4 : pos + 6], "little")
                    & 0x7FF,
                    "interval": raw[pos + 6],
                }
            )
        pos += length
    return result


def parse_port_status(byte):
    """Printer class GET_PORT_STATUS: bit3 = no error, bit4 = selected, bit5 = paper empty."""
    return {
        "raw": hex(byte),
        "paper_empty": bool(byte & 0x20),
        "selected": bool(byte & 0x10),
        "error": not byte & 0x08,
    }


class UsbPrinter:
    """Open libusb handle to the printer. Use as a context manager so it is always closed."""

    def __init__(self, handle):
        self._handle = handle

    @classmethod
    def open(cls, vendor_id=VENDOR_ID, product_id=PRODUCT_ID):
        if lib.libusb_init(None) != 0:
            raise RuntimeError("libusb_init failed")
        handle = lib.libusb_open_device_with_vid_pid(None, vendor_id, product_id)
        if not handle:
            raise RuntimeError("printer not found on USB")
        return cls(handle)

    def close(self):
        lib.libusb_close(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def control_in(self, request_type, request, value, index, length):
        buf = ctypes.create_string_buffer(length)
        n = lib.libusb_control_transfer(
            self._handle, request_type, request, value, index, buf, length, TIMEOUT_MS
        )
        if n < 0:
            raise UsbError(f"control request {request}", n)
        return buf.raw[:n]

    def string_descriptor(self, index):
        if not index:
            return ""
        buf = ctypes.create_string_buffer(256)
        n = lib.libusb_get_string_descriptor_ascii(self._handle, index, buf, 256)
        return buf.raw[:n].decode(errors="replace") if n > 0 else ""

    def device_id(self):
        """IEEE 1284 GET_DEVICE_ID (printer class request 0)."""
        return self.control_in(0xA1, 0, 0, 0, 1024)[2:].decode(errors="replace")

    def port_status(self):
        return parse_port_status(self.control_in(0xA1, 1, 0, 0, 1)[0])

    def endpoint_map(self):
        return parse_endpoint_map(
            self.control_in(0x80, 6, 0x0200, 0, CONFIG_DESCRIPTOR_MAX)
        )

    def link_info(self):
        """USB device + configuration descriptor fields and negotiated bus speed."""
        device = self.control_in(0x80, 6, 0x0100, 0, 18)
        config = self.control_in(0x80, 6, 0x0200, 0, 9)
        speed = lib.libusb_get_device_speed(lib.libusb_get_device(self._handle))
        return {
            "usb_version": bcd(int.from_bytes(device[2:4], "little")),
            "device_release": bcd(int.from_bytes(device[12:14], "little")),
            "vendor_product": f"{int.from_bytes(device[8:10], 'little'):04x}:"
            f"{int.from_bytes(device[10:12], 'little'):04x}",
            "manufacturer": self.string_descriptor(device[14]),
            "product": self.string_descriptor(device[15]),
            "serial": self.string_descriptor(device[16]),
            "ep0_max_packet": device[7],
            "interfaces": config[4],
            "self_powered": bool(config[7] & 0x40),
            "remote_wakeup": bool(config[7] & 0x20),
            "configuration": config[5],
            "total_descriptor_bytes": int.from_bytes(config[2:4], "little"),
            "max_power_ma": config[8] * 2,
            "speed": USB_SPEEDS.get(speed, f"unknown ({speed})"),
        }

    @contextmanager
    def claimed(self, interface, alt=0):
        rc = lib.libusb_claim_interface(self._handle, interface)
        if rc != 0:
            raise UsbError(f"claim interface {interface}", rc)
        try:
            if alt:
                lib.libusb_set_interface_alt_setting(self._handle, interface, alt)
            yield
        finally:
            lib.libusb_release_interface(self._handle, interface)

    def bulk_write(self, endpoint, payload):
        sent = ctypes.c_int()
        rc = lib.libusb_bulk_transfer(
            self._handle,
            endpoint,
            payload,
            len(payload),
            ctypes.byref(sent),
            TIMEOUT_MS,
        )
        if rc != 0:
            raise UsbError("bulk write", rc)

    def bulk_read(self, endpoint, timeout_ms):
        """One bulk IN transfer. b"" is a zero-length packet; None means nothing arrived in time."""
        buf, got = ctypes.create_string_buffer(READ_CHUNK), ctypes.c_int()
        rc = lib.libusb_bulk_transfer(
            self._handle, endpoint, buf, READ_CHUNK, ctypes.byref(got), timeout_ms
        )
        return buf.raw[: got.value] if rc == 0 else None


@dataclass(frozen=True)
class LedmResponse:
    status: str
    server: str
    wire_bytes: int
    body: str


class LedmClient:
    """HTTP/1.1 GET and PUT over the LEDM vendor interface (class ff/04) of a UsbPrinter."""

    def __init__(self, printer):
        self._printer = printer
        ledm = next(
            i for i in printer.endpoint_map() if tuple(i["class"][:2]) == LEDM_CLASS
        )
        bulk = [e for e in ledm["endpoints"] if e["type"] == "bulk"]
        self._interface, self._alt = ledm["number"], ledm["alt"]
        self._out = next(e["address"] for e in bulk if e["direction"] == "OUT")
        self._in = next(e["address"] for e in bulk if e["direction"] == "IN")

    def get(self, path):
        return self.fetch(path).body

    def fetch(self, path):
        return self._send(
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )

    def put(self, path, xml):
        """Write the fields in xml; the firmware answers 200 with an empty body."""
        head = (
            f"PUT {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: text/xml\r\n"
            f"Content-Length: {len(xml)}\r\nConnection: close\r\n\r\n"
        )
        return self._send(head.encode() + xml)

    def _send(self, request):
        raw = self._exchange(request)
        server = re.search(
            rb"^server:\s*(.+?)\r?$", raw.partition(b"\r\n\r\n")[0], re.I | re.M
        )
        return LedmResponse(
            status=http_status(raw),
            server=server.group(1).decode(errors="replace") if server else "",
            wire_bytes=len(raw),
            body=http_body(raw),
        )

    def _exchange(self, request):
        with self._printer.claimed(self._interface, self._alt):
            self._drain()
            self._printer.bulk_write(self._out, request)
            # The device answers late and sends zero-length packets meanwhile, so read until
            # the response is complete or the deadline passes, never until the first empty read.
            data = b""
            deadline = time.monotonic() + TIMEOUT_MS / 1000
            while not response_complete(data) and time.monotonic() < deadline:
                chunk = self._printer.bulk_read(self._in, TIMEOUT_MS)
                if chunk is None:
                    break
                data += chunk
            return data

    def _drain(self):
        """Discard leftovers of an earlier response so the next read matches the next request."""
        empty_reads = 0
        while empty_reads < MAX_EMPTY_READS:
            chunk = self._printer.bulk_read(self._in, DRAIN_TIMEOUT_MS)
            if chunk is None:
                return
            empty_reads = 0 if chunk else empty_reads + 1


def response_complete(raw):
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return False
    if b"chunked" in head.lower():
        return CHUNKED_END.search(body) is not None
    match = re.search(rb"content-length:\s*(\d+)", head, re.IGNORECASE)
    return match is None or len(body) >= int(match.group(1))


def http_status(raw):
    return raw.split(b"\r\n", 1)[0].decode(errors="replace")


def http_body(raw):
    """Strip HTTP headers and undo chunked transfer encoding."""
    head, _, body = raw.partition(b"\r\n\r\n")
    if b"chunked" not in head.lower():
        return body.decode(errors="replace")
    out = b""
    while body:
        size_line, _, rest = body.partition(b"\r\n")
        size = int(size_line.split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out, body = out + rest[:size], rest[size + 2 :]
    return out.decode(errors="replace")


def put_from_file(path, xml_file):
    with open(xml_file, "rb") as source:
        xml = source.read()
    with UsbPrinter.open() as usb:
        response = LedmClient(usb).put(path, xml)
    print("PUT", path, response.status)
    return 0 if response.status.split(" ")[1:2] == ["200"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--put"]:
        if len(sys.argv) != 4:
            sys.exit(__doc__)
        sys.exit(put_from_file(sys.argv[2], sys.argv[3]))
    with UsbPrinter.open() as usb:
        print("ENDPOINTS:", usb.endpoint_map())
        print("DEVICE_ID:", usb.device_id())
        print("PORT_STATUS:", usb.port_status())
        ledm = LedmClient(usb)
        for p in sys.argv[1:]:
            print(f"===== {p}")
            try:
                print(ledm.get(p))
            except UsbError as e:
                print("ERROR", e)

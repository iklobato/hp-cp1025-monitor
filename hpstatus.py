"""Low-level status reader for the HP LaserJet CP1025 over USB (libusb-1.0 via ctypes).

Usage: python3 hpstatus.py [LEDM_PATH ...]
Without paths it prints the IEEE 1284 device id, the printer-class port status and the
USB interface/endpoint map. Each LEDM_PATH (e.g. /DevMgmt/ConsumableConfigDyn.xml) is
fetched with HTTP over the vendor LEDM interface.
"""

import ctypes
import ctypes.util
import re
import sys
import time

VENDOR_ID = 0x03F0
PRODUCT_ID = 0x112A
TIMEOUT_MS = 5000
DRAIN_TIMEOUT_MS = 300
READ_CHUNK = 16384
MAX_EMPTY_READS = 5
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


def open_printer():
    if lib.libusb_init(None) != 0:
        raise RuntimeError("libusb_init failed")
    handle = lib.libusb_open_device_with_vid_pid(None, VENDOR_ID, PRODUCT_ID)
    if not handle:
        raise RuntimeError("printer not found on USB")
    return handle


def control_in(handle, request_type, request, value, index, length):
    buf = ctypes.create_string_buffer(length)
    n = lib.libusb_control_transfer(
        handle, request_type, request, value, index, buf, length, TIMEOUT_MS
    )
    if n < 0:
        raise UsbError(f"control request {request}", n)
    return buf.raw[:n]


def bcd(value):
    return f"{value >> 8:x}.{value & 0xFF:02x}"


def string_descriptor(handle, index):
    if not index:
        return ""
    buf = ctypes.create_string_buffer(256)
    n = lib.libusb_get_string_descriptor_ascii(handle, index, buf, 256)
    return buf.raw[:n].decode(errors="replace") if n > 0 else ""


def link_info(handle):
    """USB device + configuration descriptor fields and negotiated bus speed."""
    device = control_in(handle, 0x80, 6, 0x0100, 0, 18)
    config = control_in(handle, 0x80, 6, 0x0200, 0, 9)
    speed = lib.libusb_get_device_speed(lib.libusb_get_device(handle))
    return {
        "usb_version": bcd(int.from_bytes(device[2:4], "little")),
        "device_release": bcd(int.from_bytes(device[12:14], "little")),
        "vendor_product": f"{int.from_bytes(device[8:10], 'little'):04x}:"
        f"{int.from_bytes(device[10:12], 'little'):04x}",
        "manufacturer": string_descriptor(handle, device[14]),
        "product": string_descriptor(handle, device[15]),
        "serial": string_descriptor(handle, device[16]),
        "ep0_max_packet": device[7],
        "interfaces": config[4],
        "self_powered": bool(config[7] & 0x40),
        "remote_wakeup": bool(config[7] & 0x20),
        "configuration": config[5],
        "total_descriptor_bytes": int.from_bytes(config[2:4], "little"),
        "max_power_ma": config[8] * 2,
        "speed": USB_SPEEDS.get(speed, f"unknown ({speed})"),
    }


def endpoint_map(handle):
    """Every interface with all of its endpoints (address, direction, type, packet size, interval)."""
    raw = control_in(handle, 0x80, 6, 0x0200, 0, 512)
    result, pos = [], 0
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


def interfaces(handle):
    """Parse the raw configuration descriptor into {(iface, alt): {class, out, in}}."""
    raw = control_in(handle, 0x80, 6, 0x0200, 0, 512)
    result, current, pos = {}, None, 0
    while pos + 1 < len(raw):
        length, kind = raw[pos], raw[pos + 1]
        if length == 0:
            break
        if kind == 4:
            current = (raw[pos + 2], raw[pos + 3])
            result[current] = {
                "class": (raw[pos + 5], raw[pos + 6], raw[pos + 7]),
                "out": None,
                "in": None,
            }
        elif kind == 5 and current is not None and raw[pos + 3] & 0x03 == 2:
            address = raw[pos + 2]
            result[current]["in" if address & 0x80 else "out"] = address
        pos += length
    return result


def device_id(handle):
    """IEEE 1284 GET_DEVICE_ID (printer class request 0)."""
    raw = control_in(handle, 0xA1, 0, 0, 0, 1024)
    return raw[2:].decode(errors="replace")


def port_status(handle):
    """Printer class GET_PORT_STATUS: bit3 = no error, bit4 = selected, bit5 = paper empty."""
    b = control_in(handle, 0xA1, 1, 0, 0, 1)[0]
    return {
        "raw": hex(b),
        "paper_empty": bool(b & 0x20),
        "selected": bool(b & 0x10),
        "error": not b & 0x08,
    }


def drain(handle, ep_in):
    """Discard leftovers of an earlier response so the next read matches the next request."""
    buf, got = ctypes.create_string_buffer(READ_CHUNK), ctypes.c_int()
    empty_reads = 0
    while empty_reads < MAX_EMPTY_READS:
        rc = lib.libusb_bulk_transfer(
            handle, ep_in, buf, READ_CHUNK, ctypes.byref(got), DRAIN_TIMEOUT_MS
        )
        if rc != 0:
            return
        empty_reads = 0 if got.value else empty_reads + 1


def bulk_exchange(handle, iface, alt, ep_out, ep_in, payload):
    rc = lib.libusb_claim_interface(handle, iface)
    if rc != 0:
        raise UsbError(f"claim interface {iface}", rc)
    try:
        if alt:
            lib.libusb_set_interface_alt_setting(handle, iface, alt)
        drain(handle, ep_in)
        sent = ctypes.c_int()
        rc = lib.libusb_bulk_transfer(
            handle, ep_out, payload, len(payload), ctypes.byref(sent), TIMEOUT_MS
        )
        if rc != 0:
            raise UsbError("bulk write", rc)
        # The device answers late and sends zero-length packets meanwhile, so read until the
        # response is complete or the deadline passes, never until the first empty read.
        data, buf, got = b"", ctypes.create_string_buffer(READ_CHUNK), ctypes.c_int()
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while not response_complete(data) and time.monotonic() < deadline:
            rc = lib.libusb_bulk_transfer(
                handle, ep_in, buf, READ_CHUNK, ctypes.byref(got), TIMEOUT_MS
            )
            if rc != 0:
                break
            data += buf.raw[: got.value]
        return data
    finally:
        lib.libusb_release_interface(handle, iface)


def ledm_get(handle, layout, path):
    return ledm_fetch(handle, layout, path)["body"]


def ledm_fetch(handle, layout, path):
    """GET over LEDM; returns body plus wire metadata (status line, Server header, bytes)."""
    (iface, alt), info = next(
        (k, v) for k, v in layout.items() if v["class"][:2] == LEDM_CLASS
    )
    request = (
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    )
    raw = bulk_exchange(handle, iface, alt, info["out"], info["in"], request)
    server = re.search(
        rb"^server:\s*(.+?)\r?$", raw.partition(b"\r\n\r\n")[0], re.I | re.M
    )
    return {
        "status": http_status(raw),
        "server": server.group(1).decode(errors="replace") if server else "",
        "wire_bytes": len(raw),
        "body": http_body(raw),
    }


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


if __name__ == "__main__":
    h = open_printer()
    try:
        layout = interfaces(h)
        print("INTERFACES:", layout)
        print("DEVICE_ID:", device_id(h))
        print("PORT_STATUS:", port_status(h))
        for p in sys.argv[1:]:
            print(f"===== {p}")
            try:
                print(ledm_get(h, layout, p))
            except UsbError as e:
                print("ERROR", e)
    finally:
        lib.libusb_close(h)

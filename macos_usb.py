"""Where the printer sits on the Mac's USB tree, read from the IORegistry (`ioreg -a`)."""

import os
import plistlib
import shlex
import tempfile

from hpstatus import PRODUCT_ID, VENDOR_ID

# IOUSBHostFamily "Device Speed" enum.
MACOS_SPEEDS = {
    0: "low 1.5 Mb/s",
    1: "full 12 Mb/s",
    2: "high 480 Mb/s",
    3: "super 5 Gb/s",
    4: "super+ 10 Gb/s",
}
IOREG_COMMAND = "ioreg -a -p IOUSB -l -w0"


def read_tree():
    # os.system with shell redirection instead of subprocess (house rule).
    with tempfile.NamedTemporaryFile("rb", suffix=".plist") as out:
        if os.system(f"{IOREG_COMMAND} > {shlex.quote(out.name)} 2>/dev/null") != 0:
            return None
        try:
            return plistlib.load(out)
        except plistlib.InvalidFileException:
            return None


def find_path(node, parents=()):
    """Depth-first search for the printer; returns the chain [root .. printer]."""
    if node.get("idVendor") == VENDOR_ID and node.get("idProduct") == PRODUCT_ID:
        return [*parents, node]
    for child in node.get("IORegistryEntryChildren", []):
        found = find_path(child, (*parents, node))
        if found:
            return found
    return None


def describe(node):
    name = node.get("USB Product Name") or node.get("IORegistryEntryName", "?")
    vid, pid = node.get("idVendor"), node.get("idProduct")
    ids = f" ({vid:04x}:{pid:04x})" if vid is not None and pid is not None else ""
    speed = MACOS_SPEEDS.get(node.get("Device Speed"))
    return f"{name}{ids}" + (f" · {speed}" if speed else "")


def bus_power_ma(units):
    # IORegistry reports bus power in 2 mA units (250 here = the 500 mA system_profiler shows).
    return None if units is None else units * 2


def topology(tree):
    path = find_path(tree) if tree else None
    if not path:
        return {}
    printer = path[-1]
    power = printer.get("IOPowerManagement", {})
    return {
        "chain": [describe(n) for n in path[1:-1]],
        "address": printer.get("USB Address"),
        "port": printer.get("PortNum"),
        "location": f"0x{printer.get('locationID', 0):08x}",
        "speed": MACOS_SPEEDS.get(printer.get("Device Speed"), "?"),
        "bus_power_available_ma": bus_power_ma(printer.get("Bus Power Available")),
        "power_state": f"{power.get('CurrentPowerState', '?')}/{power.get('MaxPowerState', '?')}",
        "configuration": printer.get("kUSBCurrentConfiguration"),
        "registry_id": f"0x{printer.get('IORegistryEntryID', 0):x}",
    }


def usb_topology():
    return topology(read_tree())

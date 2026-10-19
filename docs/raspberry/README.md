# Network print server on a Raspberry Pi (plan, not started)

Goal: every computer and phone prints to the CP1025 over the network instead of plugging into its USB cable. The printer moves to a Raspberry Pi, which runs CUPS with the foo2zjs driver and advertises the printer as AirPrint / IPP Everywhere. Clients need no driver: they send PDF, the Pi converts it to ZJS.

```
Mac / iPhone / Android / Linux  --AirPrint/IPP-->  Raspberry Pi 3 (CUPS + foo2zjs + Avahi)  --USB-->  CP1025
```

Scope is network printing only. The status monitor in this repo is not part of the Pi setup.

Status on 2026-10-19: all decisions taken, nothing executed. Printing on paper is still blocked by the black and yellow cartridges (see [../FINDINGS.md](../FINDINGS.md)), so steps 0 to 4 can run now and step 5 waits for new cartridges.

## Decisions

| Topic | Choice |
|---|---|
| Hardware | Raspberry Pi 3 B/B+ on wired Ethernet; printer on a USB-A port (it is self-powered and draws 98 mA from the bus) |
| OS | Raspberry Pi OS Lite 64-bit (Bookworm), trimmed to the services printing needs |
| Reach | home LAN, plus away from home through Tailscale |
| Who runs it | done over SSH (Tailscale SSH once enrolled) |
| Queue | `HP_CP1025`, shown to clients as "HP CP1025" |
| Default color | monochrome (`print-color-mode-default=monochrome`); color is chosen per job |
| Printing access | open to the home LAN (`@LOCAL`) and the tailnet (`100.64.0.0/10`) |
| CUPS admin | web admin reachable from LAN and tailnet, login with an `lpadmin` user, HTTPS only |
| Updates | `unattended-upgrades`, security only; automatic reboot at 04:30 when a patch needs one (a default, easy to change) |
| Record | idempotent `docs/raspberry/setup.sh` plus this file, so a dead SD card can be rebuilt |
| Mac USB queue | removed at the start (step 0) |

## Hardware sizing

The only heavy work is rasterizing each page with Ghostscript. Measured on the Mac: one A4 page at 600 dpi is a 17.4 MB raster (4 color planes of 4960 x 7016 bits). The full-page mode that rendered correctly on the Mac uses one core; the banded mode can use four but rendered blank pages with Ghostscript 10.05.1 on the Mac. A Pi 3 has 1 GB RAM and 4 cores, which is expected to be enough; time and peak memory per page on the Pi are not measured yet (step 3).

Wired Ethernet is preferred over Wi-Fi so the printer does not drop out of Bonjour discovery when Wi-Fi power saving kicks in.

## Steps

### 0. Clean the Mac (about 10 min)
1. Delete the USB queue: `lpadmin -x HP_LaserJet_CP1025`.
2. Remove the HP Printer Drivers 5.1.1 package: list its files with `pkgutil --files`, delete them, `pkgutil --forget` (needs an admin password once). It never worked on macOS 14 anyway.

### 1. Prepare the SD card (about 20 min, by hand)
1. Raspberry Pi Imager: Pi OS Lite 64-bit, hostname `impressora`, a user, SSH with public key only.
2. Plug the Pi into the router by cable and reserve its IP in the router's DHCP.
3. Check `ssh <user>@impressora.local` answers.

### 2. `setup.sh` (about 1 h)
Each block is safe to run again.
1. Trim: `dtoverlay=disable-bt`, `hciuart` off, `dtoverlay=disable-wifi`, `gpu_mem=16`, `cups-browsed` off, `usbcore.autosuspend=-1`, and every other enabled service reviewed until only network, SSH, Tailscale, CUPS and Avahi remain.
2. `unattended-upgrades`, security only, reboot at 04:30 when required.
3. Tailscale: install, `tailscale up --ssh`, ACL limited to the owner's devices.
4. Printing: `cups`, `avahi-daemon`, Debian's foo2zjs package. Queue `HP_CP1025` on the USB URI, CP1025 PPD (name to be checked with `lpinfo -m`), A4, monochrome default, error policy `retry-job`, no preserved job files.
5. Access: print allowed from `@LOCAL` and `100.64.0.0/10`; admin requires an `lpadmin` user over HTTPS.
6. Share: `cupsctl --share-printers`. CUPS 2.4 advertises shared queues as AirPrint through Avahi.

### 3. Measure and decide (about 30 min)
1. Render a test page to a ZJS file through the queue's filter chain and check with `zjsdecode` that all four planes carry data (the Ghostscript blank-page bug seen on the Mac).
2. Time both Ghostscript modes with `/usr/bin/time -v`: A, full page with `-dMaxBitmap` (one core); B, banded with `-dNumRenderingThreads=4` (all cores). Keep the fastest one that is not blank. Add `zram` only if peak memory needs it.
3. Check whether the CP1025 needs a firmware upload (not expected, not confirmed).
4. Record Ghostscript version, seconds per page and peak memory in [../FINDINGS.md](../FINDINGS.md).

### 4. Clients (about 5 min each)
| Device | At home | Away (Tailscale) |
|---|---|---|
| Mac | appears by itself (AirPrint) | add by address: `ipp://impressora.<tailnet>.ts.net/printers/HP_CP1025` |
| iPhone / iPad | appears by itself | **not supported**: iOS only discovers AirPrint printers through Bonjour, which does not cross Tailscale, and cannot add one by address without a third-party app |
| Android | default print service | add the printer by address |
| Linux | automatic | add the IPP URL |

### 5. Validate (about 20 min, after the cartridges are fixed)
1. Test page from Mac, iPhone and Android at home, checked on paper; monochrome by default, color when picked.
2. One page from the Mac through Tailscale.
3. Reboot the Pi and power-cycle the printer: the queue must recover on its own.
4. The CUPS admin page asks for a password and refuses plain HTTP.
5. Optional: rebuild a fresh SD card with `setup.sh` to prove the record works.

## Not verified yet
- The CP1025 PPD name in Bookworm's foo2zjs package, and whether a firmware upload is needed.
- Whether the Pi's Ghostscript has the blank-page bug.
- Seconds and peak memory per page on a Pi 3.
- Whether the Imager-created user has passwordless `sudo` (expected on Pi OS, to confirm over SSH).
- Printing on paper at all, which waits on the cartridges.

## Trade-off accepted
Without the monitor on the Pi there is no toner or chip view once the printer leaves the Mac's USB, and foo2zjs does not report supply levels through CUPS, so AirPrint clients will not show them either. The monitor can be brought back later as a separate step.

## Sources
- foo2zjs support for the CP1025: [mankier](https://mankier.com/package/foo2zjs), [Fedora package](https://packages.fedoraproject.org/pkgs/foo2zjs/foo2zjs)
- CUPS 2.4 sharing as AirPrint on Bookworm: [Linux Junkies guide](https://linuxjunkies.org/guides/raspberry-pi-print-server)
- AirPrint reprint loop reported with iPhone, worked around with the error policy: [administrator.de](https://administrator.de/forum/cups-raspberry-airprint-drucker-678031.html)
- Firmware location for foo2zjs on Debian: [README.Debian](https://metadata-backend.ftp-master.debian.org/changelogs/main/f/foo2zjs/stable_README.Debian)

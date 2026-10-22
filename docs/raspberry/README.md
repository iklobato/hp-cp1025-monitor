# Network print server on a Raspberry Pi (plan, not started)

Goal: every computer and phone prints to the CP1025 over the network instead of plugging into its USB cable. The printer moves to a Raspberry Pi, which runs CUPS with the foo2zjs driver and advertises the printer as AirPrint / IPP Everywhere. Clients need no driver: they send PDF, the Pi converts it to ZJS.

```
Mac / iPhone / Android / Linux  --AirPrint/IPP-->  Raspberry Pi 3 (CUPS + foo2zjs + Avahi)  --USB-->  CP1025
```

Scope is network printing only. The status monitor in this repo is not part of the Pi setup.

Status on 2026-10-22: all decisions taken, step 0 done, nothing built on the Pi yet. Printing works from the Mac over USB with the driver in [../../driver/macos/README.md](../../driver/macos/README.md) (one page on paper, 2026-10-22 16:01); the Pi reuses that same driver chain.

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
| Mac USB queue | step 0 done 2026-10-22: old HP queue deleted, HP CP102x driver files moved to `/Library/Printers/hp-cp1020-backup-20261022/`. The Mac now has its own working queue `HP_CP1025`; delete it once the Pi serves the printer |

## Hardware sizing

The only heavy work is rasterizing each page with Ghostscript. Measured on the Mac: one A4 page at 600 dpi is a 17.4 MB raster (4 color planes of 4960 x 7016 bits). The full-page mode that rendered correctly on the Mac uses one core; the banded mode can use four but rendered blank pages with Ghostscript 10.05.1 on the Mac. A Pi 3 has 1 GB RAM and 4 cores, which is expected to be enough; time and peak memory per page on the Pi are not measured yet (step 3).

Wired Ethernet is preferred over Wi-Fi so the printer does not drop out of Bonjour discovery when Wi-Fi power saving kicks in.

## Steps

### 0. Clean the Mac (done 2026-10-22)
1. Deleted the USB queue `HP_LaserJet_CP1025`.
2. Moved the HP CP102x PPDs and `/Library/Printers/hp/laserjet/cp1020series` to `/Library/Printers/hp-cp1020-backup-20261022/` instead of removing the whole 581 MB HP package, which carries drivers for other HP printers.
3. When the Pi works: `lpadmin -x HP_CP1025` on the Mac and add the network printer instead.

### 1. Prepare the SD card (about 20 min, by hand)
1. Raspberry Pi Imager: Pi OS Lite 64-bit, hostname `impressora`, a user, SSH with public key only.
2. Plug the Pi into the router by cable and reserve its IP in the router's DHCP.
3. Check `ssh <user>@impressora.local` answers.

### 2. `setup.sh` (about 1 h)
Each block is safe to run again.
1. Trim: `dtoverlay=disable-bt`, `hciuart` off, `dtoverlay=disable-wifi`, `gpu_mem=16`, `cups-browsed` off, `usbcore.autosuspend=-1`, and every other enabled service reviewed until only network, SSH, Tailscale, CUPS and Avahi remain.
2. `unattended-upgrades`, security only, reboot at 04:30 when required.
3. Tailscale: install, `tailscale up --ssh`, ACL limited to the owner's devices.
4. Printing: `cups`, `cups-filters`, `avahi-daemon`, plus the driver chain proven on the Mac (see "Driver on the Pi" below). Queue `HP_CP1025` on the USB URI, A4, monochrome default, error policy `retry-job`, no preserved job files.
5. Access: print allowed from `@LOCAL` and `100.64.0.0/10`; admin requires an `lpadmin` user over HTTPS.
6. Share: `cupsctl --share-printers`. CUPS 2.4 advertises shared queues as AirPrint through Avahi.

### 3. Measure and check (about 30 min)
1. Run the queue's filter chain into a file (see "Driver on the Pi") for a black-and-white and a color page, and check with `zjsdecode` that pages and planes carry data, as on the Mac.
2. Time one color A4 page and note peak memory with `/usr/bin/time -v`. Add `zram` only if peak memory needs it.
3. Firmware upload: none was needed on the Mac, where the printer printed straight after power-on, so none is expected on the Pi either.
4. Record Ghostscript version, seconds per page and peak memory in [../FINDINGS.md](../FINDINGS.md).

### 4. Clients (about 5 min each)
| Device | At home | Away (Tailscale) |
|---|---|---|
| Mac | appears by itself (AirPrint) | add by address: `ipp://impressora.<tailnet>.ts.net/printers/HP_CP1025` |
| iPhone / iPad | appears by itself | **not supported**: iOS only discovers AirPrint printers through Bonjour, which does not cross Tailscale, and cannot add one by address without a third-party app |
| Android | default print service | add the printer by address |
| Linux | automatic | add the IPP URL |

### 5. Validate (about 20 min)
1. Test page from Mac, iPhone and Android at home, checked on paper; monochrome by default, color when picked.
2. One page from the Mac through Tailscale.
3. Reboot the Pi and power-cycle the printer: the queue must recover on its own.
4. The CUPS admin page asks for a password and refuses plain HTTP.
5. Optional: rebuild a fresh SD card with `setup.sh` to prove the record works.

## Driver on the Pi: repeat what works on the Mac

On the Mac the chain is `cgpdftoraster` (Apple) -> `cp1025-foo2zjs` -> `rastertopksm` -> `foo2zjs`. On the Pi only the first link changes: Linux CUPS turns PDF into CUPS raster with cups-filters' `pdftoraster` (or `gstoraster`), driven by the same PPD settings. The other three files are the same, and Linux CUPS has no macOS-style sandbox, so the two sandbox workarounds are harmless there.

```bash
# on the Pi (Pi OS Lite 64-bit)
sudo apt install -y cups cups-filters avahi-daemon build-essential libcups2-dev git
git clone https://github.com/iklobato/hp-cp1025-monitor.git ~/hp-cp1025-monitor
git clone --depth 1 https://github.com/koenkooi/foo2zjs.git ~/foo2zjs     # the Mac used commit e04290d

cd ~/foo2zjs && cc -O2 -w -o foo2zjs foo2zjs.c jbig.c jbig_ar.c && cc -O2 -w -o zjsdecode zjsdecode.c jbig.c jbig_ar.c
cd ~/hp-cp1025-monitor && cc -O2 -Wall -Wextra -o /tmp/rastertopksm driver/macos/rastertopksm.c -lcups

# filter + its two helpers in the same directory (the filter finds them next to itself)
F=/usr/lib/cups/filter
sudo install -o root -g root -m 755 driver/macos/cp1025-foo2zjs /tmp/rastertopksm ~/foo2zjs/foo2zjs "$F/"

# same PPD, filter path rewritten for Linux
sed 's|/Library/Printers/foo2zjs/cp1025-foo2zjs|cp1025-foo2zjs|' driver/macos/HP_CP1025-foo2zjs.ppd > /tmp/HP_CP1025.ppd

lpinfo -v | grep -i 'CP%201025'          # the usb:// URI
sudo lpadmin -p HP_CP1025 -D "HP CP1025" -E -v '<usb URI>' -P /tmp/HP_CP1025.ppd \
  -o printer-is-shared=true -o print-color-mode-default=monochrome -o printer-error-policy=retry-job
```

Check before the first real print, same as on the Mac:

```bash
cupsfilter -p /tmp/HP_CP1025.ppd -m printer/foo -o ColorModel=CMYK -o PageSize=A4 test.pdf > out.zjs   # or run pdftoraster | cp1025-foo2zjs by hand
~/foo2zjs/zjsdecode < out.zjs | grep -c ZJT_START_PLANE    # 4 for a color page, 0 for black and white
```

If `pdftoraster` rejects 1-bit black or 8-bit chunky CMYK (the two formats the PPD asks for), the fallback is the classic Linux route: Debian's `printer-driver-foo2zjs` with its own CP1025 PPD and `foo2zjs-wrapper`, plus the `-dMaxBitmap=2000000000` Ghostscript fix if pages come out blank.

## Not verified yet
- That cups-filters' `pdftoraster` on Bookworm accepts the PPD's 1-bit black and 8-bit chunky CMYK settings (Apple's rasterizer does).
- The CP1025 PPD name in Bookworm's foo2zjs package, only needed for the fallback route.
- Whether the Pi's Ghostscript has the blank-page bug (only matters for the fallback route).
- Seconds and peak memory per page on a Pi 3.
- Whether the Imager-created user has passwordless `sudo` (expected on Pi OS, to confirm over SSH).
- Printing on paper from the Pi. From the Mac it works with the same cartridges, which still show `incompatibleConsumable`.

## Trade-off accepted
Without the monitor on the Pi there is no toner or chip view once the printer leaves the Mac's USB, and foo2zjs does not report supply levels through CUPS, so AirPrint clients will not show them either. The monitor can be brought back later as a separate step.

## Sources
- foo2zjs support for the CP1025: [mankier](https://mankier.com/package/foo2zjs), [Fedora package](https://packages.fedoraproject.org/pkgs/foo2zjs/foo2zjs)
- CUPS 2.4 sharing as AirPrint on Bookworm: [Linux Junkies guide](https://linuxjunkies.org/guides/raspberry-pi-print-server)
- AirPrint reprint loop reported with iPhone, worked around with the error policy: [administrator.de](https://administrator.de/forum/cups-raspberry-airprint-drucker-678031.html)
- Firmware location for foo2zjs on Debian: [README.Debian](https://metadata-backend.ftp-master.debian.org/changelogs/main/f/foo2zjs/stable_README.Debian)

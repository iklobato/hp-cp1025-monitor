# Network print server on a Raspberry Pi (printing, dashboard and rebuild script work)

Goal: every computer and phone prints to the CP1025 over the network instead of plugging into its USB cable. The printer moves to a Raspberry Pi, which runs CUPS with the foo2zjs driver and advertises the printer as AirPrint / IPP Everywhere. Clients need no driver: they send PDF, the Pi converts it to ZJS.

```
Mac / iPhone / Android / Linux  --AirPrint/IPP-->  Raspberry Pi 3 (CUPS + foo2zjs + Avahi)  --USB-->  CP1025
```

Scope is network printing only. The status monitor in this repo is not part of the Pi setup.

## Status (last checked 2026-10-22 22:56)

| Area | State |
|---|---|
| Pi | Raspberry Pi 3 Model B Rev 1.2, wired, `impressora-pi.local` (`192.168.1.177` from DHCP, not reserved yet) |
| OS | Raspberry Pi OS Lite 64-bit **Trixie** (Debian 13), image 2026-10-06, kernel 6.18.50 |
| Login | `ssh impressora` from the Mac (key `~/.ssh/impressora`, user `iklo`); SSH password login is off; `sudo` without password |
| Printing | **works over the network.** Queue `HP_CP1025` on the Pi's USB, shared, shown as "HP CP1025 @ impressora-pi" in AirPrint. 11 pages printed by 22:35 (jobs 1 to 7, 14 and 16, from the Pi itself, an Android phone and a second network client; jobs 8 to 13 and 15 went to a test queue, not to paper); printer page counter went 1,317 to 1,328. The last phone job (14, 21:36) asked for color and went out as A4 color (`cupsColorSpace 6`), 20 s from queue to done |
| Time per job | about 13 to 18 s from submit to done for one A4 page, after the colord fix below |
| Status dashboard | `http://impressora-pi.local:8025`: the repo's web dashboard as a systemd service (`cp1025-dashboard`), read-only, open to the home LAN, one reading every 10 s. It showed a page printed at 22:35 live ("+1 página(s) impressa(s), total 1328") |
| Survives a reboot | yes (2026-10-22 22:28): back in 57 s with CUPS, Avahi, the queue and the fast job start; jobs start rendering 1 s after they are queued |
| Updates | security updates install every day by themselves (Debian-Security only); reboot at 04:30 when a patch needs it |
| Rebuild | [`setup.sh`](setup.sh) rebuilds everything above on a fresh card; ran 8 times on this Pi from a local copy, then once more as published (downloaded from GitHub `main` with `curl`, exit 0); the Pi now runs `main` as installed by it |
| Status pages | `http://impressora-pi.local:631/jobs?which_jobs=all` and `/printers/HP_CP1025`, readable from the LAN; `/admin` refused from the network |
| Not done | trimming, Tailscale, access rules, admin over HTTPS, reserving the IP in the router, power-cycling the printer, moving the Mac to the network printer, a rebuild on a blank card |

## Decisions

| Topic | Choice |
|---|---|
| Hardware | Raspberry Pi 3 B on wired Ethernet; printer on a USB-A port (it is self-powered and draws 98 mA from the bus) |
| OS | Raspberry Pi OS Lite 64-bit, Trixie (the current release on 2026-10-22; the plan said Bookworm, which is now "oldstable"), trimmed to the services printing needs |
| Reach | home LAN, plus away from home through Tailscale |
| Who runs it | done over SSH (Tailscale SSH once enrolled) |
| Queue | `HP_CP1025`, shown to clients as "HP CP1025" |
| Default color | monochrome (`print-color-mode-default=monochrome`); color is chosen per job |
| Printing access | open to the home LAN (`@LOCAL`) and the tailnet (`100.64.0.0/10`) |
| CUPS admin | web admin reachable from LAN and tailnet, login with an `lpadmin` user, HTTPS only |
| Updates | `unattended-upgrades`, security only; automatic reboot at 04:30 when a patch needs one (a default, easy to change) |
| Record | idempotent `docs/raspberry/setup.sh` plus this file, so a dead SD card can be rebuilt |
| Mac USB queue | step 0 done 2026-10-22. The Mac still has its USB queue `HP_CP1025`, which no longer reaches the printer; delete it and add the network printer (step 4) |

## Hardware sizing (measured 2026-10-22 on the Pi 3)

One A4 page at 600 dpi, CUPS test page, black and white:

| Stage | Time |
|---|---|
| `pdftopdf` | 0.2 s |
| `gstoraster` (what CUPS picks, Ghostscript 10.05.1) | 3.4 s |
| `pdftoraster` (alternative) | 6.9 s |
| `rastertopksm` + `foo2zjs` | 1.0 s |
| whole CUPS job, submit to done | 13 s |

Peak memory was 305 MB for `pdftoraster` + filter (out of 905 MB). A color page through `pdftoraster` took 5.1 s with the same peak. No `zram` needed. A 3-page phone PDF took 57 s before the colord fix, most of it the colord wait; not measured again after the fix.

Wired Ethernet is preferred over Wi-Fi so the printer does not drop out of Bonjour discovery when Wi-Fi power saving kicks in.

## Steps

### 0. Clean the Mac (done 2026-10-22)
1. Deleted the USB queue `HP_LaserJet_CP1025`.
2. Moved the HP CP102x PPDs and `/Library/Printers/hp/laserjet/cp1020series` to `/Library/Printers/hp-cp1020-backup-20261022/` instead of removing the whole 581 MB HP package, which carries drivers for other HP printers.
3. Still to do (step 4): `lpadmin -x HP_CP1025` on the Mac and add the network printer instead.

### 1. Prepare the SD card (done 2026-10-22)
1. Raspberry Pi Imager 2.0.12 (`brew install --cask raspberry-pi-imager`): device Raspberry Pi 3, OS Raspberry Pi OS Lite (64-bit), hostname `impressora-pi`, user `iklo`, time zone `America/Sao_Paulo`, keyboard `br`, no Wi-Fi, SSH with public key only (`~/.ssh/impressora.pub`, a key made for this Pi), telemetry off.
2. The first SD card was dead: the USB reader (Genesys `05e3:0751`) enumerated but reported no medium (`IOBlockStorageDriver` with no `IOMedia` under it), while another card worked in the same reader. The card used is a 32 GB one that held an old Recalbox install (2018), wiped with the owner's OK.
3. The Pi took about 1 minute to get an address and 25 s more to open SSH on the first boot. `impressora-pi.local` answers through mDNS.
4. Mac side: `Host impressora` added to `~/.ssh/config` (backup of the old file in `~/.ssh/config.bak-20261022`).
5. Still to do: reserve `192.168.1.177` for the Pi in the router's DHCP.

### 2. Server setup (mostly done; [`setup.sh`](setup.sh) holds every step that is done)
1. **Not done.** Trim: `dtoverlay=disable-bt`, `hciuart` off, `dtoverlay=disable-wifi`, `gpu_mem=16`, `cups-browsed` off (already disabled on Trixie), `usbcore.autosuspend=-1`, and every other enabled service reviewed until only network, SSH, Tailscale, CUPS and Avahi remain.
2. **Done.** `unattended-upgrades`, Debian-Security only (the Debian default also takes point releases; `#clear` replaces that list), reboot at 04:30 when required. First run installed 8 security updates, no reboot needed.
3. **Not done.** Tailscale: install, `tailscale up --ssh` (needs the owner to log in once through a link), ACL limited to the owner's devices.
4. **Done.** Printing: `cups`, `cups-filters`, `avahi-daemon`, plus the driver chain proven on the Mac (see "Driver on the Pi" below). Queue `HP_CP1025` on the USB URI with the Pi PPD variant (A4 only, CMYK as PPD default), monochrome queue default, error policy `retry-job`, no preserved job files, system default.
5. **Partly done.** Status pages on, and the dashboard on port 8025. (`cupsctl WebInterface=yes`). Not done: print allowed only from `@LOCAL` and `100.64.0.0/10`; admin with an `lpadmin` user over HTTPS.
6. **Done.** Share: `cupsctl --share-printers`. CUPS 2.4 advertises the queue as AirPrint through Avahi; the Mac sees "HP CP1025 @ impressora-pi" under `_ipp._tcp`.

### 3. Measure and check (done 2026-10-22)
1. Ran the filter chain into files for a black-and-white and a color page and decoded them with `zjsdecode`: black and white = 1 page, no color planes; color = 1 page, 4 planes, each with ink.
2. Times and memory: see "Hardware sizing" above.
3. Firmware upload: none needed, as expected; the printer printed straight away.
4. Ghostscript on the Pi is 10.05.1, the same version that renders blank `pksmraw` pages on the Mac. Through the `cups` device (`gstoraster`) it renders the page correctly: the decoded test page carries the same content as `pdftoraster`'s (3.7% versus 4.4% black dots; `gstoraster` turns light gray into white where `pdftoraster` dithers it).

### 4. Clients (about 5 min each, not done)
| Device | At home | Away (Tailscale) |
|---|---|---|
| Mac | appears by itself (AirPrint) | add by address: `ipp://impressora-pi.<tailnet>.ts.net/printers/HP_CP1025` |
| iPhone / iPad | appears by itself | **not supported**: iOS only discovers AirPrint printers through Bonjour, which does not cross Tailscale, and cannot add one by address without a third-party app |
| Android | default print service (already printed, see "Problems found") | add the printer by address |
| Linux | automatic | add the IPP URL |

### 5. Validate (about 20 min, not done)
1. Test page from Mac, iPhone and Android at home, checked on paper; monochrome by default, color when picked.
2. One page from the Mac through Tailscale.
3. Reboot the Pi (**done**, recovers on its own and the colord fix holds) and power-cycle the printer (not done).
4. The CUPS admin page asks for a password and refuses plain HTTP.
5. Optional: rebuild a blank SD card with `setup.sh` to prove the record works (it has only been run on this already-built Pi).

## Rebuild with setup.sh

On the Mac the chain is `cgpdftoraster` (Apple) -> `cp1025-foo2zjs` -> `rastertopksm` -> `foo2zjs`. On the Pi only the first link changes: CUPS picks cups-filters' `gstoraster` (cost 99, ahead of `pdftoraster` at 100 in `/usr/share/cups/mime/cupsfilters.convs`), driven by the same PPD settings. Both rasterizers accept the PPD's 1-bit black and 8-bit chunky CMYK. The other three files are the same as on the Mac.

[`setup.sh`](setup.sh) is the record of everything that was set up by hand. On a fresh Pi OS Lite card (step 1), with the printer plugged in and on:

```bash
curl -fsSLO https://raw.githubusercontent.com/iklobato/hp-cp1025-monitor/main/docs/raspberry/setup.sh
bash setup.sh
```

| Step | What it does |
|---|---|
| packages | `cups cups-filters avahi-daemon build-essential libcups2-dev git time python3-rich libusb-1.0-0 unattended-upgrades` |
| D-Bus | `systemctl reload dbus`, so colord can start (see Problems found) |
| usblp off | `/etc/modprobe.d/cp1025-no-usblp.conf`, so the dashboard can read the IEEE 1284 port status; CUPS prints through libusb and does not need usblp |
| sources | this repo in `~/hp-cp1025-monitor` (`REPO_REF`, default `main`), foo2zjs at commit `e04290d` in `~/foo2zjs` |
| driver | builds foo2zjs, `zjsdecode`, `rastertopksm`; installs the filter and its two helpers in `/usr/lib/cups/filter` |
| PPD | `/usr/local/share/cp1025/HP_CP1025-pi.ppd`: the repo PPD with the Linux filter path, A4 only, CMYK as PPD default |
| queue | finds the USB URI (`usb://HP/LaserJet%20CP%201025?serial=<serial>`, retried up to 5 times), creates `HP_CP1025` shared, monochrome by default, `retry-job`, system default; `cupsctl --share-printers PreserveJobFiles=No WebInterface=yes` |
| security updates | `20auto-upgrades` and `52cp1025-unattended` in `/etc/apt/apt.conf.d/` |
| dashboard | `/etc/systemd/system/cp1025-dashboard.service`: `printer_summary.py --web --host 0.0.0.0 --port 8025 --every 10`, as the admin user with the `lp` group (which owns the printer's USB device node) |

It refuses to run as root and needs `sudo` without a password (the Imager user has to be given that first, see step 1). The Linux USB URI starts `usb://HP/...`, not `usb://Hewlett-Packard/...` as on macOS. `lpadmin` warns that PPD drivers are deprecated in a future CUPS; CUPS 2.4 still runs them.

Check the chain without paper (`cupsfilter -m printer/foo` stops at PostScript with this PPD and was not useful, so run the filters by hand):

```bash
export PPD=/usr/local/share/cp1025/HP_CP1025-pi.ppd
o="ColorModel=CMYK PageSize=A4"
/usr/lib/cups/filter/pdftoraster 1 me t 1 "$o" /usr/share/cups/data/default-testpage.pdf \
  | /usr/lib/cups/filter/cp1025-foo2zjs 1 me t 1 "$o" > out.zjs
~/foo2zjs/zjsdecode < out.zjs | grep -c ZJT_START_PLANE    # 4 for a color page, 0 for black and white
```

Read one LEDM resource by hand (stop the dashboard first if it must not be interleaved):

```bash
cd ~/hp-cp1025-monitor && sudo python3 hpstatus.py /DevMgmt/ProductStatusDyn.xml
```

## Problems found on the Pi (2026-10-22)

| Problem | Cause | Fix | State |
|---|---|---|---|
| Each job sat about 50 s before printing; the owner thought prints failed and sent them again | `gstoraster` asks colord for a color profile over D-Bus. colord was installed with CUPS, but the D-Bus daemon had not loaded colord's new policy file, so colord could not own `org.freedesktop.ColorManager` ("lost name") and quit; each job waited for two 25 s `DBus.Error.NoReply` timeouts | `sudo systemctl reload dbus`. colord now answers (1.4.7) and a job starts rendering 1 s after it is queued | **fixed**; survival across a reboot not tested yet |
| First phone job: tray reported `trayEmptyOrOpen` | paper not seated | owner reseated the paper | fixed |
| Android job asked for US Letter (`media=Letter`) | the phone's print service picked Letter although the queue advertised A4 as default; the PPD offered A4 and Letter. Also, the filter read `media=Letter` and sent a Letter page even when the raster was drawn at A4 | the Pi's PPD offers A4 only (`media-supported` = `iso_a4_210x297mm`), and the filter now trusts only `PageSize` (which CUPS sets only for a size the PPD has) or the PPD default. On a test queue a `media=Letter` job now goes out as A4 (`paper=9`) | fixed; the next phone job (14) asked for A4 and went out as A4 |
| Android job asked for color (`print-color-mode=color`) but printed in black and white | CUPS builds a preset only for monochrome (`ColorModel=Gray`) and leaves color jobs on the PPD default, which was `Gray` | PPD default `CMYK`, queue default `print-color-mode-default=monochrome`. On a test queue: no color asked = Gray (`cupsColorSpace 3`), `monochrome` = Gray, `color` = CMYK (`cupsColorSpace 6`) | fixed; phone job 14 (color) rendered in CMYK and printed (page counter +1) |
| The status pages said "Web Interface is Disabled" | Debian ships CUPS with `WebInterface No` | `cupsctl WebInterface=yes` | fixed |
| `rastertopksm.c` built with two `-Wenum-compare` warnings under GCC | it compared `cupsColorSpace` with its own enum instead of `CUPS_CSPACE_K` / `CUPS_CSPACE_CMYK` (same values 3 and 6) | uses the libcups constants; output byte-identical before and after (md5 of a gray and a color page) | fixed |
| The dashboard could not read the port status on Linux (`GET_PORT_STATUS`: `LIBUSB_ERROR_IO`) | the kernel `usblp` driver was bound to interface 0; with it unbound the same request returned `0x18` | blacklist `usblp` | fixed |
| With the dashboard reading about once a second, CUPS discovery missed the printer (`lpinfo -v`: 4 of 5 tries) | a reading holds the USB device | the Pi's dashboard reads every 10 s (`--every 10`; 15 of 15 tries then), and `setup.sh` pauses it and retries the lookup | fixed for discovery; whether a job that starts during a reading is delayed was not tested (a page printed fine while it ran) |
| The dashboard checked a queue called `HP_LaserJet_CP1025` | the name of the old HP queue, deleted on 2026-10-22 | checks `HP_CP1025` | fixed |
| The default Debian `unattended-upgrades` list also takes point-release updates | `50unattended-upgrades` allows `label=Debian` | `#clear` and list Debian-Security only | fixed |

## Not verified yet
- Power-cycling the printer while the Pi runs (the queue and the dashboard should recover).
- `setup.sh` on a blank card; it has only run on this Pi, which was already built by hand.
- Whether a job that starts during a dashboard reading waits or fails (`retry-job` would retry it).
- How the color page from the phone (job 14) looks on paper: it printed, but nobody has reported the colors yet.
- Whether the iPhone sees and prints to the Pi.
- Tailscale printing from outside home.
- A phone job that asks for Letter, after the fixes (only checked on the test queue).
- Whether a Mac without the new filter could hit the old `media` fallback: the Mac's installed copy in `/Library/Printers/foo2zjs/` still has it until reinstalled.

## Trade-off accepted
foo2zjs does not report supply levels through CUPS, so AirPrint clients do not show toner levels; the dashboard on port 8025 does. The dashboard has no login and shows the printer's and cartridges' serial numbers, so it is meant for the home LAN only. It listens on every interface: once Tailscale is installed it will also answer on the tailnet unless the Tailscale step limits it.

## Sources
- foo2zjs support for the CP1025: [mankier](https://mankier.com/package/foo2zjs), [Fedora package](https://packages.fedoraproject.org/pkgs/foo2zjs/foo2zjs)
- CUPS 2.4 sharing as AirPrint on Bookworm: [Linux Junkies guide](https://linuxjunkies.org/guides/raspberry-pi-print-server)
- AirPrint reprint loop reported with iPhone, worked around with the error policy: [administrator.de](https://administrator.de/forum/cups-raspberry-airprint-drucker-678031.html)
- Firmware location for foo2zjs on Debian: [README.Debian](https://metadata-backend.ftp-master.debian.org/changelogs/main/f/foo2zjs/stable_README.Debian)

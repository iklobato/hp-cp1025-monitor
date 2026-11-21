# hp-cp1025-monitor: Pi observations, 2026-11-18

Snapshot: `docs/mac-queue-airprint-2026-11-18` @ `dc5f250` (2026-11-18). Read-only looks at the Pi at about 11:10 to 11:40 and again at 12:01 (UTC-3). **Nothing here was changed.** Not read: `painel`'s own repo, the HDMI cable and monitor, the Pi's power supply and cable.

Related: [monitoring-runbook.md](monitoring-runbook.md) (the commands), [open-items.md](open-items.md) (who decides what), [current-implementation.md](current-implementation.md), [../docs/FINDINGS.md](../docs/FINDINGS.md) (Known bugs), [../docs/WORKING.md](../docs/WORKING.md).

## State when the session started (about 11:10)
```
systemctl is-active cups cp1025-dashboard painel   ->  active / active / activating
lpstat -t   ->  HP_CP1025 idle, "accepting requests since Wed 18 Nov 2026 11:00:44" (the same second job 20 stopped; why the queue was re-enabled was not checked)
lsusb       ->  03f0:112a HP LaserJet CP 1025 present
uptime      ->  up 4 days 15 h (the Pi rebooted on the evening of 2026-11-13; CUPS itself was restarted 2026-11-18 00:34)
free -m     ->  905 MB total, 668 MB available, swap 38 MB used
```
At 12:01: `cups` and `cp1025-dashboard` `active`, `painel` still `activating`, `/api/status` answered, temperature 41.9 C, `throttled=0x50000` unchanged, no job left in the Pi queue (`lpstat -o` lists only the completed job 17 from 2026-10-26).

## Not fixed
- **`painel` crash loop.** `systemctl status painel`: `Scheduled restart job, restart counter is at 56646` (restarts about 5 s apart, each consuming about 2 s of CPU by systemd's own accounting). Cause in its journal: `FileNotFoundError: [Errno 2] No such file or directory: '/dev/fb0'` (from `/opt/painel/painel/infra/framebuffer.py`). Another repo ([pi-painel](https://github.com/iklobato/pi-painel)); the framebuffer device is missing, which usually means no HDMI display was detected. Not checked then: whether a monitor is plugged in. **Stopped 2026-11-21 17:05:** there was none, see the 2026-11-21 section below.
- **Under-voltage.** `vcgencmd get_throttled` -> `throttled=0x50000`: under-voltage and throttling have happened since boot, neither at the time of reading. `dmesg` holds `hwmon hwmon1: Undervoltage detected!` at 2026-11-17 13:40:59 and `Voltage normalised` six seconds later. Not checked: the power supply and cable.
- **CUPS debug log.** `LogLevel debug` is still in `/etc/cups/cupsd.conf`; `error_log` was 6.5 MB at 11:11. Already item 3 of "Next steps" in FINDINGS.
- **`dmesg` noise.** Every 10 s: `usb 1-1.4: usbfs: process <pid> (python3) did not claim interface 0 before use`. That is the status page reading the LEDM interface; harmless as far as seen, not investigated.
- **Shared box.** The Pi also runs other things of the owner's. While job 22 rendered, `ps` at 11:32:58 showed an unrelated process at 74% of one core next to `gs` at 69%; the Pi has four cores and no effect on the print was seen.

## The printer on USB, 2026-11-13 to 2026-11-18
`dmesg -T | grep "usb 1-1.4"` has no line for the printer between the Pi's boot (`uptime -s` = 2026-11-13 19:55:02) and this:
```
Wed Nov 18 08:04:41  usb 1-1.4: new high-speed USB device number 4 using dwc_otg
Wed Nov 18 08:04:42  usb 1-1.4: device descriptor read/64, error -71
Wed Nov 18 08:04:42  usb 1-1.4: New USB device found, idVendor=03f0, idProduct=112a   (serial string: a boot-time one)
Wed Nov 18 08:04:44  usb 1-1.4: USB disconnect, device number 4
Wed Nov 18 08:04:45  usb 1-1.4: new high-speed USB device number 5 ...                (serial string: the normal one)
Wed Nov 18 08:32:13  usb 1-1.4: USB disconnect, device number 5
Wed Nov 18 08:32:13  usb 1-1.4: new high-speed USB device number 6 ...                (normal serial)
```
(The serial strings are left out on purpose: this repo is public.)

Read as: the printer was off the bus for about 4.5 days and enumerated at 08:04 like a cold power-on (the first serial string differs from the final one; `error -71` is a protocol error during enumeration). Who switched it off and on was not seen. At 08:32 it dropped and came back within the same second; cause unknown, not seen again by 12:01. A prime suspect is the supply (the under-voltage line above is the day before); not proven.

The status page read at 11:37 and 12:01: `Desliga sozinha após: nunca`, `Dorme após: 15 min`. So the printer's own auto power-off was not what switched it off, unless the setting was lost and restored (not seen).

Why it matters: a switched-off printer looks like a software failure from the Mac (the job is accepted and stalls). First check on any "nothing prints": `ssh impressora 'lsusb | grep 03f0:112a'`.

## What the docs said that was stale
- `docs/FINDINGS.md` "Current state" gave the Mac's printing as broken for an unproven cause and proposed AirPrint as the first test. Done: that test is made and the row, Next steps item 1, Known bugs, Not verified and Done item 19 were rewritten in this session.
- `docs/FINDINGS.md` counters row (1,328 pages) was behind: the printer read 1,329 at 2026-11-18 11:23 and 1,344 at 11:36:58, and still 1,344 at 12:01. Where the extra page between 2026-10-26 and 11:23 came from was not checked.
- `docs/raspberry/README.md` still says the Mac is broken: not edited, listed in [open-items.md](open-items.md).

## Seen on 2026-11-21 (13:07 to 13:38, UTC-3)
Snapshot: `docs/mac-queue-airprint-2026-11-18` @ `dc5f250` (2026-11-21). Read-only except where [smart-install-outage.md](smart-install-outage.md) says otherwise (printer setting, USB port resets).
- **Reboot.** `uptime -s` = 2026-11-20 19:26:24. `/var/log/apt/history.log` last ran 2026-11-19 06:25 (`needrestart` only), so no unattended reboot explains it. `wtmpdb last` is empty and the journal is volatile (first entry 2026-11-21 10:15), so the cause left no trace. Not proven: a power cut.
- **Under-voltage again.** 29 `hwmon hwmon1: Undervoltage detected!` lines in `dmesg` from 2026-11-20 21:05 to 22:07, none after; `vcgencmd get_throttled` = `throttled=0x50000` (happened, not now) through the session.
- **Network.** `eth0` up, `wlan0` down (matches the Pi README). The DHCP address changed since 2026-11-18 (not reserved; not written here, see the privacy rule). `tailscale0` is up, while [../docs/raspberry/README.md](../docs/raspberry/README.md) still lists Tailscale as not done: stale doc, not checked further.
- **Services at 13:38.** `systemctl is-active cups cp1025-dashboard painel` -> `active active active` (`painel` was `activating` at 13:07, so it is still restarting in a loop; not touched).
- **Pi queue.** `lpstat -o` still lists job 17 (2026-10-26, `job-completed-with-errors`, `queued for HP_CP1025`); it did not block jobs 23 and 24. Not touched.
- **`painel` crash loop: cause, and stopped (17:00 to 17:06).** No display at boot: `/sys/class/drm/card0-HDMI-A-1/status` = `disconnected`, EDID 0 bytes, `vc4-drm soc:gpu: [drm] Cannot find any crtc or sizes` at 2026-11-20 19:26:38, and `/sys/class/graphics` holds only `fbcon`, so there is no `/dev/fb0`. 10,986 restarts in 21 h 35 min (56,646 in 4 d 15 h by 2026-11-18): about 7 s each, from each boot. The newest file in `/var/cache/painel` is from 2026-11-08 17:02 and the last paid events run (`runs` in `events.db`) from 2026-11-08 07:14, so it drew and spent nothing since about then. The loop filled 35,783 of the 37,196 lines in the journal, which started at 13:40. The owner said the monitor is removed for good and chose to stop the service: `sudo systemctl disable --now painel` at 17:05, then `inactive`, `disabled`, `NRestarts=0`, no new journal line in 12 s, `cups` and `cp1025-dashboard` `active`.

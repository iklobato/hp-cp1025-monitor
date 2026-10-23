# Findings: HP LaserJet CP1025 on macOS 14

Notes from getting a USB-connected CP1025 working on macOS 14.6.1 (Intel), October 2026. Numbers are what was measured on this unit; dates are when they were measured.

## Status (last checked 2026-10-22 22:56)

| Area | State |
|---|---|
| Where the printer is | **on the Raspberry Pi 3's USB** since about 19:20 on 2026-10-22, no longer on the Mac |
| Printing | **works over the network** through the Pi (CUPS + the same foo2zjs driver chain, rebuilt on Linux): 11 pages printed from the Pi, an Android phone and a second network client, about 13 to 20 s per one-page job. The Pi survives a reboot, installs security updates by itself, and can be rebuilt with `setup.sh`. Phone jobs now come out on A4 and in color when asked (job 14, 21:36). Before that it worked from the Mac over USB (first page 16:01). How color looks on paper is not reported yet. Details: [raspberry/README.md](raspberry/README.md) |
| Network queue | `HP_CP1025` on `impressora-pi.local`, shared as AirPrint ("HP CP1025 @ impressora-pi"), A4, black and white by default; status page `http://impressora-pi.local:631/jobs?which_jobs=all` |
| CUPS queue on the Mac | `HP_CP1025` (USB) still exists but no longer reaches the printer; to be replaced by the network printer. The HP CP102x driver files stay in `/Library/Printers/hp-cp1020-backup-20261022/` |
| Status reading (LEDM) | works on the Pi: the web dashboard runs there as a service at `http://impressora-pi.local:8025`, one reading every 10 s (about 210 ms each). The terminal and web dashboards still run on a Mac too, when the printer is on its USB |
| Terminal and web dashboards | done and refactored; 38 tests, all passing, no printer needed |
| Cartridges | black and yellow are non-HP; the printer still flags them `incompatibleConsumable` (Error) but prints anyway. Total pages 1,328 at 22:35, only that alert |

## Cartridge history (printing no longer blocked)

Until 2026-10-21 the printer reported cartridge errors and every test page was refused. Reading of 2026-10-18 (status `cartridgeMissing`, two `Error` alerts):

| Cartridge | Code | Last state read | Level |
|---|---|---|---|
| Black | CE310A | `missing` (earlier `misinstalled` / `incorrectCartridge`). Chip reads `0000/0000`, no brand. | unknown |
| Cyan | CE311A | ok (earlier `missingMemory`, cleared after reseating) | 50%, about 400 pages |
| Magenta | CE313A | ok, genuine HP | 40%, about 300 pages |
| Yellow | CE312A | `missingMemory` (came and went with reseating). Chip tag is readable (`DC7C/CF07`) but the printer rejects it. | unknown |
| Drum (OPC) | CE314A | ok | 100%, about 7,500 pages |

The cartridges were made in 2013, the drum in 2010. Last use before this session: 2023-01-16. The previous black cartridge printed 594 pages (installed 2020-11-18).

While a cartridge is in an error state with action `stop`, the print channel (interface 0) stops accepting data: CUPS reports `Unable to send data to printer` and the USB write returns `e00002ed`. PJL `INFO` queries on the same channel time out (`LIBUSB_ERROR_TIMEOUT`). The LEDM channel keeps answering.

### Non-HP cartridges, 2026-10-21

The black and yellow cartridges were swapped for non-HP ones. Their chips now read (black `DFB5/7679`, yellow `1833/60F3`) and report 100%, but the printer flags both as `incompatibleConsumable` with severity `Error`. Cartridge model ids written on the chips: the printer accepts `3901`, `3902`, `3903` (genuine HP yellow, magenta, cyan) and rejects `4500` (black) and `4501` (yellow).

How printing was tested: a valid four-plane ZJS page sent raw to the CUPS queue, four times (jobs 14 to 17).

| Time | Printer alerts | Bytes accepted |
|---|---|---|
| 21:35 | `trayEmptyOrOpen` + `incompatibleConsumable` | 0 (`e00002ed`) |
| 21:42 | same; back-channel on EP `0x81` repeats `@PJL USTATUS DEVICE CODE=41900 ONLINE=FALSE` every 30 s (41xyy = load paper in HP PJL) | 0 |
| 21:50 | paper reseated: only `incompatibleConsumable` left, status `processing` | 0 |
| 21:56 | after a power cycle: still only `incompatibleConsumable` | 0 |

That evening the conclusion was that the incompatible-cartridge alert alone blocked printing. **That conclusion was wrong.** On 2026-10-22 the same alert was still active (`incompatibleConsumable`, Error, black and yellow) and the printer accepted and printed a full page through the new driver (job 22, 71,095 bytes, 16:01).

What was different on 10-21 is not proven. The readings at 21:50 and 21:56 showed status `processing`, while on 10-22 it was not; a likely explanation is that the printer was still holding a stuck earlier job, but nothing measured it. Practical rule: `incompatibleConsumable` alone does not stop this printer from printing; if pages are refused, read the status category first.

### Reseating session, as recorded in history.jsonl

The history file kept every reading that changed, which captured one reseating attempt:

| Time | Status | Supplies not ok |
|---|---|---|
| 21:39 | `incorrectCartridge` | black `misinstalled` |
| 22:45 to 22:52 | alternates `closeDoorOrCover` and `initializing` 7 times | all five `unknown` while the door is open |
| 22:53:26 | `initializing` | black `missing`, yellow `missingMemory` |
| 22:53:30 | `cartridgeMissing` | same, unchanged since |

So: with the front door open every supply reads `unknown`; after closing, the printer spends a few seconds in `initializing`; and after this reseat black went from `misinstalled` to `missing`, with its chip still unreadable.

## Drivers

### Driverless (AirPrint / URF)

The device id lists `URF` and `PCLm`, but the printer interface is class `7/1/2` (bidirectional), not `7/1/4` (IPP-over-USB). A queue that sends URF raster over raw USB hangs at "Starting page 1". Not usable over USB.

### HP driver (Apple "HP Printer Drivers" 5.1.1)

- Download: `HewlettPackardPrinterDrivers.dmg` from `updates.cdn-apple.com` (584 MB, Apple-signed). It contains `HP LaserJet CP 1025.gz`.
- The installer refuses macOS above 12.0 (`InstallationCheck` in `Distribution`). Raising the limit in `Distribution` and re-flattening with `pkgutil --flatten` installs it.
- **It does not work on macOS 14.** The `hpcp1020` filter (x86_64, links `libstdc++.6.dylib` and old ColorSync `CM*` calls) exits cleanly but always emits a 464-byte ZJS job with zero raster data: a blank page. Verified with `zjsdecode`.

### foo2zjs (works)

- Built from `github.com/koenkooi/foo2zjs` with `cc -O2 -o foo2zjs foo2zjs.c jbig.c jbig_ar.c`.
- CP1025 options: `-z3 -P -L0 -c`, A4 = `-p9`, `-r600x600 -g4960x7016`.
- **Ghostscript 10.05.1 bug:** `pksmraw` at 600 dpi renders an empty page through its clist banding path. Adding `-dMaxBitmap=2000000000` renders correctly (858,605 non-zero bytes instead of 208 on the test page).
- Working pipeline:

```bash
gs -q -dBATCH -dSAFER -dNOPAUSE -dMaxBitmap=2000000000 -g4960x7016 -r600x600 \
   -sDEVICE=pksmraw -sOutputFile=- input.pdf \
 | foo2zjs -r600x600 -g4960x7016 -p9 -m1 -n1 -d1 -s7 -z3 -P -L0 -c > page.zjs
lp -d <queue> -o raw page.zjs
```

The resulting ZJS had data in all four color planes (verified with `zjsdecode`). On 10-21 the printer refused it (see the cartridge history above).

This pipeline works from a shell but **not inside a macOS CUPS queue**: the CUPS sandbox does not let filters reach Homebrew's Ghostscript. The driver that works on macOS does not use Ghostscript at all.

### foo2zjs driver for macOS (works, 2026-10-22)

Apple's `cgpdftoraster` draws the page as CUPS raster (1-bit black or 8-bit CMYK), a small C program `rastertopksm` turns it into the four 1-bit planes foo2zjs reads, and foo2zjs encodes ZJS. Full description, install steps and checks: [../driver/macos/README.md](../driver/macos/README.md).

Things learned building it:
- The macOS CUPS sandbox blocks filters from `/usr/local` (Homebrew) and from opening `/dev/stdin` by path (`Operation not permitted`). Neither shows as a sandbox "deny" line that was found in the system log.
- `cgpdftoraster` only produces 1-bit K (`cupsColorSpace 3`) or 8-bit **chunky** color; it rejects 1-bit CMYK and every planar order.
- Its raster covers only the PPD's imageable area (A4: 4766 x 6825 dots with 11.52 pt margins), so it has to be centred on the full 4960 x 7016 page before foo2zjs clips 96 dots off each edge.
- The first page printed on paper was an A4 black-and-white document from Brave's Print dialog.

## Low-level protocol notes

### USB

| Item | Value |
|---|---|
| IDs | `03f0:112a`, bcdDevice 0.01, USB 2.00, EP0 64 B |
| Power | self-powered, asks 98 mA, 500 mA available on the port |
| Configuration | 1 configuration, 2 interfaces, 62-byte descriptor |
| Interface 0 | class `7/1/2` printer: bulk OUT `0x01` (ZJS data), bulk IN `0x81`, 512 B |
| Interface 1 | class `ff/04/01` HP LEDM: bulk OUT `0x02`, bulk IN `0x82`, 512 B, interrupt IN `0x83` (8 B, interval 8) for events |
| IEEE 1284 port status | `0x18` = selected, no error, paper present |

macOS IORegistry reports `Bus Power Available` in 2 mA units (250 = the 500 mA that `system_profiler` shows).

### LEDM (HTTP over USB)

- Plain HTTP/1.1 requests written to EP `0x02`, responses read from `0x82`. Server header: `Mrvl-R1_0` (most likely a Marvell controller; inferred from the name only).
- The device sends zero-length packets in the middle of a response, so an empty read does not mean the response ended.
- Chunked responses end with a **zero-padded** last chunk, `0000\r\n\r\n`, not `0\r\n\r\n`. Waiting for the standard terminator made every read hit the 5 s timeout. Matching `0+` brought a full reading from about 35 s to about 0.3 s.
- Leftover bytes from an earlier response must be drained before the next request, or the next read returns the previous resource. Responses are checked against the expected XML root element for that reason.
- `/DevMgmt/DiscoveryTree.xml` lists 25 resources. The `*Cap.xml` ones list every state the printer can report: 32 status categories, 8 consumable states (including brand `clone`), event log size 10.
- Event capabilities: `PowerUpEvent` and `AlertTableChanged`, delivered over USB raw on interface `FF#04#01`.

### Counters on this unit (2026-10-18)

1,315 pages total (862 mono, 453 color), 0 jams, 0 mispicks. Firmware dated 2012-11-08, 6 of 8 MB memory free, system board `YL20BR1`, engine board `40`.

### Event log codes

| Code | Meaning | Confidence |
|---|---|---|
| `10.1000` .. `10.1003` | supply memory / communication error; last digit 0 black, 1 cyan, 2 magenta, 3 yellow | likely: HP forum post for another model and an HP support document; not CP1025 documentation. Consistent with this unit: the log holds `10.1001` and `10.1003`, and cyan and yellow are the two cartridges that reported `missingMemory` |
| `99.100A`, `99.1006`, `99.0B00` | unknown | none found |

The log keeps only the last 10 events (`EventLogMaxSize` = 10), all at page 1,302 to 1,315, so it cannot tell when the black chip first failed.

## Done

1. Diagnosed the USB device and the empty CUPS setup.
2. Ruled out driverless URF over USB.
3. Installed the Apple HP driver (patched version check) and proved its filter emits blank pages on macOS 14.
4. Built foo2zjs, found the Ghostscript 600 dpi banding bug and its workaround, produced valid four-plane ZJS.
5. Read cartridge chip errors directly from the printer over LEDM (later shown not to be what blocked printing, see the cartridge history).
6. Wrote the libusb/LEDM reader, the terminal dashboard (`--full`, `--live`, `--watch`, `--json`), the history and forecast, and the local real-time web dashboard.
7. Published the code as a public repo with screenshots that mask every device and cartridge serial (checked across the whole git history before the first push).
8. Refactored into transport objects (`UsbPrinter`, `LedmClient`), an injectable `PrinterReader`, one shared `Poller`, a rendering-free `viewmodel`, and a `Severity` enum. Terminal output stayed byte-identical to the pre-refactor version (80 and 120 columns, normal, `--full`, `--live`), the web page looked the same, and the whole read path is now tested without hardware.
9. Planned the move to a network print server on a Raspberry Pi 3 and took every decision with the owner (2026-10-19): hardware, OS, Tailscale for remote printing, monochrome default, access rules, automatic security updates, a rebuildable `setup.sh`. Found while planning: Bonjour does not cross Tailscale, so iPhone and iPad can only print at home. See [raspberry/README.md](raspberry/README.md).
10. **Printing works**: wrote a macOS CUPS driver around foo2zjs (`cgpdftoraster` + `rastertopksm` + foo2zjs), replaced the HP queue with `HP_CP1025`, and printed a page on paper (2026-10-22). See [../driver/macos/README.md](../driver/macos/README.md).
11. **Network printing works** (2026-10-22 evening): flashed Pi OS Lite 64-bit Trixie on a Raspberry Pi 3, rebuilt the same driver chain there (`gstoraster` from cups-filters replaces Apple's rasterizer; the filter, `rastertopksm` and foo2zjs are unchanged), shared the queue as AirPrint, and printed 9 pages from the Pi and from phones. Found and fixed a 50 s wait per job (colord could not start until D-Bus reloaded its policy). Measured 13 s per one-page job and 305 MB peak memory. Then made phone jobs print on A4 and in color when asked: the filter ignores a bare `media` option, and the Pi's PPD offers A4 only and defaults to CMYK while the queue stays monochrome by default. See [raspberry/README.md](raspberry/README.md).
12. **Pi hardened and dashboard moved** (2026-10-22 night): security-only automatic updates with a 04:30 reboot when needed; reboot test passed (back in 57 s, colord fix holds); `usblp` blacklisted so the port status can be read; the web dashboard runs on the Pi as a systemd service on the home LAN, reading every 10 s because faster readings made CUPS miss the printer; `setup.sh` rebuilds all of it, was run 8 times on the Pi and once more downloaded from GitHub, and the Pi now runs `main` as installed by it; `rastertopksm.c` uses libcups' color-space constants (no more GCC warning, output byte-identical); the dashboard checks the current queue name and no longer says "Mac" on the Pi.

## Still open, in order

1. **Look at the color page** the phone printed (job 14, 2026-10-22 21:36): it went out as A4 CMYK, the colors and the dither are not judged yet.
2. **Reinstall the filter on the Mac** if the Mac uses its USB queue again: its installed copy in `/Library/Printers/foo2zjs/` still reads the bare `media` option.
3. **Move the Mac to the network printer**: delete the Mac's USB queue `HP_CP1025` and add "HP CP1025 @ impressora-pi".
4. **Finish the Pi** ([raspberry/README.md](raspberry/README.md)): Tailscale (owner logs in once), print access limited to LAN and tailnet, admin over HTTPS, trim services, reserve its IP in the router, power-cycle the printer, a check from the iPhone, and one `setup.sh` run on a blank card.
5. **Optional**: genuine HP 126A black and yellow. Not needed to print, but the printer keeps an `incompatibleConsumable` error and cannot report their level.

## Not verified yet

For the Pi's own open questions (reboot, color on paper, iPhone, Tailscale) see [raspberry/README.md](raspberry/README.md#not-verified-yet). For the printer and the monitor:

- Why the printer refused every page on 2026-10-21 and accepted them on 2026-10-22 with the same cartridge alert.
- Color printing on paper, and the color quality of the plain Bayer dither (Mac) and of `gstoraster`'s output (Pi).
- The live change-feed panel with a real change. The diff logic is tested with simulated readings, and history.jsonl did record the real reseating transitions above, but nobody watched the panel while it happened.
- The run-out forecast: it needs at least a day of real printing in the history.
- The meaning of the `99.xxxx` event codes.
- The role of interrupt endpoint `0x83`: inferred from its interface class and the event capabilities, never captured.
- That "Mrvl" in the server header means a Marvell controller: inferred from the name only.

## Maintenance notes

- Screenshots in `docs/screenshots` mask the printer serial, UUID, USB serial and cartridge serials. To refresh them, wrap `PrinterReader.read()` so it blanks those fields before rendering, and check the result with `git grep` for the real values before committing.
- `history.jsonl` is local data and gitignored. Deleting it resets the trend lines and the forecast.

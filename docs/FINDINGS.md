# Knowledge base: HP LaserJet CP1025 (status monitor, driver, Pi print server)

Everything learned about this one printer and the code around it, kept so a person or an agent can pick the work up cold. Numbers are what was measured on this unit, with the date and time they were measured (local time, UTC-3, unless it says UTC). "Not verified" means exactly that: nobody checked it.

## Start here

| Need | Read |
|---|---|
| How to work in this repo (tests, deploy to the Pi, privacy check before a push) | [WORKING.md](WORKING.md) |
| What the status monitor is, how to run it, code layout | [../README.md](../README.md) |
| The Raspberry Pi print server: what runs there, how to rebuild it | [raspberry/README.md](raspberry/README.md) and [raspberry/setup.sh](raspberry/setup.sh) |
| The CUPS driver (macOS and the Pi) | [../driver/macos/README.md](../driver/macos/README.md) |
| Current state, next steps, bugs, what is unverified | this file, the next five sections |
| What was learned about the printer itself | this file, [Discoveries](#discoveries) |
| The HDMI monitor on the same Pi | separate repo since 2026-10-25: [pi-painel](https://github.com/iklobato/pi-painel) |

## Current state (measured 2026-10-26 01:03)

| Area | State |
|---|---|
| Where the printer is | on the Raspberry Pi 3's USB since about 19:20 on 2026-10-22. Not on the Mac any more |
| Printing from phones and the Pi | **works**: 11 pages on paper by 2026-10-22 22:35 (the Pi itself, an Android phone, a second network client), about 13 to 20 s per one-page job. A phone job asking for color went out as A4 CMYK (job 14) |
| Printing from the Mac | **broken**. The Mac's queue `HP_CP1025___impressora_pi` (added 2026-10-22 22:56) uses the "Generic PostScript Printer" PPD. Its job 17 (2026-10-26 00:56:55, 4.9 MB of PostScript) printed nothing: the Pi's `gstoraster` logged `No pages left, outputting empty file.`, then the driver failed with `rastertopksm: no pages in raster`. Cause not proven, see [Next steps](#next-steps-in-order) |
| The Mac's old USB queue | gone (deleted at a time nobody recorded). The macOS driver files are still installed in `/Library/Printers/foo2zjs/` |
| Pi services | `cups`, `cp1025-dashboard` (port 8025) and `painel` (HDMI, other repo) active; `~/hp-cp1025-monitor` at `3d17461` = `main`; up 22 h; 488 of 905 MB memory available; 55.8 C; `throttled=0x0`; 10 packages with an update waiting (security ones install by themselves, the 10 were not checked one by one) |
| Status page | `http://impressora-pi.local:8025`, verdict `▲ ATENÇÃO` (only the non-HP cartridge alert, a warning), a reading every 10 s taking about 210 ms, plus the "Avançado" section with every raw value (29 blocks, 1,796 fields) |
| Printer settings | full power-off after idle **off** (`AutoOffTime=never` since about 00:20 on 2026-10-26; factory was 4 hours); sleep after 15 min (a job wakes it); calibration 15 min after power-on and every 48 h; `AutoContinue` off; at very low toner it waits for OK on the panel (`acknowledge`); with no paper it waits (15 s) |
| Counters | 1,328 pages (873 black and white, 455 color), 0 jams, 0 mispicks. Unchanged since 2026-10-22 22:35 |
| Supplies | black 100% and yellow 100% are non-HP (the printer cannot vouch for those levels), cyan 50%, magenta 40%, drum 100% |
| CUPS logging | `LogLevel debug` is on in `/etc/cups/cupsd.conf` (set by hand while debugging, not by `setup.sh`): 28,402 of 28,536 lines in the current log are debug, about 10.8 MB a day on the SD card |
| Tests | 46, all passing, no printer needed |

## How it fits together

```
Phones / Mac / Linux  --AirPrint, IPP-->  Raspberry Pi 3: CUPS
                                           gstoraster -> cp1025-foo2zjs -> rastertopksm -> foo2zjs -> usb backend  --USB-->  CP1025
                                                                                                                         ^
Browsers  <--SSE--  cp1025-dashboard :8025  <--  poller  <--  printer_model  <--  hpstatus (libusb, LEDM)  ---------------'
pi-painel (HDMI)  <--  /api/status
```

- The printer has two USB interfaces: `7/1/2` for print data (ZJS) and `ff/04/01` for LEDM, HP's HTTP-over-USB status and settings channel. CUPS uses the first, the status monitor the second.
- Only one process can hold the USB device at a time. While a status reading holds it, CUPS may fail to find the printer, so the Pi reads every 10 s (1 s made `lpinfo` miss it 4 times in 5), and `setup.sh` stops the status page while it looks for the printer.
- The status page reads 8 LEDM resources on every reading and the other 18 the printer lists every 10 minutes.
- Settings are changed with `hpstatus.py --put` (an HTTP `PUT` over the same LEDM channel). `setup.sh` applies [raspberry/product-config.xml](raspberry/product-config.xml) on every run.

## Next steps, in order

1. **Fix printing from the Mac** (about 15 min). Re-add the printer on the Mac as AirPrint instead of "Generic PostScript Printer" (System Settings, Printers, add "HP CP1025 @ impressora-pi", Use: Secure AirPrint), delete `HP_CP1025___impressora_pi`, print one page and check the CUPS log on the Pi. The idea that PostScript is the cause is a guess: what is measured is only that this PostScript job came out of `gstoraster` with zero pages. If AirPrint also fails, find why `gstoraster` drops the pages (keep the job file with `PreserveJobFiles=Yes` and run the filters by hand, see [raspberry/README.md](raspberry/README.md#rebuild-with-setupsh)).
2. **Switch the printer off and on with its button** (2 min): check that the status page still shows `Desliga sozinha após: nunca`, and that the queue and the page recover by themselves.
3. **Turn CUPS debug logging off** (`cupsctl --no-debug-logging`) and put it in `setup.sh`, so the SD card stops taking about 10.8 MB of log a day.
4. **Look at the color page** the phone printed (job 14, 2026-10-22 21:36): the colors and the dither are not judged yet.
5. **Status page bugs** below, the forecast first.
6. **Finish the Pi** ([raspberry/README.md](raspberry/README.md)): Tailscale (the owner logs in once), print access limited to LAN and tailnet, CUPS admin over HTTPS, trim services, reserve the Pi's address in the router, a check from the iPhone, one `setup.sh` run on a blank card.
7. **Optional**: genuine HP 126A black and yellow (not needed to print; the printer keeps an `incompatibleConsumable` error and cannot report their level); alerts to phone; toner level in the print dialog.

## Known bugs, not fixed yet

Status page (port 8025), found in the 2026-10-24 review unless dated:
- The run-out forecast uses the whole history from its first to its last change and accepts a single page as a rate (`history.forecast`), so it gives far-off dates. Seen: "0.5 pág./dia (1 em 2 dias)" on 2026-10-24.
- A reading where one LEDM document failed is still saved to `history.jsonl`, and the change feed then says "alerta resolvido" and "novo alerta" for the same alert. With the door open the feed says "nível 100% → None%".
- `history.jsonl` grows without a limit and is read whole on every reading.
- An SSE client that stops reading keeps a thread forever (no socket timeout in `web_dashboard`).
- "fila do Mac pausada" is still the wording on the Pi.
- (2026-10-26) Each page update now carries the whole "Avançado" data, about 170 KB, to every open tab every 10 s. Fine on the home network; if it is ever slow, serve that data from its own endpoint.

Printing: the Mac's Generic PostScript queue prints nothing (job 17, see Current state and Next steps).

## Not verified yet

For the Pi's own list (iPhone, Tailscale, blank card, a printer switched off during `setup.sh`) see [raspberry/README.md](raspberry/README.md#not-verified-yet). For the printer and the code:

- That `AutoOffTime=never` survives the power button and a power cut: it read back `never` right after the write, the printer was not switched off since.
- That the printer really powered itself off after 4 hours. What is known: the setting said so, and the Pi's kernel log shows the printer arriving on USB at 2026-10-26 00:18 with no USB event since the Pi booted at 2026-10-25 02:39, so it was off for that whole time. Who or what switched it off was not seen.
- `setup.sh` from start to end with the code of 2026-10-26 (the settings step ran only as single commands).
- Why the printer refused every page on 2026-10-21 and accepted them on 2026-10-22 with the same cartridge alert.
- Color on paper, and the quality of the plain Bayer dither (Mac) and of `gstoraster`'s output (Pi).
- The live change feed with a real change: the diff is tested with simulated readings, nobody watched the panel during a real change.
- The run-out forecast with a day of real printing.
- The meaning of the `99.xxxx` event codes.
- The role of interrupt endpoint `0x83`: inferred from its interface class and the event capabilities, never captured.
- That "Mrvl" in the LEDM server header means a Marvell controller: inferred from the name only.

## Done

1. Diagnosed the USB device and the empty CUPS setup.
2. Ruled out driverless URF over USB.
3. Installed the Apple HP driver (patched version check) and proved its filter emits blank pages on macOS 14.
4. Built foo2zjs, found the Ghostscript 600 dpi banding bug and its workaround, produced valid four-plane ZJS.
5. Read cartridge chip errors directly from the printer over LEDM (later shown not to be what blocked printing, see the cartridge history).
6. Wrote the libusb/LEDM reader, the terminal dashboard (`--full`, `--live`, `--watch`, `--json`), the history and forecast, and the local real-time web dashboard.
7. Published the code as a public repo with screenshots that mask every device and cartridge serial (checked across the whole git history before the first push).
8. Refactored into transport objects (`UsbPrinter`, `LedmClient`), an injectable `PrinterReader`, one shared `Poller`, a rendering-free `viewmodel`, and a `Severity` enum. Terminal output stayed byte-identical (80 and 120 columns, normal, `--full`, `--live`), and the whole read path is tested without hardware.
9. Planned the move to a network print server on a Raspberry Pi 3 with the owner (2026-10-19): hardware, OS, Tailscale for remote printing, monochrome default, access rules, automatic security updates, a rebuildable `setup.sh`. Found while planning: Bonjour does not cross Tailscale, so iPhone and iPad can only print at home.
10. **Printing works from macOS** (2026-10-22 16:01): a CUPS driver around foo2zjs (`cgpdftoraster` + `rastertopksm` + foo2zjs) printed a page on paper. See [../driver/macos/README.md](../driver/macos/README.md).
11. **Network printing works** (2026-10-22 evening): Pi OS Lite 64-bit Trixie on a Raspberry Pi 3, the same driver chain with `gstoraster` in place of Apple's rasterizer, shared as AirPrint, 9 pages printed from the Pi and from phones. Fixed a 50 s wait per job (colord could not start until D-Bus reloaded its policy). Measured 13 s per one-page job and 305 MB peak memory. Phone jobs print on A4 and in color when asked: the filter ignores a bare `media` option, and the Pi's PPD offers A4 only and defaults to CMYK while the queue stays monochrome by default.
12. **Pi hardened, status page moved there** (2026-10-22 night): security-only automatic updates with a 04:30 reboot when needed; reboot test passed (back in 57 s); `usblp` blacklisted so the port status can be read; the status page runs as a systemd service on the home LAN, reading every 10 s; `setup.sh` rebuilds all of it (run 8 times on the Pi and once downloaded from GitHub); `rastertopksm.c` uses libcups' color-space constants.
13. **HDMI monitor on the Pi** (2026-10-23): a carousel drawn into the framebuffer, with screens for this printer and for the Pi, news summaries from a cheap model capped at US$ 3 a month, and memory held between 100 and 194 MB.
14. **Monitor fixes** (2026-10-23 night): news refresh no longer aborted by one page cut short; settings page cut from 6,277 px to 1,064 px tall; Docker tried and removed from the Pi (about 71 MB).
15. **Code review and fixes** (2026-10-24), one PR each: #14 accented links broke the news refresh; #15 the non-HP cartridge alert is a warning, every alert is an action, the page says why it has no reading; #16 `setup.sh` with the printer off finishes every other step and fails at the end; #17 the HDMI settings page checks Host, Origin and JSON; #18 one failing block no longer freezes the HDMI screen; #19 and #20 refactors proven byte-identical by 29 renders.
16. **HDMI monitor replaced** (2026-10-25) by [pi-painel](https://github.com/iklobato/pi-painel); the old service, its files and its cache were removed from the Pi, and `setup.sh` no longer installs a monitor.
17. **Auto power-off removed** (2026-10-26, #23): `AutoOffTime` set from `4hour` to `never` over LEDM (`PUT` answered `HTTP/1.1 200 OK`, read back `never`). `hpstatus.py --put` writes any LEDM setting; `setup.sh` applies `product-config.xml` and fails at the end if it did not take. The status page had shown the 15 minute sleep delay as "Desliga sozinha após"; it now shows "Dorme após" and "Desliga sozinha após" apart.
18. **Every raw value on the status page** (2026-10-26, #24): a folded "Avançado" section with a filter: all 26 LEDM resources (25 answer, `InternalPrintDyn` returns 404), the IEEE 1284 device ID and the raw USB data, 29 blocks and 1,796 fields. On the Pi a reading took 218 to 225 ms with it (212 ms before), an open block stays open across readings, and it fits a 390 px phone screen.

## Discoveries

### Cartridge history (printing no longer blocked)

Until 2026-10-21 the printer reported cartridge errors and every test page was refused. Reading of 2026-10-18 (status `cartridgeMissing`, two `Error` alerts):

| Cartridge | Code | Last state read | Level |
|---|---|---|---|
| Black | CE310A | `missing` (earlier `misinstalled` / `incorrectCartridge`). Chip reads `0000/0000`, no brand. | unknown |
| Cyan | CE311A | ok (earlier `missingMemory`, cleared after reseating) | 50%, about 400 pages |
| Magenta | CE313A | ok, genuine HP | 40%, about 300 pages |
| Yellow | CE312A | `missingMemory` (came and went with reseating). Chip tag readable (`DC7C/CF07`) but rejected. | unknown |
| Drum (OPC) | CE314A | ok | 100%, about 7,500 pages |

The cartridges were made in 2013, the drum in 2010. Last use before this work: 2023-01-16. The previous black cartridge printed 594 pages (installed 2020-11-18).

While a cartridge is in an error state with action `stop`, the print channel (interface 0) stops accepting data: CUPS reports `Unable to send data to printer` and the USB write returns `e00002ed`. PJL `INFO` queries on the same channel time out (`LIBUSB_ERROR_TIMEOUT`). The LEDM channel keeps answering.

#### Non-HP cartridges, 2026-10-21

The black and yellow cartridges were swapped for non-HP ones. Their chips read (black `DFB5/7679`, yellow `1833/60F3`) and report 100%, but the printer flags both as `incompatibleConsumable` with severity `Error`. Model ids written on the chips: the printer accepts `3901`, `3902`, `3903` (genuine HP yellow, magenta, cyan) and rejects `4500` (black) and `4501` (yellow).

How printing was tested that evening: a valid four-plane ZJS page sent raw to the CUPS queue, four times (jobs 14 to 17 of the Mac).

| Time | Printer alerts | Bytes accepted |
|---|---|---|
| 21:35 | `trayEmptyOrOpen` + `incompatibleConsumable` | 0 (`e00002ed`) |
| 21:42 | same; back-channel on EP `0x81` repeats `@PJL USTATUS DEVICE CODE=41900 ONLINE=FALSE` every 30 s (41xyy = load paper in HP PJL) | 0 |
| 21:50 | paper reseated: only `incompatibleConsumable` left, status `processing` | 0 |
| 21:56 | after a power cycle: still only `incompatibleConsumable` | 0 |

That evening the conclusion was that the incompatible-cartridge alert alone blocked printing. **That conclusion was wrong.** On 2026-10-22 the same alert was still active and the printer printed a full page through the new driver (job 22, 71,095 bytes, 16:01). What was different on 10-21 is not proven; the readings at 21:50 and 21:56 showed status `processing`, so a stuck earlier job is likely, but nothing measured it. Rule: `incompatibleConsumable` alone does not stop this printer; if pages are refused, read the status category first.

#### Reseating session, as recorded in history.jsonl

| Time | Status | Supplies not ok |
|---|---|---|
| 21:39 | `incorrectCartridge` | black `misinstalled` |
| 22:45 to 22:52 | alternates `closeDoorOrCover` and `initializing` 7 times | all five `unknown` while the door is open |
| 22:53:26 | `initializing` | black `missing`, yellow `missingMemory` |
| 22:53:30 | `cartridgeMissing` | same, unchanged since |

With the front door open every supply reads `unknown`; after closing, the printer spends a few seconds in `initializing`.

### Drivers

#### Driverless (AirPrint / URF) over USB

The device id lists `URF` and `PCLm`, but the printer interface is class `7/1/2` (bidirectional), not `7/1/4` (IPP-over-USB). A queue that sends URF raster over raw USB hangs at "Starting page 1". Not usable over USB. (The Pi's AirPrint is CUPS talking to clients; to the printer it still sends ZJS.)

#### HP driver (Apple "HP Printer Drivers" 5.1.1)

- `HewlettPackardPrinterDrivers.dmg` from `updates.cdn-apple.com` (584 MB, Apple-signed) contains `HP LaserJet CP 1025.gz`.
- The installer refuses macOS above 12.0 (`InstallationCheck` in `Distribution`). Raising the limit and re-flattening with `pkgutil --flatten` installs it.
- **It does not work on macOS 14.** The `hpcp1020` filter (x86_64, old ColorSync `CM*` calls) exits cleanly but always emits a 464-byte ZJS job with zero raster data: a blank page. Verified with `zjsdecode`.

#### foo2zjs

- Built from `github.com/koenkooi/foo2zjs` (commit `e04290d`) with `cc -O2 -o foo2zjs foo2zjs.c jbig.c jbig_ar.c`.
- CP1025 options: `-z3 -P -L0 -c`, A4 = `-p9`, `-r600x600 -g4960x7016`.
- **Ghostscript 10.05.1 bug:** `pksmraw` at 600 dpi renders an empty page through its clist banding path. `-dMaxBitmap=2000000000` renders it correctly (858,605 non-zero bytes instead of 208 on the test page).
- Shell pipeline that works:

```bash
gs -q -dBATCH -dSAFER -dNOPAUSE -dMaxBitmap=2000000000 -g4960x7016 -r600x600 \
   -sDEVICE=pksmraw -sOutputFile=- input.pdf \
 | foo2zjs -r600x600 -g4960x7016 -p9 -m1 -n1 -d1 -s7 -z3 -P -L0 -c > page.zjs
lp -d <queue> -o raw page.zjs
```

It does **not** work inside a macOS CUPS queue: the CUPS sandbox does not let filters reach Homebrew's Ghostscript. The driver that works uses no Ghostscript on macOS.

#### The driver in this repo (works on macOS and on the Pi)

Apple's `cgpdftoraster` (macOS) or cups-filters' `gstoraster` (Pi) draws the page as CUPS raster (1-bit black or 8-bit CMYK); `rastertopksm` turns it into the four 1-bit planes foo2zjs reads; foo2zjs encodes ZJS. Details and install: [../driver/macos/README.md](../driver/macos/README.md). Learned building it:
- The macOS CUPS sandbox blocks filters from `/usr/local` (Homebrew) and from opening `/dev/stdin` by path (`Operation not permitted`), with no sandbox "deny" line found in the system log.
- `cgpdftoraster` only produces 1-bit K (`cupsColorSpace 3`) or 8-bit **chunky** color; it rejects 1-bit CMYK and every planar order.
- Its raster covers only the PPD's imageable area (A4: 4766 x 6825 dots with 11.52 pt margins), so it is centred on the full 4960 x 7016 page before foo2zjs clips 96 dots off each edge.
- On the Pi, with Ghostscript 10.05.1, `gstoraster` (the `cups` device) renders correctly; the `pksmraw` bug above does not apply there.

### USB

| Item | Value |
|---|---|
| IDs | `03f0:112a`, bcdDevice 0.01, USB 2.00, EP0 64 B |
| Power | self-powered, asks 98 mA, 500 mA available on the port |
| Configuration | 1 configuration, 2 interfaces, 62-byte descriptor |
| Interface 0 | class `7/1/2` printer: bulk OUT `0x01` (ZJS data), bulk IN `0x81`, 512 B |
| Interface 1 | class `ff/04/01` HP LEDM: bulk OUT `0x02`, bulk IN `0x82`, 512 B, interrupt IN `0x83` (8 B, interval 8) for events |
| IEEE 1284 port status | `0x18` = selected, no error, paper present |

- macOS IORegistry reports `Bus Power Available` in 2 mA units (250 = the 500 mA that `system_profiler` shows).
- On Linux the kernel `usblp` driver binds interface 0 and makes `GET_PORT_STATUS` fail (`LIBUSB_ERROR_IO`); it is blacklisted on the Pi.
- On the Pi, opening the device needs root or the `lp` group (the service runs with `SupplementaryGroups=lp`). As a plain user `hpstatus.py` fails with `RuntimeError: printer not found on USB`, which looks like the printer is off; run manual reads with `sudo`.

### LEDM (HTTP over USB)

- Plain HTTP/1.1 requests written to EP `0x02`, responses read from `0x82`. Server header: `Mrvl-R1_0`.
- The device sends zero-length packets in the middle of a response, so an empty read does not mean the response ended.
- Chunked responses end with a **zero-padded** last chunk, `0000\r\n\r\n`, not `0\r\n\r\n`. Waiting for the standard ending made every read hit the 5 s timeout; matching `0+` brought a full reading from about 35 s to about 0.3 s.
- Leftover bytes of an earlier response must be drained before the next request, or the next read returns the previous resource. The 8 resources of a normal reading are checked against their expected XML root element for that reason.
- `/DevMgmt/DiscoveryTree.xml` lists 26 resources (measured 2026-10-26 on the Pi): all of them took 408 ms together and about 160 KB. The biggest: `MediaDyn` 30.5 KB, `MediaCap` 22.9 KB, `ShopForSupplies` 20.4 KB (it embeds whole XML documents as text). `InternalPrintDyn` answers 404.
- The `*Cap.xml` resources list every value a field can take: 32 status categories, 8 consumable states (including brand `clone`), event log size 10.
- Event capabilities: `PowerUpEvent` and `AlertTableChanged`, delivered on interface `FF#04#01`.

#### Changing settings

A `PUT` of the same `...Dyn.xml` path, carrying only the fields to change, with `Content-Type: text/xml` and a `Content-Length`. The firmware answers `HTTP/1.1 200 OK` with an empty body. The matching `...Cap.xml` marks each field `readWrite` or `readOnly` and lists the values it accepts. Done with `hpstatus.py --put PATH XML_FILE`; only `AutoOffTime` has been written so far.

Writable on this unit (read 2026-10-26):

| Field (resource) | Value now | Accepted |
|---|---|---|
| `AutoOffTime` (ProductConfig): full power-off after idle; the printer then leaves USB until its button is pressed | `never` (factory `4hour`) | `never`, `30_minutes`, `1hour`, `2hours`, `4hours`, `8hours`, `24hours` |
| `AutoOffAutoOn/AutoOffDelay/AutoOffDelayTime` (ProductConfig): sleep; a job wakes the printer | `15_minutes` | `1_minute`, `5_minutes`, `15_minutes`, `30_minutes`, `1hour` (no `never`; `AutoOffDelaySetting` only `enabled`) |
| `PowerOnCalibration` (ProductConfig) | `15minutes` | `never`, `always`, `5minutes`, `15minutes`, `30minutes`, `60minutes` |
| `EngineCalibrationTimer` (ProductConfig) | `48hours` | `never`, `12hours`, `24hours`, `48hours`, `96hours`, `128hours` |
| `CountryAndRegionName`, `SmartInstall` (ProductConfig) | `brazil`, `enabled` | a country list; Smart Install values not recorded |
| `AutoContinue` (PrintConfig): carry on after a minor error such as a paper size mismatch | `off` | `on`, `off` |
| Print density per color and tone (PrintConfig) | `noAdjustment` | listed in `PrintConfigCap` (not copied here) |
| `MediaOutActionType`, `MediaOutTime` (MediaHandling): no paper | `waitForPaperToBeLoaded`, 15 s | `waitForPaperToBeLoaded` or `cancel`; -1 to 3600 s |
| `ConsumableStateAction` at very low (ConsumableConfig) | `acknowledge` (waits for OK on the panel) | `acknowledge`, `stop`, `continue` |

The firmware reports `4hour` while its Cap list spells it `4hours`. The owner chose (2026-10-26) to change only the power-off; `AutoContinue` and the very-low action stay as they are.

### Counters on this unit

2026-10-18: 1,315 pages (862 black and white, 453 color). 2026-10-26: 1,328 (873, 455), 0 jams, 0 mispicks. Firmware dated 2012-11-08, 6 of 8 MB memory free, system board `YL20BR1`, engine board `40`.

### Event log codes

| Code | Meaning | Confidence |
|---|---|---|
| `10.1000` .. `10.1003` | supply memory / communication error; last digit 0 black, 1 cyan, 2 magenta, 3 yellow | likely: from HP material for other models, not CP1025 documentation. Fits this unit: the log holds `10.1001` and `10.1003`, and cyan and yellow are the two cartridges that reported `missingMemory` |
| `99.100A`, `99.1006`, `99.0B00` | unknown | none found |

The log keeps only the last 10 events (`EventLogMaxSize` = 10), all at page 1,302 to 1,315, so it cannot tell when the black chip first failed.

## Maintenance notes

- Screenshots in `docs/screenshots` mask the printer serial, UUID, USB serial and cartridge serials. To refresh them, wrap `PrinterReader.read()` so it blanks those fields before rendering, and `git grep` for the real values before committing.
- `history.jsonl` is local data and gitignored. Deleting it resets the trend lines and the forecast.
- The live status page and the "Avançado" section show the real serials. That is accepted for the home LAN only.

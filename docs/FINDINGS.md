# Findings: HP LaserJet CP1025 on macOS 14

Notes from getting a USB-connected CP1025 working on macOS 14.6.1 (Intel), October 2026. Numbers are what was measured on this unit; dates are when they were measured.

## Status (2026-10-18)

| Area | State |
|---|---|
| USB link | works: USB 2.0 high speed, behind a USB 2.0 hub (`1a40:0101`) |
| Status reading (LEDM) | works: full reading in about 270 ms |
| Terminal and web dashboards | done, tested (30 tests, all passing) |
| Printing | **blocked by cartridges**, not by software |
| CUPS queue | exists (`HP_LaserJet_CP1025`) but still points at the HP driver, which produces blank pages |

## What blocks printing

The printer reports cartridge errors and refuses print data until they clear. Seen over the session:

| Cartridge | Code | Last state read | Level |
|---|---|---|---|
| Black | CE310A | `cartridgeMissing` (earlier `misinstalled` / `incorrectCartridge`). Chip reads `0000/0000`, no brand. | unknown |
| Cyan | CE311A | ok (earlier `missingMemory`, cleared after reseating) | 50% |
| Magenta | CE313A | ok, genuine HP | 40% |
| Yellow | CE312A | `missingMemory` (comes and goes with reseating) | unknown |
| Drum (OPC) | CE314A | ok | 100% |

The cartridges were made in 2013, the drum in 2010. Last use before this session: 2023-01-16. The previous black cartridge printed 594 pages (installed 2020-11-18).

While a cartridge is in an error state with action `stop`, the print channel (interface 0) stops accepting data: CUPS reports `Unable to send data to printer` and the USB write returns `e00002ed`. PJL `INFO` queries on the same channel time out (`LIBUSB_ERROR_TIMEOUT`). The LEDM channel keeps answering.

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

The resulting ZJS had data in all four color planes (verified with `zjsdecode`). It was sent to the printer but not printed, because of the cartridge errors above.

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
| `10.1000` .. `10.1003` | supply memory / communication error; last digit 0 black, 1 cyan, 2 magenta, 3 yellow | likely: HP forum post for another model and an HP support document; not CP1025 documentation |
| `99.100A`, `99.1006`, `99.0B00` | unknown | none found |

## Done

1. Diagnosed the USB device and the empty CUPS setup.
2. Ruled out driverless URF over USB.
3. Installed the Apple HP driver (patched version check) and proved its filter emits blank pages on macOS 14.
4. Built foo2zjs, found the Ghostscript 600 dpi banding bug and its workaround, produced valid four-plane ZJS.
5. Found the real blocker: cartridge chip errors, read directly from the printer over LEDM.
6. Wrote the libusb/LEDM reader, the terminal dashboard (`--full`, `--live`, `--watch`, `--json`), the history and forecast, and the local real-time web dashboard.

## Still open

1. **Black cartridge (CE310A)**: reports missing / misinstalled with an unreadable chip. Reseat; if it persists, replace with a CE310A that has a working chip.
2. **Yellow cartridge (CE312A)**: chip read fails intermittently. Clean the contacts and reseat; replace if it keeps failing.
3. **Print queue**: once the printer reports ready, replace the HP driver queue with a foo2zjs-based one (wrapper or CUPS filter using the pipeline above, including `-dMaxBitmap`), then print a test page and confirm it on paper.
4. **Optional cleanup**: remove the unusable HP driver package (`/Library/Printers/hp/laserjet/cp1020series`, the PPDs under `/Library/Printers/PPDs/Contents/Resources/HP LaserJet CP*`).
5. **Not verified yet**: the change feed with a real change (only tested with simulated readings), the run-out forecast (needs a day of real printing), the meaning of the `99.xxxx` codes, and the role of the interrupt endpoint `0x83` (inferred from the class and the event capabilities, not captured).

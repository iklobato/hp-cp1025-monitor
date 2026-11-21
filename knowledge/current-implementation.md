# hp-cp1025-monitor: current implementation (Mac to Pi printing)

Snapshot: `docs/mac-queue-airprint-2026-11-18` @ `dc5f250` (2026-11-18). Values read live at 12:01 to 12:03 (UTC-3) on the Mac (`lpstat`, `lpoptions`, the queue's PPD) and the Pi (`systemctl`, `lsusb`, `/api/status`, the Pi queue's PPD, `ls /usr/lib/cups/filter`). Not read: the content of the Pi's `cp1025-foo2zjs` against the repo copy (same size, 2,536 bytes, not diffed), the Mac's `printers.conf`.

Related: [mac-printing.md](mac-printing.md) (why it was broken), [monitoring-runbook.md](monitoring-runbook.md), [../docs/raspberry/README.md](../docs/raspberry/README.md) (how the Pi was built), [../docs/FINDINGS.md](../docs/FINDINGS.md) ("How it fits together").

## How a Mac print travels today
```
Mac app (browser, Preview)
  -- PDF --> Mac CUPS queue  HP_CP1025___impressora_pi   (IPP Everywhere PPD, A4 only, default Gray)
  -- IPP over TLS, ipps://impressora-pi.local.:631/printers/HP_CP1025 --> Pi CUPS queue HP_CP1025
        (PPD A4 only, default ColorModel CMYK, queue default print-color-mode=monochrome)
  Pi filters, as logged for jobs 21 and 22:
        pdftopdf -> gstoraster -> cp1025-foo2zjs (= rastertopksm + foo2zjs) -> usb backend
  -- USB (ZJS, interface 7/1/2) --> HP LaserJet CP1025
```
Evidence: Pi log of job 22, `File of type application/pdf queued by "iklo"`, then `Started filter .../pdftopdf`, `.../gstoraster`, `.../cp1025-foo2zjs`, `Started backend .../usb`; Mac log, `printer-uri uri ipps://impressora-pi.local.:631/printers/HP_CP1025`. The old route was PostScript (`application/vnd.cups-postscript`, no `pdftopdf`, no `pstops`), see [mac-printing.md](mac-printing.md).

## Values measured (2026-11-18 12:01 to 12:03)
| Item | Value | Read with |
|---|---|---|
| Mac queue driver | `*NickName: "LaserJet CP1025 foo2zjs - IPP Everywhere"` | `grep NickName /etc/cups/ppd/HP_CP1025___impressora_pi.ppd` |
| Mac queue paper | `*DefaultPageSize: A4`, `PageSize/Media Size: *A4` (only A4 offered) | same PPD, `lpoptions -p <queue> -l` |
| Mac queue color default | `ColorModel/Output Mode: *Gray RGB` (Gray is the default) | `lpoptions -p <queue> -l` |
| Mac queue state | idle, enabled, system default destination | `lpstat -p -d` |
| Pi queue paper | A4 only (`media-supported = iso_a4_210x297mm`, also `media-default`, `media-ready`) | `ipptool -tv ipps://impressora-pi.local.:631/printers/HP_CP1025 get-printer-attributes.test` |
| Pi queue color | PPD `*DefaultColorModel: CMYK`; IPP `print-color-mode-default = monochrome`, `print-color-mode-supported = monochrome,color` | the PPD, the same `ipptool` call |
| Pi filters present | `cp1025-foo2zjs`, `foo2zjs`, `gstoraster`, `pdftopdf`, `rastertopksm` in `/usr/lib/cups/filter` | `ls` |
| Canvas | 4960 x 7016 for A4 (`cp1025-foo2zjs page=4960x7016 paper=9 copies=1` in jobs 20 and 21). The script also has a Letter branch (5100 x 6600) that the Pi never reaches, because the Pi PPD has no Letter | the Pi's CUPS log; [../driver/macos/cp1025-foo2zjs](../driver/macos/cp1025-foo2zjs) |
| Ghostscript for a color PDF job | `gs ... -sDEVICE=cups -r600x600 -dDEVICEWIDTHPOINTS=595 -dDEVICEHEIGHTPOINTS=842 -dcupsBitsPerColor=8 -dcupsColorOrder=0 -dcupsColorSpace=6 -scupsPageSizeName=A4 ... .HWMargins[11.52 ...]` | Pi log, job 22 |
| Pi services | `cups` active, `cp1025-dashboard` active, `painel` activating (crash loop, see [pi-observations.md](pi-observations.md)) | `systemctl is-active` |
| Printer on USB | present (`03f0:112a`) | `lsusb` on the Pi |
| Printer settings | `Desliga sozinha após: nunca`, `Dorme após: 15 min`, `Auto-continuar: desligado` | status page, key `settings` |
| Printer state | `economia de energia` (asleep, a job wakes it) with the usual non-HP cartridge alert `incompatibleConsumable` | status page, keys `status_rows`, `alerts` |
| Page counter | 1,344 total, 875 black and white, 469 color, 0 jams, 0 mispicks | status page, key `counter_rows` |
| Toner | cyan 50% (raw 49%); magenta "next to run out", about 250 pages (the printer's estimate; about 300 at about 11:18, before the two Mac jobs) | status page, keys `consumables`, `next_out` |

## What this session changed
On the owner's Mac, locally (not tracked by any repo):
```
lpadmin -p HP_CP1025___impressora_pi -v '<the queue's existing dnssd:// URI>' -m everywhere -E   # exit 0, no sudo needed
lpadmin -p HP_CP1025___impressora_pi -o ColorModel=Gray                                          # exit 0
```
The first swaps the driver in place (name, device URI and "system default" status kept); the second puts black and white back, because IPP Everywhere defaults to color (`DefaultColorModel: RGB`) and the Pi queue is monochrome on purpose. Rollback: the old PPD was copied only to the session's scratch folder, which a reboot or cleanup can erase, so there is no durable rollback; and going back to "Generic PostScript" would bring the bug back.

In the repo (uncommitted, on branch `docs/mac-queue-airprint-2026-11-18`): `docs/FINDINGS.md` (Current state rows for the Mac and the counters, Next steps item 1, Known bugs, Not verified, Done item 19) and this `knowledge/` folder. No code, no PPD, no `setup.sh`, no test changed, so the test command of `docs/WORKING.md` was not run.

## What this session did not change
- Anything on the Pi (config, PPD, filters, services, CUPS settings). A temporary folder was created and removed (`ls` of it answers "No such file or directory").
- The printer's settings. Nothing was written over LEDM.
- `painel`, the CUPS debug log level, the Pi's power supply.

## Rebuilding the Mac queue
Only the in-place switch above was run. A fresh add with the same options (`lpadmin -p <name> -E -v <uri> -m everywhere`, or System Settings, Add Printer, "HP CP1025 @ impressora-pi", Use: Secure AirPrint, as `docs/raspberry/README.md` already advises) was **not** tested. Find the device URI with `lpstat -v`. After any re-add, check `grep NickName /etc/cups/ppd/<queue>.ppd`: it must say IPP Everywhere, not "Generic PostScript Printer". Setting the color default needs the extra `-o ColorModel=Gray`.

## Why this fix and not another
- The Pi already advertises A4 only over IPP, so a client that reads the printer's attributes (AirPrint, IPP Everywhere) can only ask for A4. The Generic PostScript PPD knows nothing of the printer and defaults to Letter.
- Setting `PageSize=A4` on the Generic PostScript queue was rejected: apps can still pick Letter, and that route keeps the unexplained 12.5 minute wait of job 20 and the empty page of job 17.
- Adding Letter back to the Pi PPD was rejected: the tray holds A4 and `AutoContinue` is off, so a Letter page would wait on the printer's panel.

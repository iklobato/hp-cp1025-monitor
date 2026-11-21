# hp-cp1025-monitor: monitoring runbook (Mac to Pi printing)

Snapshot: `docs/mac-queue-airprint-2026-11-18` @ `dc5f250` (2026-11-18). Every command below was run on 2026-11-18 and the "last seen" values are from 12:01 to 12:03 (UTC-3), except `cupsctl PreserveJobFiles=Yes` (standard CUPS syntax, the same key `setup.sh` sets to `No`, not run in this session). Not tested: the commands from a machine other than the owner's Mac, and any alerting (nothing here is automated; a person or an agent runs these).

Related: [current-implementation.md](current-implementation.md), [mac-printing.md](mac-printing.md), [open-items.md](open-items.md), [../docs/WORKING.md](../docs/WORKING.md) ("Rules that come from real breakage": one USB reader at a time, `sudo` for manual reads, end every Pi session with a service check).

## After every Mac print: prove it
1. Read the counter before and after (one command, run from the Mac):
```
ssh impressora 'curl -s -m 8 localhost:8025/api/status | python3 -I -c "import sys,json; d=json.load(sys.stdin); print(d[\"read_at\"], d[\"counter_rows\"], d[\"status_rows\"])"'
```
   Expect `Páginas no total` up by the number of sheets, and the split (`Preto e branco` or `Coloridas`) to match what was asked. The status page reads every 10 s, so wait until `Estado` is no longer `processando` and two reads agree. A CUPS "completed" comes about 2 minutes before the last page (job 22).
2. Read the job on the Pi (N is the Pi job number, see [README.md](README.md) for matching):
```
ssh impressora 'sudo grep -hE "\[Job N\]" /var/log/cups/error_log | grep -E "rastertopksm wrote|Job completed|Job stopped|^E "'
```
   Expect `rastertopksm wrote K page(s)` with K equal to the counter increase, then `Job completed.`. Find N with `lpstat -W all -o` on the Pi. `error_log` is rotated about once a day (file dates on 2026-11-18: `error_log.1` 00:34, `.2.gz` 2026-11-17 00:02), and `LogLevel debug` makes it large, so older jobs are in `error_log.1` and `error_log.N.gz` (use `zgrep`).

## Symptom to first check
| Symptom | First check | Meaning if it matches |
|---|---|---|
| Job waits on `Waiting for printer to become available`, status page says `leitura falhou` | `ssh impressora 'sudo dmesg -T \| grep -E "Smart Install\|SerialNumber" \| tail -3'` | a `Smart Install` CD-ROM or the SI serial: the printer is in HP Smart Install mode; see [smart-install-outage.md](smart-install-outage.md) (2026-11-21) for the unlock and the setting |
| Job "completed" but no paper | the counter (step 1), then `lsusb \| grep 03f0:112a` on the Pi | counter flat and printer absent from USB: the printer is off or unplugged (it was off the bus from 2026-11-13 to 2026-11-18 08:04) |
| `rastertopksm: raster is larger than the page` | the client queue's PPD default page size, the Pi log's `Ghostscript command line` | a client is sending Letter (or a size above A4) to the A4-only Pi queue; see [mac-printing.md](mac-printing.md) |
| `rastertopksm: no pages in raster` with `No pages left, outputting empty file` | keep the job file: `sudo cupsctl PreserveJobFiles=Yes`, resend, copy `/var/spool/cups/d000NN-001`, then set it back to `No` | job 17's failure; **cause unknown, not reproduced** |
| Job sits in `Sending data to printer` for hours | `lpstat -p HP_CP1025`, then `lsusb`, then the panel | CUPS cancels a stuck job after 10,800 s (job 19) |
| Job starts but Ghostscript does not appear in the log for minutes | `grep "Ghostscript command line"` for the job; see which route (PostScript or PDF) in `File of type ...` | the PostScript route waited 12.5 minutes once (job 20); the PDF route started in 9 s (job 22) |
| Pages come out in color when black and white was wanted | the job's `ColorModel`/`print-color-mode` in `argv[5]` of the Pi log | the app asked for color; the queue default does not override it |
| Mac queue prints Letter again | `grep NickName /etc/cups/ppd/HP_CP1025___impressora_pi.ppd` on the Mac | it says "Generic PostScript Printer": the queue was re-added by hand, redo the switch in [current-implementation.md](current-implementation.md) |

## Standing watch list
| What | Command | Normal | Last seen |
|---|---|---|---|
| Printer on USB | `ssh impressora 'lsusb \| grep 03f0:112a'` | one line | present, 12:01 |
| USB drops | `ssh impressora 'sudo dmesg -T \| grep -E "usb 1-1\.[2-5]\|port[2-5]" \| grep -v usbfs'` (the cable is on port 5 since 2026-11-21) | no new lines | 2026-11-21: port 4 failures 12:58 to 13:10, printer on 1-1.5 since 13:21:57, see [smart-install-outage.md](smart-install-outage.md) |
| Smart Install off | read `ProductConfigDyn.xml` over LEDM with the status page stopped, look for `<dd:SmartInstall>` | `disabled` | `disabled`, 2026-11-21 13:34 |
| Power of the Pi | `ssh impressora 'vcgencmd get_throttled'` | `0x0`. Bits 0 and 2 (`0x1`, `0x4`) set = happening now; bits 16 and 18 (`0x10000`, `0x40000`) = has happened since boot | `0x50000` (happened, not now), 41.9 C |
| Services | `ssh impressora 'systemctl is-active cups cp1025-dashboard painel'` | `active` x3 | `active active activating` (`painel` is crash looping, not this project's code) |
| Mac queue driver | `grep NickName /etc/cups/ppd/HP_CP1025___impressora_pi.ppd` | `... IPP Everywhere` | IPP Everywhere, 12:01 |
| Mac queue color default | `lpoptions -p HP_CP1025___impressora_pi -l \| grep ColorModel` | `*Gray RGB` | `*Gray RGB` |
| Toner after color jobs | status page keys `consumables`, `next_out` | cyan and magenta falling by the pages printed | cyan 50%, magenta about 250 pages (about 300 before the 14 color pages), 12:02 |
| CUPS log size | `ssh impressora 'ls -la /var/log/cups/error_log'` | small (debug off) | 6.5 MB at 11:11, `LogLevel debug` still on |
| Pi services after a session | `systemctl is-active cups cp1025-dashboard painel` and `curl -s localhost:8025/api/status \| head -c 300` | as `WORKING.md` says | done at 11:24 and 12:01 |

## Rules for whoever runs these
- Do not stop `cp1025-dashboard` for a quick look: one status read at a time is the rule, and it already reads every 10 s. Stop it only for a long manual session on the Pi, and start it again.
- Manual USB reads on the Pi need `sudo`; without it `hpstatus.py` says `printer not found on USB`, which looks like a switched-off printer.
- Do not print test pages on a loop: each color page spends toner that is already at 50% and about 250 pages.

# hp-cp1025-monitor: knowledge index

Snapshot: `docs/mac-queue-airprint-2026-11-18` @ `dc5f250` (2026-11-21, first written 2026-11-18); `docs/FINDINGS.md` and `docs/raspberry/product-config.xml` are modified and this folder is untracked, nothing committed or pushed. The 2026-11-21 part was verified live on the Pi and the Mac between 13:07 and 13:38 (UTC-3); see [smart-install-outage.md](smart-install-outage.md). Verified live on the Pi and on the owner's Mac between 11:10 and 12:03 (UTC-3) on 2026-11-18. Not read: the printer's panel, the printed pages, the HDMI monitor, the source of `pi-painel`. Facts rot: re-measure a counter or a state before acting on it.

## Scope of this folder
Only what two sessions did, found and left open: 2026-11-18 ("printing from the Mac does not work", a Letter vs A4 bug) and 2026-11-21 ("the Pi does not print any more", the printer stuck in HP Smart Install mode). The long-lived knowledge base of the repo is [../docs/FINDINGS.md](../docs/FINDINGS.md), the working rules are in [../docs/WORKING.md](../docs/WORKING.md), the Pi build is in [../docs/raspberry/README.md](../docs/raspberry/README.md). Read those for everything else about the printer, the status page and the Pi. Nothing here repeats them except where this session proved something they got wrong or left open.

## TL;DR
- Printing from the owner's Mac through the Pi **works again** (2026-11-18 11:24 onward). Proof is the printer's own page counter: 1,329 to 1,330 for a test page, then 1,330 to 1,344 for the owner's real 14-page document (job 22).
- Cause: the Mac queue used the "Generic PostScript Printer" driver, which sends **Letter**; the Pi driver draws an **A4-only** canvas, so `rastertopksm` refused every page (`raster is larger than the page`, jobs 19 and 20).
- Fix: the Mac queue was switched in place to IPP Everywhere (AirPrint), A4 only, default black and white. **That fix lives only on the Mac, not in this repo.** Nothing on the Pi was changed.
- Not yet judged: how the pages look on paper. Not yet committed: the docs of this session.

## Status at a glance
- **2026-11-21, done:** printing works again; cause and fix in [smart-install-outage.md](smart-install-outage.md). The printer sat in HP Smart Install mode (a virtual CD-ROM, no printer interface), and the GET_DEVICE_ID that CUPS and the status page send put it back there. `SmartInstall=disabled` written over LEDM at 13:34 and added to `docs/raspberry/product-config.xml`; Pi job 24 printed, counter 1,344 to 1,345 at 13:36:33.
- **2026-11-21, next:** power-button test passed at 13:49:27 (straight to printer mode, 153 status reads by 14:01 without a flip); next is commit and PR this branch. See [open-items.md](open-items.md).
- **Done:** root cause found and reproduced without paper; Mac queue switched; one page and one real 14-page document proven by the counter; FINDINGS and this folder updated. See [current-implementation.md](current-implementation.md) and [mac-printing.md](mac-printing.md).
- **Discovered:** Letter against an A4 canvas (above); the Mac and Pi job numbers differ; the CUPS job is "completed" about 2 minutes before the last page; an app's color choice overrides the queue default (all 14 pages came out in color); the printer was off the USB bus from 2026-11-13 to 2026-11-18 08:04; `docs/raspberry/README.md` still says the Mac is broken. See [mac-printing.md](mac-printing.md), [pi-observations.md](pi-observations.md).
- **Next steps (ordered):** the owner looks at the paper; commit and PR the docs branch; fix the stale lines in `docs/raspberry/README.md`; record the Mac client setup in the repo; follow up the printer leaving USB only if it happens again. Detail and owners in [open-items.md](open-items.md).
- **What's left:** paper quality, job 17's empty page, why the PostScript route waited 12.5 minutes, who switched the printer off. All unproven. See [open-items.md](open-items.md).
- **Monitor:** the page counter after each Mac job, the printer on USB, power (`get_throttled`), the Mac queue still on IPP Everywhere, toner after color jobs. Commands and normal values in [monitoring-runbook.md](monitoring-runbook.md).

## Topics
| File | What it holds |
|---|---|
| [current-implementation.md](current-implementation.md) | How a Mac print travels today, the values measured, what this session changed and did not, how to rebuild the Mac queue |
| [mac-printing.md](mac-printing.md) | The incident: root cause, reproduction without paper, the two proofs on paper, traps |
| [monitoring-runbook.md](monitoring-runbook.md) | How to prove a print, symptom to first check, the standing watch list |
| [open-items.md](open-items.md) | Next steps in order, what is unverified, decisions for the owner, stale docs outside this folder |
| [smart-install-outage.md](smart-install-outage.md) | The 2026-11-21 outage: printer stuck as an HP Smart Install CD-ROM, GET_DEVICE_ID as the trigger, the LEDM fix, the manual unlock command, what is unproven |
| [pi-observations.md](pi-observations.md) | Things seen on the Pi that were not touched (`painel` crash loop until it was disabled on 2026-11-21, under-voltage, USB drops, debug log) |

## Names and numbers used in these files
- **Mac queue** `HP_CP1025___impressora_pi` (the owner's Mac). **Pi queue** `HP_CP1025` (on the Pi). The same print has a different job number on each. Mac job 25 is Pi job 20 (proven: the Mac's CUPS log says `job-id integer 20`). Mac job 28 is Pi job 22 (matched by the identical size, 7,874,560 B, and a 4 s gap; the remote id was not read). Match by the remote id in the Mac's CUPS log (`job-id integer N`) when it is still there; the Mac log rotates at 1 MB.
- **Canvas** 4960 x 7016 dots = A4 at 600 dpi, what `cp1025-foo2zjs` gives `rastertopksm` on the Pi. A4 content arrives as a 4766 x 6825 raster (margins of 11.52 pt); Letter as 5100 x 6600.
- **Status page / counter** `http://impressora-pi.local:8025/api/status`, key `counter_rows` (`Páginas no total`, `Preto e branco`, `Coloridas`), refreshed every 10 s.
- **Times** are local (UTC-3) unless marked. Pi and Mac timestamps are compared to within a few seconds; clock skew between them was not measured.

## Session log
- 2026-11-18 11:10 to 11:40 (the failing job 20 had been sent at 10:47): investigated "printing on the Pi does not work". Read the repo docs and the Pi's CUPS log; found jobs 19 and 20 failing with `rastertopksm: raster is larger than the page`; reproduced it on the Pi with test PostScript pages; switched the Mac queue to IPP Everywhere (A4 only, Gray); proved a one-page test (counter 1,329 to 1,330) and then the owner's real 14-page document, job 22 (1,330 to 1,344, color). Found the printer had been off USB since 2026-11-13. Edited `docs/FINDINGS.md` (Current state, Next steps, Known bugs, Done 19) and created this folder. Nothing changed on the Pi except a temporary folder that was removed. No commit, no push.
- 2026-11-18 12:00: re-measured and reorganized this folder into the five topic files above; counter still 1,344, Mac queue unchanged, Pi services as before.
- 2026-11-21 13:07 to 13:38: "the Pi does not print any more". Found port 4 failing to enumerate (12:58 to 13:10, cause unknown), then, after the owner pulled and moved the USB cable (now port 5), the printer enumerating as an HP Smart Install CD-ROM. Switched it by hand with `usb_modeswitch`, proved that GET_DEVICE_ID flips it back, wrote `SmartInstall=disabled` over LEDM, added it to `docs/raspberry/product-config.xml`, added a paragraph and a settings row to `docs/FINDINGS.md`, re-sent the owner's PDF (Pi job 24, counter 1,344 to 1,345). Created [smart-install-outage.md](smart-install-outage.md); updated [open-items.md](open-items.md), [monitoring-runbook.md](monitoring-runbook.md), [pi-observations.md](pi-observations.md). On the Pi only the printer setting changed (temp files `/tmp/cp-*-1121*.xml` left). No commit, no push.
- 2026-11-21 17:00 to 17:06: `painel` crash loop. Cause: no HDMI display at boot, so no `/dev/fb0`. The owner said the monitor is removed for good and chose to disable the service (`systemctl disable --now painel`). Updated [pi-observations.md](pi-observations.md), [open-items.md](open-items.md), [monitoring-runbook.md](monitoring-runbook.md), [current-implementation.md](current-implementation.md), `docs/WORKING.md`, `docs/FINDINGS.md`, `docs/raspberry/README.md` and `README.md`. Nothing else changed on the Pi.

# hp-cp1025-monitor: incident, printing from the Mac (2026-11-18)

Snapshot: `docs/mac-queue-airprint-2026-11-18` @ `dc5f250` (2026-11-18). Measured on the Pi (CUPS log, `rastertopksm` run by hand, a raster header read by a throwaway script) and on the Mac (`lpadmin`, `lpoptions`, `ipptool`, `lp`). Not read: the document of job 20 (not found on disk by size or name), the Mac's CUPS log before 10:57 (it rotates at 1 MB and had lost the start of the job).

Related: [current-implementation.md](current-implementation.md) (the fix as applied), [open-items.md](open-items.md) (what is still unproven), [monitoring-runbook.md](monitoring-runbook.md), [../docs/FINDINGS.md](../docs/FINDINGS.md), [../driver/macos/README.md](../driver/macos/README.md).

## Symptom
The owner printed a PDF from the Mac to the Pi (Mac job 25 = Pi job 20, sent 10:47, title of a 7.8 MB PDF) and nothing came out. CUPS ended the job with `Job stopped due to filter errors`. Pi job 19 (Mac, 2026-11-13) had failed the same way and was cancelled by CUPS after 10,800 s (`Canceling stuck job after 10800 seconds`).

## Root cause
The Mac queue `HP_CP1025___impressora_pi` used the "Generic PostScript Printer" PPD, whose default page is Letter. Mac jobs reached the Pi as `application/vnd.cups-postscript`, so the Pi ran only `gstoraster` then `cp1025-foo2zjs` (no `pstops`, so no rescaling). The Pi PPD is A4 only (`setup.sh` deletes the Letter lines), and `cp1025-foo2zjs` gives `rastertopksm` a 4960 x 7016 canvas when the page size is A4. A Letter page at 600 dpi is 5100 x 6600, wider than 4960:
```
E [13/Nov/2026:17:07:47 -0300] [Job 19] rastertopksm: raster is larger than the page
E [18/Nov/2026:11:00:36 -0300] [Job 20] rastertopksm: raster is larger than the page
```
`rastertopksm.c` (`if (header.cupsWidth > page_w || header.cupsHeight > page_h) die("raster is larger than the page")`) is doing what it was written to do; the wrong input was the Letter page.

Probably not a regression. Proven: Mac job 25 is Pi job 20. Inferred, not matched one by one: Mac jobs 23 and 24 are Pi jobs 17 and 19 (same dates, same order), which would mean every Mac job through the Pi failed since the queue was added on 2026-10-22. The Pi access log also holds a 78 KB job on 2026-10-26 10:32 outside that series; the counter moved one page between 2026-10-26 and 2026-11-18 and nobody checked whether that job was it.

## Reproduction without paper (use this again)
Raster size straight from `gstoraster`, then the driver binary on that raster. Run on the Pi in a `mktemp -d` folder (delete it after); no printer access, no USB. `t_a4.ps` and `t_letter.ps` were one-page hand-written PostScript files with `<< /PageSize [595 842] >> setpagedevice` and `[612 792]`.
```
sudo cupsfilter -i application/vnd.cups-postscript -m application/vnd.cups-raster -p /etc/cups/ppd/HP_CP1025.ppd t_a4.ps     > t_a4.ras
sudo cupsfilter -i application/vnd.cups-postscript -m application/vnd.cups-raster -p /etc/cups/ppd/HP_CP1025.ppd t_letter.ps > t_letter.ras
/usr/lib/cups/filter/rastertopksm 4960 7016 < t_a4.ras     > /dev/null   # exit=0  DEBUG: rastertopksm wrote 1 page(s)
/usr/lib/cups/filter/rastertopksm 4960 7016 < t_letter.ras > /dev/null   # exit=1  ERROR: rastertopksm: raster is larger than the page
```
First-page header of each raster (CUPS raster v3, little endian: `cupsWidth` at byte 4+372, `cupsHeight` at 4+376, `PageSize` at 4+352):
```
t_a4      cupsWidth 4766 cupsHeight 6825 pageSizePt 595 842 dpi 600 600 -> fits
t_letter  cupsWidth 5100 cupsHeight 6600 pageSizePt 612 792 dpi 600 600 -> TOO BIG
```
A PostScript made by the Mac's own filters (`cupsfilter -p <mac generic ppd> -m application/vnd.cups-postscript /etc/hosts`) gave the same 5100 x 6600 and the same driver error. The header-reading script was throwaway and was not kept; the offsets above are enough to write it again.

Traps found while doing it (each cost a wrong first result):
- `cupsfilter -m printer/HP_CP1025` does **not** run the driver. The output has the same size as the input (262 B, 110,623 B) and only `gziptoany` ran. Use `-m application/vnd.cups-raster` and run `rastertopksm` by hand.
- Without `-i application/vnd.cups-postscript`, `cupsfilter` detects `application/postscript` and inserts `pstops`, which rescales to A4 (`DEBUG: Page = 595x842`) and hides the bug.
- The Pi has no `zjsdecode`; a ZJS file of about 1.5 KB for a text page is not proof of a good page. Only the printer's page counter is.

## Proof 1: one page (Mac job 27 = Pi job 21)
A one-page A4 PDF (`cupsfilter -p <new mac ppd> -m application/pdf file.txt`, `/MediaBox [0 0 595.2756 841.8898]`), sent with `lp -d HP_CP1025___impressora_pi`:
```
[Job 21] File of type application/pdf queued ...
[Job 21] Started filter pdftopdf / gstoraster / cp1025-foo2zjs, Started backend usb
[Job 21] rastertopksm wrote 1 page(s)
[Job 21] Job completed.            (11:23:50 to 11:24:05, 15 s)
counter: 1.329 -> 1.330, black and white 874 -> 875, color 455 -> 455
```

## Proof 2: the owner's real document (Mac job 28 = Pi job 22, 11:32)
A 7,874,560 B PDF printed from the browser's print dialog, watched with an ssh loop on the Pi every 10 s (`lpstat -p`, `/api/status`).
```
[Job 22] Queued 11:32:35; Ghostscript command line 11:32:44    (9 s; the PostScript job 20 needed 12.5 min)
[Job 22] gstoraster exited with no errors 11:34:18; "rastertopksm wrote 14 page(s)" 11:34:21; "Job completed." 11:34:39   (2 min 4 s)
counter: 1.330 (875 b/w, 455 color) at job start and still at 11:34:41; 1.337 at 11:35:05; 1.344 (875, 469) stable from 11:36:58   (4 min 23 s after the job)
```
14 pages generated = 14 pages counted. No error, nothing left in either queue. At 12:01 the counter still read 1,344.

What to remember from it:
- The CUPS job is "completed" about 2 minutes before the last page: the printer keeps printing from its buffer and the status page shows `processando`. Read the counter only after the printer is idle and the value stops changing.
- The job carried `ColorModel=Color` (sent by the browser's dialog), so Ghostscript ran in CMYK and all 14 pages used color toner, although the Mac queue default is Gray and the Pi queue default is monochrome. An app's explicit color choice wins; to print black and white, pick it in the print dialog.

## Rule that came out of it
**Success is the printer's page counter going up, not the CUPS job state.** Job 17 (2026-10-26) also ended "completed" and printed an empty page.

## Matching a Mac job to a Pi job
The Mac log of an IPP job prints the remote id (`job-id integer 20` under `[Job 25]`). `lpstat -W completed -o` on the Mac shows the size of the source PDF (7,875,584 B for jobs 25 and 26), while the Pi access log shows the size of the PostScript body (42,092,870 B for Pi job 20): same job, different bytes. Mac jobs 25 and 26 were both cancelled from the Mac at 11:05:42 to 11:05:43; Pi job 20 was cancelled at 11:05:44.

## What is still unproven from this incident
Moved to [open-items.md](open-items.md) so it has one home: paper quality, job 17's empty page, the 12.5 minute wait of job 20, other Generic PostScript clients.

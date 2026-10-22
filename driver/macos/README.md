# CP1025 driver for macOS (foo2zjs, no HP driver)

Status: **works on paper** since 2026-10-22 16:01 on macOS 14.6.1 (Intel): a one-page A4 document printed from the normal macOS Print dialog, black and white, sent as 71,095 bytes of ZJS (CUPS job 22). Color has only been checked by decoding the generated ZJS back into an image, not on paper yet.

## Why this exists

| Attempt | Result |
|---|---|
| Apple's "HP Printer Drivers" 5.1.1 (`hpcp1020` filter) | installs only after patching its macOS version check, then turns every document into a blank 464-byte page |
| Driverless URF over USB | the printer interface is class 7/1/2, not IPP-over-USB; jobs hang |
| A CUPS filter calling Homebrew Ghostscript + foo2zjs | works from the shell, fails inside CUPS: the macOS CUPS sandbox does not let filters reach `/usr/local` (`Ghostscript not found`) |
| **This driver**: Apple's `cgpdftoraster` + `rastertopksm` + foo2zjs | **works** |

Two macOS sandbox limits shaped the design:
1. A filter cannot run Homebrew's Ghostscript (or read its libraries under `/usr/local/opt`), so the page is drawn by Apple's own `cgpdftoraster`, which already runs inside the sandbox.
2. A filter cannot open `/dev/stdin` by path (`Operation not permitted`), so the filter keeps its inherited stdin instead of redirecting from it.

## How it works

```
PDF from the Print dialog
  -> cgpdftoraster   (macOS, /usr/libexec/cups/filter)   CUPS raster at 600 dpi
  -> cp1025-foo2zjs  (this filter, bash)                  picks page size and copies
     -> rastertopksm (C, libcups)                         raster -> 4 PBM planes, C M Y K
     -> foo2zjs      (C, from the foo2zjs project)        planes -> ZJS for the printer
  -> usb backend     (macOS)
```

- The PPD asks `cgpdftoraster` for **1-bit black** (`ColorModel=Gray`, `cupsColorSpace 3`) or **8-bit chunky CMYK** (`ColorModel=CMYK`, `cupsColorSpace 6`). Those are the only useful formats it supports: it rejects 1-bit CMYK and every planar order (tested 2026-10-22).
- `rastertopksm` centres the raster (which covers only the imageable area, 4766 x 6825 dots for A4) on a full-page canvas and always writes four planes. Black-only pages leave C, M and Y empty, and foo2zjs then sends a black-only page. CMYK is reduced to 1 bit per color with an 8x8 Bayer ordered dither.
- foo2zjs options come from the official `foo2zjs-wrapper` for this model: `-z3 -P -L0 -c`, clipping `-u 96x96 -l 96x96` at 600x600, A4 = `-p9 -g4960x7016`, Letter = `-p1 -g5100x6600`, `-m1 -d1 -s7`, copies passed with `-n` (the PPD sets `cupsManualCopies: False`, so the printer makes the copies).
- Default is black and white, A4. Color is picked per job in the Print dialog.

## Files

| File | Installed as |
|---|---|
| `cp1025-foo2zjs` | `/Library/Printers/foo2zjs/cp1025-foo2zjs` (root:wheel 755) |
| `rastertopksm.c` | compiled to `/Library/Printers/foo2zjs/rastertopksm` (root:wheel 755) |
| `HP_CP1025-foo2zjs.ppd` | `/Library/Printers/PPDs/Contents/Resources/HP LaserJet CP1025 foo2zjs.ppd` (root:wheel 644) |
| foo2zjs (built from source, see below) | `/Library/Printers/foo2zjs/foo2zjs` (root:wheel 755) |

Filters must be owned by root and not group/world writable, or CUPS refuses them.

## Install from scratch

```bash
# 1. Build foo2zjs (the copy used: github.com/koenkooi/foo2zjs, commit e04290d, 2021-04-26)
git clone --depth 1 https://github.com/koenkooi/foo2zjs.git /tmp/foo2zjs
cc -O2 -w -o /tmp/foo2zjs/foo2zjs /tmp/foo2zjs/foo2zjs.c /tmp/foo2zjs/jbig.c /tmp/foo2zjs/jbig_ar.c
cc -O2 -w -o /tmp/foo2zjs/zjsdecode /tmp/foo2zjs/zjsdecode.c /tmp/foo2zjs/jbig.c /tmp/foo2zjs/jbig_ar.c   # optional, for checking output

# 2. Build rastertopksm (needs Xcode command line tools; libcups ships with macOS)
clang -O2 -Wall -Wextra -o /tmp/rastertopksm driver/macos/rastertopksm.c -lcups

# 3. Install (admin password)
sudo mkdir -p /Library/Printers/foo2zjs
sudo install -o root -g wheel -m 755 /tmp/foo2zjs/foo2zjs /tmp/rastertopksm driver/macos/cp1025-foo2zjs /Library/Printers/foo2zjs/
sudo install -o root -g wheel -m 644 driver/macos/HP_CP1025-foo2zjs.ppd "/Library/Printers/PPDs/Contents/Resources/HP LaserJet CP1025 foo2zjs.ppd"

# 4. Create the queue (your user must be in the _lpadmin group, admins are)
lpinfo -v | grep -i 'CP%201025'          # copy the usb:// URI
lpadmin -p HP_CP1025 -D "HP CP1025" -L USB -E -o printer-is-shared=false \
  -v 'usb://Hewlett-Packard/HP%20LaserJet%20CP%201025?serial=<serial>' \
  -P "/Library/Printers/PPDs/Contents/Resources/HP LaserJet CP1025 foo2zjs.ppd"
lpoptions -d HP_CP1025
```

Keep the HP driver's CP102x PPDs out of `/Library/Printers/PPDs/Contents/Resources`, or macOS may pick the broken one again when the printer is re-plugged. On the author's Mac they were moved, not deleted, to `/Library/Printers/hp-cp1020-backup-20261022/` together with `/Library/Printers/hp/laserjet/cp1020series`; moving them back restores the HP driver.

## Checking output without paper

```bash
PPD=driver/macos/HP_CP1025-foo2zjs.ppd /usr/libexec/cups/filter/cgpdftoraster 1 me t 1 "ColorModel=CMYK PageSize=A4" doc.pdf \
| CP1025_DRIVER_DIR=/path/with/rastertopksm-and-foo2zjs PPD=driver/macos/HP_CP1025-foo2zjs.ppd \
  driver/macos/cp1025-foo2zjs 1 me t 1 "ColorModel=CMYK PageSize=A4" > out.zjs
zjsdecode < out.zjs | grep -c ZJT_START_PAGE      # one per page
zjsdecode -d page < out.zjs                        # writes page-01-1.pbm .. -4.pbm (C, M, Y, K)
```

A black-only page shows no `ZJT_START_PLANE`; a color page shows four.

## Logs

`/private/var/log/cups/error_log` (needs `cupsctl --debug-logging` for the `DEBUG:` lines): look for `cp1025-foo2zjs page=...`, `rastertopksm wrote N page(s)` and `Sent N bytes`. A blank job from the old HP driver shows `Sent 464 bytes`.

## Known limits
- Color quality: plain 8x8 Bayer dither, no color profile. foo2zjs ships a CP1025 profile (`hp-cp1025-rgb-392-bpp1.icm`) that is not used yet.
- Only A4 and US Letter.
- macOS no longer reports toner levels for this queue (that came from the HP driver). The status monitor in this repo reads them over USB instead.

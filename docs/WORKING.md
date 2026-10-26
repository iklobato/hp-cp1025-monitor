# Working on this repo

For any agent or person picking this up. What the project is: [README.md](../README.md). Everything known, the current state and the next steps: [docs/FINDINGS.md](FINDINGS.md). The Pi print server: [docs/raspberry/README.md](raspberry/README.md).

## Read first

1. [docs/FINDINGS.md](FINDINGS.md): "Current state", "Next steps, in order", "Known bugs", "Not verified yet". Take the first open next step unless the owner asks for something else.
2. [docs/raspberry/setup.sh](raspberry/setup.sh): every step that set up the Pi. If it is not in the script, it was done by hand and is listed as such in the Pi README.
3. The code: `hpstatus.py` (USB and LEDM) -> `printer_model.py` (reading) -> `poller.py` -> `viewmodel.py` (all wording) -> `web_dashboard.py` + `dashboard.html` / `dashboard.py`.

## Where things run

| What | Where |
|---|---|
| Printer | USB of a Raspberry Pi 3, `impressora-pi.local`, `ssh impressora` from the owner's Mac (user `iklo`, `sudo` without password) |
| Print queue | CUPS queue `HP_CP1025` on the Pi, shared as AirPrint "HP CP1025 @ impressora-pi" |
| Status page | systemd `cp1025-dashboard`, port 8025, runs `~/hp-cp1025-monitor` on the Pi as a detached checkout of `main` |
| HDMI monitor | systemd `painel`, a separate repo ([pi-painel](https://github.com/iklobato/pi-painel)); it reads `/api/status` from port 8025 |

## Rules that come from real breakage

- **One USB reader at a time.** A status reading holds the USB device; CUPS may then not find the printer. The service reads every 10 s. Before a long manual session on the Pi, `sudo systemctl stop cp1025-dashboard`, and start it again after.
- **Manual reads on the Pi need `sudo`** (or the `lp` group). Without it `hpstatus.py` says `printer not found on USB`, which looks like the printer is off when it is not. Check `lsusb | grep 03f0:112a` before concluding the printer is off.
- **Never leave the Pi without its services.** End every Pi session with `systemctl is-active cups cp1025-dashboard painel` and one look at `curl -s localhost:8025/api/status`.
- **Do not edit files inside `~/hp-cp1025-monitor` on the Pi.** `setup.sh` checks out `main` there; local edits make that fail. Test new code from a temp folder (`mktemp -d`, copy the `.py` files and `dashboard.html`, run on another port such as 8099), then delete the folder.
- **`pkill -f` with a pattern that is also in your own `ssh` command kills that command.** Match on something only the target process has, or use the PID.
- **Settings:** read `/DevMgmt/<Name>Cap.xml` first, `PUT` only fields marked `readWrite` with a value from its list, then read `<Name>Dyn.xml` back. A setting that must survive a rebuild or a printer reset goes into `docs/raspberry/product-config.xml` (applied by `setup.sh`), and into the settings table in FINDINGS.
- **When a print fails**, read the job in the CUPS log on the Pi before guessing: `sudo grep "\[Job N\]" /var/log/cups/error_log`, then the status page.

## Change, test, ship

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider test_printer_summary.py   # no printer needed
ruff check . && black --check . && shellcheck docs/raspberry/setup.sh
```

- Code is formatted with `isort` then `black`; keep it so, or the next edit reformats lines nobody touched.
- One branch and one PR per change; the owner has had them merged with `gh pr merge N --rebase --delete-branch`.
- Deploy to the Pi after a merge (about 1 min):

```bash
ssh impressora 'cd ~/hp-cp1025-monitor && git status --short && git fetch -q origin \
  && git checkout -q --detach origin/main && git rev-parse --short HEAD \
  && sudo systemctl restart cp1025-dashboard && sleep 20 \
  && systemctl is-active cups cp1025-dashboard && curl -s localhost:8025/api/status | head -c 300'
```

  `git status --short` must print nothing; if it prints files, stop and find out who changed them. A change to `setup.sh` or the driver needs a full `bash ~/hp-cp1025-monitor/docs/raspberry/setup.sh` instead (about 2 min).

## Privacy (the repo is public)

Before every push, search the diff for: the printer serial, UUID and USB serial, cartridge serials, LAN and IPv6 addresses, and the owner's town or employers. Never commit `history.jsonl` (gitignored) or invoices. Screenshots must mask every serial (see FINDINGS, Maintenance notes). The home LAN address is already in old commits; do not add it again.

## Keeping the docs true

- After any change, update FINDINGS: "Current state" (with the date and time measured), "Next steps", "Done", and the Pi README if the Pi changed.
- Write numbers you measured, with when; re-measure before writing a state table, do not copy an old one.
- Say what was not checked. "Merged" is not "running on the Pi", and "running" is not "checked on paper".

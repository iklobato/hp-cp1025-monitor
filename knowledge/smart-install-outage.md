# hp-cp1025-monitor: outage of 2026-11-21 (printer stuck in HP Smart Install mode)

Snapshot: `docs/mac-queue-airprint-2026-11-18` @ `dc5f250` (2026-11-21); `docs/FINDINGS.md` and `docs/raspberry/product-config.xml` modified, nothing committed or pushed. Measured live on the Pi (`dmesg`, `lsusb -v`, CUPS `error_log` and `access_log`, `hpstatus.py` used as a library, `usb_modeswitch`) and on the owner's Mac (`lp`) between 13:07 and 13:38 (UTC-3). Not read: the printer's panel LEDs (asked, never answered), the printed page itself, the Pi's power supply. Facts rot; re-check before acting on a number or line ref.

Related: [README.md](README.md), [open-items.md](open-items.md), [monitoring-runbook.md](monitoring-runbook.md), [pi-observations.md](pi-observations.md), [../docs/FINDINGS.md](../docs/FINDINGS.md) (Known bugs, "Fixed 2026-11-21" paragraph; settings table row `SmartInstall`).

## Symptom
Owner sent a 1-page PDF from the Mac at 13:00:25 (Pi job 23). It sat on `Waiting for printer to become available` every 5 s and the Mac cancelled it at 13:07:34:
```
access_log: ... 13:00:25 "POST /printers/HP_CP1025" Send-Document successful-ok
            ... 13:07:34 "POST /printers/HP_CP1025" Cancel-Job successful-ok
error_log:  D [21/Nov/2026:13:07:31 -0300] [Job 23] Waiting for printer to become available.
```
The job's data file was gone afterwards (only `c00023` left in `/var/spool/cups`), so it could not be restarted.

## What was wrong, in the order it was found
1. **12:58 to 13:10, hub port 4: no enumeration at all.** `lsusb` had no `03f0:112a`; the hub said a device was there but the port never enabled:
```
$ sudo lsusb -v -s 001:002 | grep -A5 "Hub Port Status"
   Port 4: 0000.0101 power connect
$ sudo dmesg -T | grep port4
[Sat Nov 21 12:58:46 2026] usb 1-1-port4: Cannot enable. Maybe the USB cable is bad?
...
[Sat Nov 21 13:09:45 2026] usb 1-1.4: device not accepting address 11, error -71
[Sat Nov 21 13:10:33 2026] usb 1-1-port4: unable to enumerate USB device
```
   Three software port resets (`echo 1 > /sys/bus/usb/devices/1-1:1.0/1-1-port4/disable`, then `0`), one with `usbcore old_scheme_first=Y`, all failed the same way. Pulling the printer's mains cord for 30 s changed nothing on the bus (no new `dmesg` line), which suggests the printer's USB side stays powered by VBUS (its descriptor asks 98 mA). **Not proven.** Pulling the USB cable from the Pi dropped `connect` (13:17:20). **Cause of this stage is not known**; it did not come back after the replug.
2. **13:21:45 onward: enumerates, but as a virtual CD-ROM.** Once the cable went back (port 4, then the owner moved it to port 5 at 13:21:57) the printer came up with one mass storage interface, a different USB serial string (letters `SI` where the normal one has `PR`) and no printer or LEDM interface:
```
usb 1-1.5: SerialNumber: ...SI..
usb-storage 1-1.5:1.0: USB Mass Storage device detected
scsi 0:0:0:0: CD-ROM            HP       Smart Install    1.0
$ sudo lsusb -v -s 001:028 | grep -E "bNumInterfaces|bInterfaceClass"
    bNumInterfaces          1
      bInterfaceClass         8 Mass Storage
```
   Effects: `lpinfo -v` lists `usb://HP/LaserJet%20CP%201025?serial=<SI serial>`, not the queue's URI, so jobs wait forever; the status page fails in `hpstatus.LedmClient.__init__` with `StopIteration` (no `ff/04` interface) and shows "leitura falhou".
3. **Nothing on the Pi switches it out.** `usb-modeswitch-data` 20191128 ships the HP Smart Install recipe only for the P1102 (`03f0:002a`, `03f0:032a`: `TargetClass=0x07`, `MessageContent="555342431234567800000000000006d0000000000000000000000000000000"`, a bulk-only CBW with SCSI opcode `0xd0`). There is no entry for `03f0:112a`, and `/lib/udev/rules.d/40-usb_modeswitch.rules` has no rule for it.
4. **The same recipe works for the CP1025 by hand**, and printer mode then shows `7/1/2` + `ff/04/01` with the normal serial:
```
$ sudo usb_modeswitch -v 0x03f0 -p 0x112a -M 555342431234567800000000000006d0000000000000000000000000000000
 OK, message successfully sent ... Device is gone, skip any further commands
usb 1-1.5: SerialNumber: ...PR..
```
5. **A printer-class GET_DEVICE_ID puts it straight back into Smart Install**, with no USB disconnect: the kernel keeps the printer-mode view (`/sys/bus/usb/devices/1-1.5/serial` = PR) while live descriptor reads return the CD-ROM. CUPS's usb backend and the status page both send GET_DEVICE_ID on every look, so the switch never held while either ran. One variable at a time, status page stopped:
```
switch, idle 60 s, then one GET_DESCRIPTOR   -> PR, [(7,1,2), (255,4,1)]     (stays)
PORT_STATUS before [(7, 1, 2), (255, 4, 1)]  -> result 0x18 -> after [(7, 1, 2), (255, 4, 1)]   (stays)
DEVICE_ID   before [(7, 1, 2), (255, 4, 1)]  -> result MFG:Hewlett-Packard;... -> after [(8, 6, 80)]   (flips)
```
   Trap: after a flip the kernel and the device disagree, and `usb_modeswitch` then finds the cached printer interface and sends nothing. Reset the port first (`disable` 1 then 0) so the device re-enumerates as the CD-ROM, then switch.

## Fix applied (2026-11-21 13:34)
- `ProductConfigCap.xml`: `<prdcfgcap:SmartInstallSupport access="readWrite">` with `enabled` and `disabled`. `ProductConfigDyn.xml` had `<dd:SmartInstall>enabled</dd:SmartInstall>` under `prdcfgdyn2:ProductSettings` (Smart Install software dated 2012-12-25).
- PUT with the repo file (status page stopped, printer in printer mode reached without GET_DEVICE_ID):
```
$ sudo python3 hpstatus.py --put /DevMgmt/ProductConfigDyn.xml /tmp/cp-product-config-1121.xml
PUT /DevMgmt/ProductConfigDyn.xml HTTP/1.1 200 OK
read back:  <dd:AutoOffTime>never</dd:AutoOffTime>  <dd:SmartInstall>disabled</dd:SmartInstall>
```
- `docs/raspberry/product-config.xml` now carries `<dd:SmartInstall>disabled</dd:SmartInstall>`, so `setup.sh` re-applies it (`setup.sh` line ~100, the same PUT as `AutoOffTime`). Undo: the same PUT with `enabled`.
- After the PUT: `DEVICE_ID before [(7, 1, 2), (255, 4, 1)] ... after [(7, 1, 2), (255, 4, 1)]` (no flip); `lpinfo` shows the queue's own URI again; status page `reads: 3, read_ms: 184`.
- Proof on the printer: the same PDF sent again from the Mac (`lp -d HP_CP1025___impressora_pi -o media=A4 ~/Downloads/Detalhamento_do_Agendamento.pdf`, Mac job 30 = Pi job 24):
```
[Job 24] rastertopksm wrote 1 page(s)
I [21/Nov/2026:13:36:08 -0300] [Job 24] Job completed.
counter_rows "Páginas no total": 1.344 (13:36:22) -> 1.345 (13:36:33)
```

## Power-button test (2026-11-21, after the fix)
The owner's power-button cycle (reported done; the only USB event after the request is 2026-11-21 13:49:27) brought it back straight in printer mode, with no Smart Install CD-ROM: `usb 1-1.5: USB disconnect, device number 35` then `New USB device found ... SerialNumber: ...PR..` in the same second; by 14:01:17 the status page had made 153 reads (each sends GET_DEVICE_ID) and the printer was still in printer mode, `lpinfo` listing the queue's own URI.
```
$ ssh impressora 'sudo dmesg -T | grep -E "1-1\.[2-5]|Smart Install" | tail -3'
[Sat Nov 21 13:49:27 2026] usb 1-1.5: USB disconnect, device number 35
[Sat Nov 21 13:49:27 2026] usb 1-1.5: New USB device found, idVendor=03f0, idProduct=112a, bcdDevice= 0.01
[Sat Nov 21 13:49:27 2026] usb 1-1.5: SerialNumber: ...PR..
/api/status at 14:01:17: reads 153, counter 1.345, no feed_error
```
Caveat: nobody watched the press itself; the disconnect and reconnect in one second is read as the formatter rebooting while its USB side stays on VBUS (not proven). Re-check after the next real power cut.

## Not verified
- Why Smart Install never blocked printing from 2026-10-22 to 2026-11-18 with the same setting `enabled`. On 2026-11-18 08:04 it came up with the SI serial and left it by itself within 3 s ([pi-observations.md](pi-observations.md)).
- The cause of the port 4 failures from 12:58 to 13:10 (stage 1). Port 4 enumerated fine at 13:21:45; the cable is on **port 5** now.
- Why the Pi rebooted at 2026-11-20 19:26 (no apt run explains it; the journal is volatile, so the previous boot left nothing).

## What not to do
- Do not read the printer by hand while `cp1025-dashboard` runs during this kind of test: its first read (device id) flips the printer back. `sudo systemctl stop cp1025-dashboard`, test, then start it ([../docs/WORKING.md](../docs/WORKING.md), "one USB reader at a time").
- Do not force the bus to full speed (`dwc_otg.speed=1`) or reset the whole hub (`usbreset` on 001/002): Ethernet sits on the same hub and SSH would drop.
- Do not add a udev `usb_modeswitch` rule for `112a` as the fix: the switch does not hold while CUPS sends GET_DEVICE_ID. With Smart Install disabled it is not needed. Add it only if the printer is ever seen in Smart Install with the setting still `disabled`.

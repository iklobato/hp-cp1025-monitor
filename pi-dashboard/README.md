# Pi dashboard: a carousel on the Pi's HDMI monitor

The Raspberry Pi that serves the printer also drives a monitor. This folder is the program that fills it: a carousel of screens with the market, interest rates, news, weather, fuel prices, lottery results, and the printer and the Pi themselves. It draws with Pillow straight into the Linux framebuffer (`/dev/fb0`), so there is no desktop, no browser and no API key. Every source is public.

Everything shown, and how long each screen stays, is set on a small web page on the home network: `http://<pi-address>:8026`.

## Screens (default `carrossel.example.toml`)

| Screen | What it shows |
|---|---|
| Mercado | 4 big quotes with 1d, 1w and 1m charts; a table of stocks; Brazil and market news; a technology ticker; open/closed marks for B3, NYSE, currencies and crypto |
| Juros | Selic, CDI, dollar (market and PTAX), US 3m/5y/10y/30y, IPCA and IPCA-15, Focus survey, Tesouro bonds, each with 1d/1w/1m/1y; Tesouro curve now and a year ago; consumer credit rates; more indicators |
| Notícias | 5 topics, 5 stories each with a photo, a featured story, watched words in amber; a narrow weather column beside it |
| Clima | now, next 24 h, days ahead, rain of the last 7 days, sun and moon; rain radar over a map, INMET alerts, air quality, a second city |
| Combustível e Loteria | ANP weekly pump prices: 6 fuels with this week against last week, the 3 cheapest stations; every Caixa lottery, biggest next prize first |
| Impressora e Pi | the printer monitor's verdict, ports, toner, pages, queue, live changes, event log, USB link (from `http://127.0.0.1:8025/api/status`); the Pi every second: cores, clock, memory, disk, network, top processes, services, journal errors, ports, USB |

A screen can show one block or split between several, side by side (`lado`) or stacked (`empilhado`), each block in a full (`cheio`) or compact (`compacto`) layout.

## The settings page

- `http://<pi-address>:8026`, served by the dashboard itself. It answers only private addresses (home network, loopback); anything else gets 403. It listens on IPv4 only.
- It edits screens, their order and times, the blocks on each screen, how often each block fetches data, and what each block shows: quotes, watched words, news sources, cities, fuel city and stations, lottery games.
- Saving checks the file with the same rules the carousel uses, refuses a wrong value with a message and leaves the file alone, keeps the previous file as `carrossel.toml.bak`, and the carousel picks up the new file at its next screen switch. Saving from the page drops the comments in the file.

## Install on the Pi

```bash
sudo apt install python3-pil fonts-dejavu-core fonts-dejavu-extra
mkdir -p ~/dashboard && cp pi-dashboard/* ~/dashboard/
cp ~/dashboard/carrossel.example.toml ~/dashboard/carrossel.toml
python3 ~/dashboard/screen.py selftest /tmp/dashboard-selftest   # every module's checks, no internet needed
sudo cp ~/dashboard/dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now dashboard
```

The service runs as `iklo` with the `video` and `tty` groups on `tty1` (it takes the console over from getty). `screen.py preview DIR` saves one PNG per screen from live data, without touching the monitor.

To change it safely: copy the folder to a scratch directory on the Pi, run `screen.py selftest` there and check its exit code, then copy it to `~/dashboard` and restart the service. To see what is really on the monitor, read `/dev/fb0` and open it with Pillow as `Image.frombytes("RGB", (1920, 1080), raw, "raw", "BGR;16")`.

Data is cached in `~/.cache/pi-dashboard/`. The big files that change rarely (the 14.6 MB Tesouro file, the ANP weekly surveys) are kept in `downloads/` for 12 hours and 7 days, so a restart does not download them again; a file is kept only after it was read fine.

## Files

| File | Role |
|---|---|
| `screen.py` | the carousel, the framebuffer writer, the background refresher, `run`, `preview`, `selftest` |
| `layout.py` | reads and checks `carrossel.toml`; notices when it changes |
| `fields.py` | the settings each block takes and how they are checked |
| `admin.py`, `admin.html` | the settings page |
| `ui.py` | the page base class, colors, fonts, charts |
| `sources.py` | every data source (Yahoo chart API, RSS, Banco Central, IBGE, Tesouro, Open-Meteo, INMET, RainViewer, Esri tiles, ANP, Caixa, local commands) |
| `procfs.py` | the Pi's numbers read from `/proc` and `/sys` |
| `page_*.py` | one module per block |

## Things found while building it

- CARTO map tiles came back HTTP 200 with "API KEY REQUIRED" drawn on the picture; the map uses Esri tiles. A 200 does not prove the picture is right: look at it.
- Some g1 pages declare their share picture as the word `None`; a bad picture address now means a story without a photo instead of a failed refresh.
- Yahoo returns an empty quote before the first trade of the day. Eletrobras trades as AXIA3.
- The Tesouro site answers HTTP 429 to `curl`'s default user agent but 200 to the dashboard's (`Mozilla/5.0 (pi-dashboard)`): test sources with `curl -A` and the same agent. On 2026-10-23 its newest prices were 8 days old, and reading the file takes about 16 s of CPU on a Pi 3.
- ANP writes city names without accents and sometimes repeats a city's prices from one week to the next.
- Caixa's Super Sete endpoint answered HTTP 500 on every check from 11:41 to at least 15:45 on 2026-10-23, on every address tried, including Caixa's own summary of all games: the error is on Caixa's side.
- The public EMBI (country risk) series stopped on 2024-07-30, so it is shown only as a note.
- Redrawing half the screen and converting it costs about 0.3 s of CPU on a Pi 3, so live blocks redraw only when their data changed (the printer) or every second (the Pi).

## Status (2026-10-23) and what is still open

- Running on the Pi since 2026-10-23; this folder is the exact code on it.
- Measured: about 22% of one core on average over one carousel lap; the Pi at 52 to 60 C with no throttling; the dashboard process between 188 and 306 MB. Whether memory settles over hours is being measured.
- Not seen yet: the printer screen with a full reading (the printer was off the whole time).
- Open: install it from `setup.sh`; decide whether the settings page should answer on the tailnet once Tailscale is on; record the underlying network error when a download fails ("sem conexao" twice right after a restart, cause unknown); the city name is drawn twice on the map for a big city that the map already labels.

# Pi dashboard: a carousel on the Pi's HDMI monitor

The Raspberry Pi that serves the printer also drives a monitor. This folder is the program that fills it: a carousel of screens with the market, interest rates, news, weather, fuel prices, lottery results, and the printer and the Pi themselves. It draws with Pillow straight into the Linux framebuffer (`/dev/fb0`), so there is no desktop and no browser. Every data source is public. The only key is optional: with `OPENROUTER_API_KEY` set, each news story gets a short summary in Portuguese.

Everything shown, and how long each screen stays, is set on a small web page on the home network: `http://<pi-address>:8026`.

## Screens (default `carrossel.example.toml`)

Each screen stays 30 s, Mercado e Juros 45 s, so one lap takes 2 min 45 s.

| Screen | What it shows |
|---|---|
| Mercado e Juros | one screen, split 30/70. Left: 4 highlighted quotes and a list of stocks, each with its 1d, 1w and 1m change as numbers with an arrow (no charts). Right: Selic, CDI, dollar (market and PTAX), US 3m/5y/10y/30y, IPCA and IPCA-15, Focus survey, Tesouro bonds with 1d/1w/1m/1y changes as numbers; the Tesouro curve now and a year ago; consumer credit rates; 16 more indicators (real rate, IGP-M, INPC, savings, TR, jobless, GDP, IPCA this year, IBC-Br, gross debt, euro, reserves, Focus for this year and next); open/closed marks for B3, NYSE, currencies and crypto |
| Notícias | the only screen with news: 5 topics, 3 stories each, every card with a photo, the title and a short summary in Portuguese (also for English sources); a featured story; watched words in amber; a narrow weather column beside it. Text flows around the photo and is never cut: a summary that does not fit is left out |
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

Data is cached in `~/.cache/pi-dashboard/`. Summaries are kept in `summaries.json` there, with what was spent each month. The big files that change rarely (the 14.6 MB Tesouro file, the ANP weekly surveys) are kept in `downloads/` for 12 hours and 7 days, so a restart does not download them again; a file is kept only after it was read fine.

## Container (optional, not used on the Pi)

`Dockerfile` builds the same code with Pillow and the DejaVu fonts; the image holds no config. The Pi runs the systemd service instead: in a test on 2026-10-23 the Docker daemon alone took about 71 MB of the Pi's 905 MB, and four blocks of the Pi screen stay empty in a container. The image is not published.

```bash
docker build -t pi-dashboard pi-dashboard
docker run -d --name dashboard --user 0:0 --cap-add SYS_TTY_CONFIG \
  --device /dev/fb0 --device /dev/tty1 -p 8026:8026 \
  -e PI_DASHBOARD_PRINTER_URL=http://<pi-ip>:8025/api/status \
  --env-file /etc/pi-dashboard/openrouter.env \
  -v ~/dashboard:/config -v ~/.cache/pi-dashboard:/root/.cache/pi-dashboard \
  --entrypoint sh pi-dashboard -c "exec python3 /app/screen.py run < /dev/tty1"
```

- `PI_DASHBOARD_CONFIG` (default `~/dashboard/carrossel.toml`) and `PI_DASHBOARD_PRINTER_URL` (default `http://127.0.0.1:8025/api/status`) move the config and the printer reading. `/config` must be a writable folder, not a single file, because the settings page replaces the file.
- It runs as root: the `dashboard` user cannot open `/dev/tty1` (`620 root:tty`).
- Empty in a container: print queue (`lpstat`), power (`vcgencmd`), services (`systemctl`) and journal (`journalctl`). Ports, connections and processes show only the container's own unless it runs with `--network host` and `--pid host`.
- A run as root leaves root-owned files in the cache folder: `sudo chown -R $USER: ~/.cache/pi-dashboard` before going back to the service.

## Files

| File | Role |
|---|---|
| `screen.py` | the carousel, the framebuffer writer, the background refresher, `run`, `preview`, `selftest` |
| `layout.py` | reads and checks `carrossel.toml`; notices when it changes |
| `fields.py` | the settings each block takes and how they are checked |
| `admin.py`, `admin.html` | the settings page |
| `ui.py` | the page base class, colors, fonts, charts |
| `summary.py` | the news summaries: asks OpenRouter once per story, keeps the text for 3 days, stops at US$ 3 a month |
| `sources.py` | every data source (Yahoo chart API, RSS, Banco Central, IBGE, Tesouro, Open-Meteo, INMET, RainViewer, Esri tiles, ANP, Caixa, local commands) |
| `procfs.py` | the Pi's numbers read from `/proc` and `/sys` |
| `page_*.py` | one module per block |

## News summaries

- Turned on by `OPENROUTER_API_KEY` in the service's environment. On the Pi it is in `/etc/pi-dashboard/openrouter.env` (root only, mode 600), read by `EnvironmentFile=` in `dashboard.service`. Never put it in the repo, the toml or a log. Without it the cards show photo and title only.
- Model `qwen/qwen3.7-flash`, with `deepseek/deepseek-v4-flash` if the first fails; thinking is turned off (it is billed and a summary does not need it). Each story is asked once; if the text does not fit the card, once more asking for a shorter one; if it still does not fit, the card has no summary.
- The cost of each answer comes back with it and is added up per month; nothing more is asked once the month reaches US$ 3.
- Picked by trying 5 models on 10 real stories (2026-10-23): qwen was the cheapest and kept the facts and the size; Claude Haiku ignored the size (up to 363 characters); GPT-5-mini refuses to answer with thinking turned off.

## Things found while building it

- CARTO map tiles came back HTTP 200 with "API KEY REQUIRED" drawn on the picture; the map uses Esri tiles. A 200 does not prove the picture is right: look at it.
- Some g1 pages declare their share picture as the word `None`; a bad picture address now means a story without a photo instead of a failed refresh.
- Yahoo returns an empty quote before the first trade of the day. Eletrobras trades as AXIA3.
- The Tesouro site answers HTTP 429 to `curl`'s default user agent but 200 to the dashboard's (`Mozilla/5.0 (pi-dashboard)`): test sources with `curl -A` and the same agent. On 2026-10-23 its newest prices were 8 days old, and reading the file takes about 16 s of CPU on a Pi 3.
- ANP writes city names without accents and sometimes repeats a city's prices from one week to the next.
- Caixa's Super Sete endpoint answered HTTP 500 on every check from 11:41 to at least 15:45 on 2026-10-23, on every address tried, including Caixa's own summary of all games: the error is on Caixa's side.
- The public EMBI (country risk) series stopped on 2024-07-30, so it is shown only as a note.
- Memory grew without stopping: 306 MB to 599 MB in 61 minutes, about 75 MB every 10 minutes. The same work in the main thread stayed flat and grew only in new threads, so the cause is glibc keeping one memory pool per thread. With `MALLOC_ARENA_MAX=2` in `dashboard.service` one process stayed between 100 and 194 MB for 2 h 23 min.
- Drawing is not what makes the carousel slow: a full screen takes at most about 1 s of CPU (about 0.4 s of it is the color conversion for the framebuffer) and is drawn 4 s before it shows.
- Redrawing half the screen and converting it costs about 0.3 s of CPU on a Pi 3, so live blocks redraw only when their data changed (the printer) or every second (the Pi).

## Status (2026-10-23 20:26) and what is still open

- Running on the Pi since 2026-10-23; `main` is the code on it.
- Measured: about 22% of one core on average over one carousel lap; the Pi at 52 to 60 C with no throttling; the dashboard process between 100 and 194 MB over 2 h 23 min with `MALLOC_ARENA_MAX=2`. Over a full day it is not measured yet.
- Summaries: 93 written in the first 2 h 23 min, all of them fit, US$ 0.00125 spent. That includes the first fill of every story on screen, so a month should cost well under US$ 0.40; a full day is not measured yet. Some summaries almost repeat the title.
- The OpenRouter key used today has no spending limit and is shared with other uses: the US$ 3 cap in the code only guards against this program's own mistakes. A key just for the dashboard, with its own limit, is safer.
- Not seen yet: the printer screen with a full reading (the printer was off the whole time).
- Open: install it from `setup.sh`; decide whether the settings page should answer on the tailnet once Tailscale is on; record the underlying network error when a download fails ("sem conexao" twice right after a restart, cause unknown); the city name is drawn twice on the map for a big city that the map already labels; the "pages per day" line has no data yet.

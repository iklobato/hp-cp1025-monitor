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

- `http://<pi-address>:8026`, served by the dashboard itself. It answers only private addresses (home network, loopback); anything else gets 403. It listens on IPv4 only. It also refuses a request addressed to any name other than an IP or the Pi's own name (403, so a website pointed at the Pi cannot read it), a save that is not JSON (415) or that comes from another page's Origin (403), and a broken length (400). Every time in `carrossel.toml` is at least 1 s.
- It edits screens, their order and times, the blocks on each screen, how often each block fetches data, and what each block shows: quotes, watched words, news sources, cities, fuel city and stations, lottery games.
- Every screen and every block starts folded; the folded line shows the time and the split (`45 s · Mercado 30% + Juros 70%`). Redraw, live interval and data refresh are under "Avançado". Sizes are percents; with two blocks, changing one changes the other. Yahoo lists are one line per stock (`PETR4.SA Petrobras`); news sources are a table (they can no longer be moved up or down). The page warns about unsaved changes and before leaving. Closed, the page is 1,064 px tall (it was 6,277 px with everything open).
- Cities are typed by name. When a name changed, saving looks up its IBGE code (the IBGE list of 5,571 cities, kept 30 days in `downloads/`) and its coordinates (Open-Meteo geocoding, matched by state) and writes them; a name used in more than one state asks for `Name, UF`; a city not found refuses the save and leaves the file alone. The message says which city was found (`cidade achada: Campinas (SP)`).
- Saving checks the file with the same rules the carousel uses, refuses a wrong value with a message and leaves the file alone, keeps the previous file as `carrossel.toml.bak`, and the carousel picks up the new file at its next screen switch. Saving from the page drops the comments in the file, and the first save writes sizes as percents (`30` instead of `0.3`, same split).

## Install on the Pi

[`setup.sh`](../docs/raspberry/setup.sh) installs it with the print server (step "HDMI monitor"): packages, a selftest from the repo, a copy to `~/dashboard`, `consoleblank=0` and `dashboard.service`. An existing `carrossel.toml` is kept. The OpenRouter key is the one thing to add by hand, in `/etc/pi-dashboard/openrouter.env`.

The service runs as `iklo` with the `video` and `tty` groups on `tty1` (it takes the console over from getty). `screen.py preview DIR` saves one PNG per screen from live data, without touching the monitor.

To change it safely: copy the folder to a scratch directory on the Pi, run `screen.py selftest` there and check its exit code, then copy it to `~/dashboard` and restart the service. To see what is really on the monitor, read `/dev/fb0` and open it with Pillow as `Image.frombytes("RGB", (1920, 1080), raw, "raw", "BGR;16")`.

Data is cached in `~/.cache/pi-dashboard/`. Summaries are kept in `summaries.json` there, with what was spent each month. The big files that change rarely (the 14.6 MB Tesouro file, the ANP weekly surveys) are kept in `downloads/` for 12 hours and 7 days, so a restart does not download them again; a file is kept only after it was read fine.

## Working on this code (read before changing it)

How a change was made and checked on 2026-10-24 (PRs #14 to #20). Follow it so a change does not break the monitor in the house.

- **Where things run.** The code runs only on the Pi (`ssh impressora`): the DejaVu fonts, `/dev/fb0` and the `vcgencmd`/`systemctl` readings exist only there. On a Mac the data modules' selftests run (`net`, `news_data`, `market_data`, `weather_data`, `rates_data`, `city_data`, `lottery_data`, `fuel_data`, `system_data`, `summary`, `fields`, `layout`, `admin`); anything that draws text fails with `OSError: cannot open resource`, which only means the fonts are missing.
- **Test on a copy, never on `~/dashboard` first.** Copy the folder to `/tmp/dash-stage-<name>` on the Pi and run `python3 screen.py selftest /tmp/dash-stage-<name>/out`. Check the exit code, not the text: a `grep` on the output once let a failure through. The selftest also saves one PNG per block in `out/`: look at them.
- **Refactors must not move a pixel.** Run the selftest on the old and the new code into two folders and compare every PNG with `cmp`. Also draw every block with no data yet, full and compact, through `screen.compose_part` (the "carregando…" state), before and after. The renders are deterministic: the same code gives byte-identical PNGs.
- **Bug fixes start with a selftest case that fails on the old code.** Prove it fails, then fix.
- **The settings page without touching the real config.** From the stage folder: `admin.serve(screen.build_blocks(), Path("<copy of carrossel.toml>"), 8027)`, then try it from a browser on the LAN. Stop it by its pid (`ss -ltnpH "sport = :8027"`); `pkill -f` over `ssh` matches the ssh command itself and kills the session.
- **Install.** Back up the files being replaced, copy them into `~/dashboard`, `sudo systemctl restart dashboard` (the screen is blank for about 20 s), then check `systemctl is-active dashboard` and `journalctl -u dashboard --since "-1min"` for `ERROR`/`Traceback`. A file removed from the repo must also be deleted from `~/dashboard`, because `setup.sh` copies with `cp -r` and never deletes.
- **See the real screen.** Read the framebuffer on the Pi: `Image.frombytes("RGB", (1920, 1080), open("/dev/fb0", "rb").read(1920 * 1080 * 2), "raw", "BGR;16")`. `screen.py preview DIR` saves every screen with live data (about 65 s, and it calls the real sources).
- **Formatting.** The repo uses `ruff format` and `ruff check` (ruff 0.13). An editor that runs isort or black reformats lines that are not part of the change; put those lines back so the diff holds only the change.
- **House rules.** No `subprocess` (shell commands go through `system_data.run_command`: `os.system` with output to a file). No numpy. The repo is public: no personal city, word, address or key in code, tests or docs; personal values live only in the Pi's `carrossel.toml`. Tests use Campinas, Sorocaba and São Paulo.

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

## How it runs

- `screen.py run` builds every block once (`build_blocks`: one `ui.Page` object per block id named in `carrossel.toml`), reads the plan (`layout.load`), starts the settings page thread (port 8026), the `Refresher` thread and the display loop (`Carousel.run`).
- **Data:** the `Refresher` calls each block's `refresh(now)` on its own interval (`[dados]` in the toml) in a pool of 4 threads; a block still busy is skipped, so a slow download never delays the 1 s Pi readings. `refresh()` replaces whole attributes, never mutates them, so the display thread always reads a complete snapshot. A source that fails is recorded in the block's `problems()`, logged once when it starts and once when it clears, and shown in red in the status bar.
- **Display:** each screen stays `na_tela` seconds and is fully redrawn every `redesenho` seconds; blocks marked live (printer, Pi) are redrawn every `ao_vivo` seconds, and only when their `live_key()` changed; the progress line in the status bar moves every 2 s; the next screen is drawn 4 s before the switch. Each block is painted on its own (`compose_part`): a block that raises shows a red error card in its box and the rest of the screen draws as usual.
- **Config reload:** a saved `carrossel.toml` is picked up at the next screen switch; a file the rules reject is logged and the old plan stays.
- **Printer block:** reads the 8025 monitor's `/api/status` every second, the monitor's `history.jsonl` (toner, pages per day) and `lpstat` (queue, jobs) every 30 s, and keeps a copy of the last full reading in `printer-status.json` (at most once a minute) for when the printer is off.

## Files

| File | Role |
|---|---|
| `screen.py` | the carousel, the framebuffer writer, the background refresher, `run`, `preview`, `selftest` |
| `layout.py` | reads and checks `carrossel.toml`; notices when it changes |
| `fields.py` | the settings each block takes and how they are checked |
| `admin.py`, `admin.html` | the settings page |
| `ui.py` | the page base class, colors, fonts, charts |
| `summary.py` | the news summaries: asks OpenRouter once per story, keeps the text for 3 days, stops at US$ 3 a month |
| `net.py` | what every data source shares: the HTTP fetch with retries, the download cache, the text helpers |
| `*_data.py` | one module per data source: `news_data` (RSS), `market_data` (Yahoo chart API), `rates_data` (Banco Central, IBGE, Tesouro), `weather_data` (Open-Meteo, INMET, RainViewer, Esri tiles), `city_data` (IBGE, Open-Meteo), `fuel_data` (ANP), `lottery_data` (Caixa), `system_data` (local commands) |
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
- One news page that arrived cut short (`http.client.IncompleteRead`, not an `OSError`) aborted the whole news refresh: 7 times on 2026-10-23, the screen kept old stories. It is now a retried source error (0 failures from 22:10 to 23:47).
- OpenRouter answers `401 "User not found."` to a provisioning key (a key made only to create other keys) and, on 2026-10-23, to one normal key for a reason not found. `GET /api/v1/key` with the key shows `is_provisioning_key`. The dashboard's key was made from a provisioning key with `POST /api/v1/keys` (`limit` 3, `limit_reset` `monthly`).
- Memory grew without stopping: 306 MB to 599 MB in 61 minutes, about 75 MB every 10 minutes. The same work in the main thread stayed flat and grew only in new threads, so the cause is glibc keeping one memory pool per thread. With `MALLOC_ARENA_MAX=2` in `dashboard.service` one process stayed between 100 and 194 MB for 2 h 23 min.
- Drawing is not what makes the carousel slow: a full screen takes at most about 1 s of CPU (about 0.4 s of it is the color conversion for the framebuffer) and is drawn 4 s before it shows.
- Redrawing half the screen and converting it costs about 0.3 s of CPU on a Pi 3, so live blocks redraw only when their data changed (the printer) or every second (the Pi).
- A feed gave a picture link with raw accents; Python's HTTP client sends ASCII only and raised `UnicodeEncodeError`, which is not an `OSError`, so it escaped `fetch_once` and aborted the whole news refresh (3 times on 2026-10-24, 12:25 to 12:45). Links are percent-encoded now (#14). Same family: a corrupt gzip body raises `EOFError` or `zlib.error`, now a retried source error. Rule from #9 and #14: any new error type a fetch can raise must become a `SourceError` in `net.fetch_once`, or one bad item stops a whole block.
- Thumbnails were written straight to their final name; a power cut mid-write left a cut file that broke every news refresh until it aged out after 3 days. They go through a temporary file now, and a cut file found on disk is fetched again (#14).
- One block raising while drawing used to drop the whole frame: the monitor kept the previous screen while the status bar named the new one (#18 fixed it, see "How it runs").
- The settings page trusted any caller with a private address: any website open on a phone at home could post a new config, and `Content-Length: -1` passed the size check and was read until the client hung up (#17 fixed both).

## Status (2026-10-24 22:53) and what is still open

- Running on the Pi since 2026-10-23; `main` is the code on it (PR #20 installed 2026-10-24, old `sources.py` deleted from `~/dashboard`). All 5 screens were captured from the framebuffer on 2026-10-24 between 21:10 and 21:18 and drew complete.
- Measured: about 22% of one core on average over one carousel lap (2026-10-23); the Pi at 49 to 60 C with no throttling (`throttled=0x0`); the dashboard process between 100 and 194 MB over 2 h 23 min with `MALLOC_ARENA_MAX=2`, 191 MB 14 min after a restart on 2026-10-24. The service was restarted many times on 2026-10-24 for installs, so a full day is still not measured.
- Summaries: US$ 0.0089 spent in October by 2026-10-24 22:53 (`summaries.json`), on the dashboard's own key with a US$ 3 monthly limit. Some summaries almost repeat the title.
- Logged warnings on 2026-10-24 come from the sources, not the code: Caixa Super Sete HTTP 500, Banco Central sometimes answering an HTML error page, Hacker News 502 once.
- Not seen yet: the printer screen with a full reading (it was captured with the printer off; the printer was switched on later).
- Open, in order:
  1. The bugs listed in [docs/FINDINGS.md, Known bugs](../docs/FINDINGS.md#known-bugs-not-fixed-yet) (weather details crash in a short compact box, settings change racing a refresh, printer block "ao vivo" when 8025 is down, market losing its last prices on one failed fetch, shell commands without timeout).
  2. Features chosen on 2026-10-24, not started: the age of each block's data on screen; a strip for urgent items on every screen; next/pin-screen buttons and a screen preview on the settings page; charts from data already downloaded (`Series.closes`, the rates windows' points, `radar.past` for an animated radar). A visual redesign is being drafted separately; port it with every value showing its unit and every card showing when its data is from.
  3. Splitting `page_weather.py` (about 1,550 lines) and `page_system.py` (about 1,450 lines) was started and cancelled on 2026-10-24; nothing was kept. Ask before picking it up again.
  4. Measure a full day (memory and summary cost) and fix the prompt if summaries keep repeating the title; decide whether the settings page should answer on the tailnet once Tailscale is on; record the underlying network error when a download fails ("sem conexao" twice right after a restart, cause unknown); the city name is drawn twice on the map for a big city that the map already labels; the "pages per day" line has no data yet.

# run-autopilot 🏃⚡

Self-hosted autopilot for a runner's data. It watches Garmin Connect, and a few
minutes after every run your Strava activity gets a proper title and a rich,
data-dense description — power, weather physics, blood glucose, training
stress — with zero manual input.

Built for one athlete (T1D, Stryd power meter, tandem insulin pump) on a
Raspberry Pi, then open-sourced. It's opinionated and paths are plain
constants, but every integration is a small standalone client you can steal.

## What a synced activity looks like

**Title** (auto-classified from lap structure + power zones):

> ⚡ 10×1k — 17.0 km 🌬️

**Description:**

```
🌡️ 30.1°C ☀️ Clear sky
💨 Wind: 4.7 km/h W → (gusts 12)
💧 Humidity: 37%

⚡ Weather Impact: 🔴 severe (+14.2 s/km)
  🌡️ temp +12.2s · 💨 wind +0.0s · 💧 humidity +1.9s
  🏃 Actual: 5:08/km → Ideal equiv: 4:54/km
  🧭 Headwind: 50% (balanced)

🩸 Glucose: 8.7 → 12.0 mmol/L (+3.3) 📈
  Range: 8.7–12.3 mmol/L

🎽 Stryd: RSS 76 · CP 304W

📊 Splits:
  #1 · 4:00/km · 327W · 164bpm
  ...
  #10 · 3:55/km · 319W · 169bpm
```

## Features

- **Fully unattended sync** — a systemd timer polls Garmin every 10 minutes,
  downloads the FIT file, and pushes title + description to Strava. Matching
  is by start time + distance, so two runs on the same day never collide.
- **Power-zone workout classification** — work reps are laps ≥45 s at ≥90% of
  your Critical Power (pulled from Stryd for the date of the run). `10×1k`,
  `7×6'`, `4×800m` are detected and named automatically; steady runs are
  binned into Recovery / Easy / Moderate / Tempo / Long Run by %CP.
  Interval/threshold workouts get per-rep splits (pace · power · HR) in the
  description.
- **Weather physics, not weather trivia** — temperature/wind/humidity cost in
  s/km (Ely-based temp model, quadratic wind drag with gusts, dew point), plus
  real headwind exposure computed from the GPS track bearings vs wind
  direction. Weather is fetched for the run's actual coordinates, so it's
  correct when you travel.
- **CGM overlay** — recent runs get the Dexcom Share glucose trend
  (start → end, delta, range). The Share↔UTC clock offset is derived live from
  the latest reading, so timezone travel doesn't shift your data.
- **Pump-upload watcher** — a second timer checks Glooko for new pump data
  (bolus/basal series only, so phone CGM sync doesn't trigger it) and
  backfills insulin/IOB context into processed workouts.
- **Training-load report** — one script aggregates Stryd fitness/fatigue/RSB,
  CP trend, 28-day activity list, weekly volume, and Garmin
  readiness/sleep/HRV/body battery into a single JSON — ready to feed an LLM
  coach or your own dashboard. Includes a 300-day long-run baseline so
  recommendations are judged against *your* history, not textbook defaults.
- **Override protection** — your own titles and descriptions are never
  touched. Only Strava defaults ("Morning Run"), bot descriptions, and the
  tool's previous output are replaced.
- **Rate-limit aware** — Strava 429s are retried at the next quota window;
  per-activity state makes every run idempotent and resumable.

## Architecture

| File | Role |
|---|---|
| `scripts/auto_sync.py` | The engine: poll → FIT → classify → weather → glucose → Stryd → push |
| `scripts/strava_client.py` | Strava OAuth refresh, activity matching, title/description builders |
| `stryd_client.py` | Unofficial PowerCenter API: calendar (RSS/power), CP history, rotating-refresh-token auth |
| `garmin_client.py` | Garmin Connect: paged activity list, FIT download, readiness/sleep/HRV |
| `dexcom_client.py` | Dexcom Share CGM readings + live UTC-offset derivation |
| `glooko_client.py` | Glooko EU web-session client (pump/CGM series) |
| `fit_processor.py` | FIT parsing, lap/interval extraction, workout JSON |
| `scripts/glooko_watch.py` | Detects new pump USB uploads, triggers backfill |
| `scripts/training_report.py` | Aggregated training-load JSON for coaching |
| `scripts/extract_tracks.py`, `scripts/add_weather.py` | GPS track + historical weather enrichment |
| `systemd/` | User units: sync every 10 min, pump watch every 6 h |

State lives in two small JSON files (`auto_sync_state.json`,
`glooko_watch_state.json`); tokens live in `config.ini` and are rotated in
place with merge-on-write (two processes can refresh different sections
without clobbering each other).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.ini.example config.ini   # fill it in — see notes inside
python3 garmin_auth.py             # one-time Garmin login (handles MFA)
.venv/bin/python scripts/auto_sync.py --dry-run --limit 2   # smoke test
```

Then install the timers (adjust paths inside the unit files first):

```bash
cp systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now strava-autosync.timer glooko-watch.timer
```

Backfill your history:

```bash
.venv/bin/python scripts/auto_sync.py --since 2026-01-01
```

## How hard are the integrations?

| Integration | API | Effort | Notes |
|---|---|---|---|
| Strava | Official OAuth | Easy | Create an app, one OAuth dance, auto-refresh forever |
| Open-Meteo | Official, no key | Trivial | Forecast API for recent dates, archive API for backfill |
| Garmin | Unofficial (`garminconnect`/`garth`) | Easy | One interactive login incl. MFA; OAuth1 token lives ~a year |
| Dexcom | Semi-official Share API (`pydexcom`) | Easy | Needs Share enabled; only ~24 h of history |
| Stryd | Unofficial, reverse-engineered | Medium | Email/password login is simple; Facebook/Google SSO accounts must grab tokens from browser localStorage once — rotation keeps them alive after that |
| Glooko | Unofficial web session | Fragile | EU endpoints hardcoded; HTML login can break any time |

## Caveats

- **Personal project.** Paths (`/home/pi/...`), a dashboard hook in
  `glooko_watch.py`, and some defaults (Copenhagen fallback coordinates) are
  constants — grep and adjust. PRs that make them configurable are welcome.
- **Unofficial APIs.** Garmin, Stryd and Glooko clients talk to endpoints that
  are not public contracts. They can break, and you use them under your own
  accounts at your own risk. Be polite with polling intervals.
- **Health data.** Glucose and insulin data are yours; everything stays on
  your machine except what you choose to publish to Strava. Keep `config.ini`
  and the state/results files out of version control (see `.gitignore`).

## License

MIT

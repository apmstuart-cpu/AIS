# yachtspeed: yacht AIS tracking, weather matching and SOG forecasting

Tracks selected sailing yachts over AISStream, matches each report to Open-Meteo wind,
waves and modelled currents at the yacht's time and position, and builds a per-yacht
history for forecasting **speed over ground (SOG)** on future passages.

It runs as two supervised services on an always-on Linux host:

| service | what it does |
|---|---|
| `yachtspeed-collector` | AISStream websocket → SQLite. Class A (msg 1-3) and Class B (18, 19) positions, plus static data (5, 24) used to verify MMSIs. Keeps the first valid report per vessel, source and 10-minute UTC bucket. Reconnects with back-off. |
| `yachtspeed-enricher` | Every 2 min, matches reports at least 90 min old to the nearest UTC hour of weather. Fetches one day per grid cell and caches it. Stays inside a call budget, backs off on failures and processes newest data first. |
| `yachtspeed-backup.timer` | Daily online SQLite backup (gzip, integrity-checked, keeps 14). |

## Install on the server

```bash
git clone <this repo> yacht-speed && cd yacht-speed
sudo ./deploy/install.sh
sudoedit /etc/yachtspeed/env          # AISSTREAM_API_KEY=...   (root-only, 0600)
sudo yachtspeed discover ALIBI --minutes 60   # find/verify the MMSI (expect ~27 m length)
sudoedit /etc/yachtspeed/config.toml  # set mmsi = "...", verified = true
sudo systemctl restart yachtspeed-collector
yachtspeed status
```

Upgrade: `git pull && sudo ./deploy/install.sh` (config, key and data are kept).

## Commands

```
yachtspeed status [--json]           last report per yacht, reconnects, backlog, match rate, API use
yachtspeed samples --limit 5         raw AISStream payloads next to parsed values (parser check)
yachtspeed discover NAME             list MMSIs broadcasting NAME in the bounding box
yachtspeed export out.csv [--which enriched|unmatched|all] [--yacht "ALIBI OF LONDON"] [--since ...]
yachtspeed import-csv hist.csv       historical AIS: mmsi,timestamp_utc,lat,lon,sog_kn,cog_deg,heading_deg,nav_status
yachtspeed enrich --backfill-since 2024-01-01   enrich imported history, then exit
yachtspeed enrich --once --retry-failed         reset failed weather fetches and retry
yachtspeed forecast --yacht "ALIBI OF LONDON" --lat 39.4 --lon 2.9 --time 2026-11-18T10:00Z --course 120 [--distance 60]
yachtspeed passage --yacht "ALIBI OF LONDON" --depart 2026-11-18T08:00Z 39.55,2.63 39.0,3.5 38.9,1.4
yachtspeed evaluate [--method chrono|voyage]     hold-out MAE vs per-yacht median
yachtspeed backtest-leadtime --days 1 3 5        held-out rows scored with N-day-old wind forecasts
yachtspeed backtest-passages                     predicted vs actual voyage durations
yachtspeed evaluate-live                         stored live predictions vs what happened, by lead time
yachtspeed backup | restore FILE | smoke-test
```

Logs: `journalctl -u yachtspeed-collector -f` (JSON lines; persistent across reboots).

## Data semantics

* All times UTC. `provider_time_utc` = AISStream receive time (`MetaData.time_utc`, ms precision);
  `ingested_utc` = when we stored it; `ais_second` = the transponder's UTC-second field (NULL if 60-63).
* Units: SOG kn, COG/heading °true, wind kn, waves m, currents kn (converted from Open-Meteo km/h
  using the units in each response).
* Directions: wind and waves are **from**. Ocean current is **towards**.
* AIS sentinels map to NULL: SOG 102.3, COG 360, heading 511, lat 91, lon 181.
* Enrichment status: `matched` (all key fields), `partial` (some NULL, listed in `missing`,
  e.g. no current model near the coast), `missing` (fetch gave up after 8 tries), and
  `pending` (not yet attempted). Every row records `wind_provider` / `marine_provider`.
* Weather providers: `om_forecast` (last 2 days and future), `om_hist_forecast` (archived
  forecasts, 2022+), `om_prev_dayN` (wind as forecast N days before), `om_marine`.

## Model

A per-yacht nearest-neighbour **SOG** baseline (see `yachtspeed/model.py`). It uses comparable
history on TWS, TWA-to-course and wave height, removes the along-course current from history
and adds back the target's current. It abstains with fewer than 8 matches. The p10–p90 spread
is descriptive, not a calibrated interval. AIS SOG includes current and possibly motoring. The
2 kn and nav-status filters are only a first pass, so this is not a sailing polar.

## Tests

`pip install -e .[test] && pytest` covers AIS parsing and sentinels, bucket de-duplication,
unit conversion, quota pause and back-off, partial and missing weather, hold-out splits,
forecasts and passages, CSV round trips, backup and restore, and collector reconnects
against a local websocket server.

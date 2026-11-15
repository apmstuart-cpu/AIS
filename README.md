# yachtspeed: yacht AIS tracking, weather matching and SOG forecasting

Tracks selected sailing yachts over AISStream, matches each report to Open-Meteo wind,
waves and modelled currents at the yacht's time and position, and builds a per-yacht
history for forecasting **speed over ground (SOG)** on future passages.

It runs as two supervised services on an always-on Linux host:

| service | what it does |
|---|---|
| `yachtspeed-collector` | AISStream websocket → SQLite. Class A (msg 1-3) and Class B (18, 19) positions, plus static data (5, 24) used to verify MMSIs. Keeps the first valid report per vessel, source and 10-minute UTC bucket. Reconnects with back-off. |
| `yachtspeed-enricher` | Every 2 min, matches reports at least 90 min old to the nearest UTC hour of weather. Fetches one day per grid cell and caches it. Stays inside a call budget, backs off on failures and processes newest data first. |
| `yachtspeed-yb.timer` | Every 3 h, imports rally satellite-tracker positions (YB Tracking, e.g. the ARC) for yachts with `yb_race` set. These cover the ocean legs that AIS cannot. |
| `yachtspeed-report.timer` | Hourly at :05, builds `yachtspeed report` (status, four-model ETAs, polar evidence) and force-pushes it as a single commit to the repo's `server-reports` branch, using a separate write deploy key (`~/.ssh/ais_reports`). |
| `yachtspeed-backup.timer` | Daily online SQLite backup (gzip, integrity-checked, keeps 14). |

## Install on the server

```bash
git clone <this repo> yacht-speed && cd yacht-speed
sudo ./deploy/install.sh
sudoedit /etc/yachtspeed/env          # AISSTREAM_API_KEY=...   (root-only, 0600)
sudo systemctl restart yachtspeed-collector  # ALIBI OF LONDON (235099768) is preconfigured
yachtspeed status
```

Upgrade: `git pull && sudo ./deploy/install.sh` (config, key and data are kept).

## Commands

```
yachtspeed status [--json]           last report per yacht, reconnects, backlog, match rate, API use
yachtspeed samples --limit 5         raw AISStream payloads next to parsed values (parser check)
yachtspeed discover NAME             list MMSIs broadcasting NAME in the bounding box
yachtspeed export out.csv [--which enriched|unmatched|all] [--yacht "ALIBI OF LONDON"] [--since ...]
yachtspeed import-yb [--list arc2026 --search sorc]   rally tracker positions (source yb:<race>)
yachtspeed import-csv hist.csv       historical AIS: mmsi,timestamp_utc,lat,lon,sog_kn,cog_deg,heading_deg,nav_status
yachtspeed enrich --backfill-since 2024-01-01   enrich imported history, then exit
yachtspeed enrich --once --retry-failed         reset failed weather fetches and retry
yachtspeed forecast --yacht "ALIBI OF LONDON" --lat 39.4 --lon 2.9 --time 2026-11-18T10:00Z --course 120 [--distance 60]
yachtspeed passage --yacht "ALIBI OF LONDON" --to rubicon            ETA from latest position, sailing only
yachtspeed passage --yacht X --to rubicon --from 33.62,-8.38 --at 2026-11-14T18:03Z   from a position you supply
yachtspeed passage --yacht X --depart 2026-11-18T08:00Z 39.55,2.63 39.0,3.5   explicit waypoints
      (--motor-below 6 --motor-speed 7 to allow motoring; --speed-table none to disable the fallback polar)
yachtspeed passage --yacht X --to rubicon --models ecmwf,aifs,gfs,icon   same passage on 4 forecast models: ETA spread
yachtspeed wind-ahead --yacht "ALIBI OF LONDON" --threshold 10    when will she reach 10 kn on current course/speed
yachtspeed wind-ahead --yacht X --from 33.62,-8.38 --course 238 --speed 4.5 [--below]
yachtspeed report [--out DIR]                    status + four-model ETAs (yachts with `destination`) + polar evidence
yachtspeed polar-check --yacht "ALIBI OF LONDON"    each recorded fix as % of the polar (evidence for polar_factor)
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

## Rally trackers (YB)

Set `yb_race = "arc2026"` (and `yb_team` if the tracker name differs) on a yacht. The timer
then imports its track every 3 hours, and re-runs are de-duplicated. YB provides position
and time only. SOG and COG are **made good** from the fix at least 10 minutes earlier
(typically 1–6 h offshore), so they read low when the yacht tacks or gybes between fixes.
Rows carry `source = yb:<race>` and are weather-matched like AIS rows.

## Weather routing (isochrones)

`yachtspeed route --yacht X --to rubicon|LAT,LON [--models ecmwf,aifs,gfs,icon] [--track]`
finds the fastest track through a gridded forecast using the yacht's polar (raw polar
speed by true wind angle, no-go zone upwind), sea state, current, and a time loss per
tack/gybe (default 3 min). It avoids land using Natural Earth (1:50m worldwide, 1:10m for
the Eastern Atlantic; `yachtspeed fetch-land` downloads 1:10m worldwide). Grid spacing and
time step scale with the passage (0.5 deg/1 h coastal to 1.5 deg/4 h ocean); beyond the end
of the forecast the last hour is held and the result says how many hours rely on that.
`yachtspeed destinations` lists named ports; any LAT,LON works. The hourly server report
includes a routed ETA (`report_route_models`, default ECMWF only to save API calls).
Routes are model output for ETAs, not navigational advice.

## Passage speed when history is thin

With fewer than 8 comparable observations, `passage` uses a generic speed table
(`cruiser27`, a rough polar for a ~27 m cruiser, in `yachtspeed/speedtable.py`) and flags
those steps `[speed_table]`. It never motors unless `--motor-below` is given. Live wind
comes from Open-Meteo, with a MET Norway fallback when Open-Meteo fails or its quota is
used. Named destinations (`rubicon`, `las-palmas`) include approach points that keep the
straight-line model off land. They are planning aids, not navigational advice.

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

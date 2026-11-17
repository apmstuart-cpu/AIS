# Alibi of London: passage model session handover (17 Nov 2026)

Handover from a Claude Code session on the Mac. It covers the weather downloads, the server work, the results and the PDF write-up for Alibi's Lanzarote to Antigua passage. Pass it to the master Alibi session as context.

## 1. The passage being modelled

- **Yacht:** ALIBI OF LONDON (Oyster 885, 2013, standard keel).
- **Route:** Rubicon (Lanzarote, 28.857N 13.817W) to English Harbour (Antigua).
- **Departure:** Fri 20 Nov 2026, 10:00 UTC.
- **Deadline:** Sun 6 Dec 2026, 23:59 Antigua time (07 Dec 03:59 UTC). That allows 16.75 days.
- **Model settings:**
  - Polar: `oyster885_std` × **0.85**.
  - Each tack or gybe costs 3 min.
  - Motoring: 8.5 kn when the wind is below 10 kn, at most 177 h.
  - Fuel: 3,500 L with a 10% reserve, 15 L/h motoring plus 1.25 L/h generator.
  - Waves: ERA5 ocean waves.
  - Current: the mean for these dates over 2022-2025.
  - Past years: 2006-2025.
- This morning's restart ran with `--motor-speed 8 --fuel-l 4500 --burn-lph 25 --genset-lph 2`. The later reports use the settings above, so the runs were re-launched with them at some point (not by this session).

## 2. Infrastructure (as used today)

- **Server:** reached with `ssh scaleway` (Ubuntu, user `ubuntu`). Code checkout `~/ais`, branch `claude/keen-cray-08u47b`, remote `github-ais:apmstuart-cpu/AIS`. Package `yachtspeed` v0.2.0.
- **Installed copy:** `/opt/yachtspeed` (venv `/opt/yachtspeed/.venv`). Update it with `cd ~/ais && git pull && sudo ./deploy/install.sh`. The installer also restarts journald, the enricher and the collector, and re-enables the timers.
- **Config:** `/etc/yachtspeed/config.toml`. `polar_factor = 0.85`, `motor_wind_below_kn = 10`, `motor_speed_kn = 8.5`; skill weighting on, 30-day window, 12 minimum checks.
- **Data:** `/var/lib/yachtspeed/` (owned by user `yachtspeed`, mode 750).
- **Reports:** `/var/lib/yachtspeed/report/`. The `ubuntu` user cannot read it: plain `scp` fails, so copy with `ssh scaleway "sudo cat <file>" > local`. Files:
  - `climate_alibi_antigua{,_75,_80}.{md,json,jpg}`: single-route runs at 85%, 75% and 80% of polar.
  - `climate_alibi_strategies.{md,json,jpg}`: the five routing options.
  - `climate_alibi_antigua_fc.*`
  - `route_fleet_tracks.jpg`
- **Mac copy of the code:** `~/ais-climate` (venv `.venv`, editable install). Refresh it with `rsync -a --exclude .venv scaleway:ais/ ~/ais-climate/`. It is used only to download weather on the Mac's own Open-Meteo allowance.

## 3. What was done today, in order

### Morning: passage climate data (wind and waves)
1. On the Mac: `climate-fetch` for 2021-2025 (wind and waves), then 2006-2020 (`--waves-only`). It produced `~/climate.db` (51 MB). API-limit waits were about 1 min each (per-minute limit).
2. Copied to the server, then the merge failed: `/opt/yachtspeed` was older than `~/ais` and had no `climate-merge` command. Fixed by running only the code-update lines of `install.sh` (rsync to `/opt` and pip install).
3. The merge printed `{'climate_wind': 241680, 'climate_cells': 1007, 'climate_wave': 177600, 'climate_wave_cells': 900}`.
4. Restarted `yachtspeed-climate` (`alibi_antigua`). Before the restart it had been stalled, waiting for weather quota until midnight. Afterwards it routed all 20 years in about 4 minutes, then ran the live forecast. `yachtspeed-climate75` (the 75% run) was queued to start after it.

### Afternoon: polar-history weather (calibrating Alibi's real speed)
1. On the server: `git pull`, `sudo ./deploy/install.sh`, then `history-jobs --yacht "ALIBI OF LONDON"`. That listed 2,286 cell-months over 1,094 cells.
2. On the Mac: `history-fetch --jobs ~/history_jobs.json --db ~/history.db`.
   - **Gotcha:** it exits at any API limit, including the per-minute limit of 150 calls, and does not wait.
   - It resumes safely: completed jobs are tracked in `hist_wx_done`. It needed 6 runs about 65 s apart, roughly 10 minutes in total. All **2,286/2,286** jobs completed, giving a 228 MB file.
3. Merged on the server: `{'hist_wx': 1650477, 'hist_wx_done': 2286}`.
4. `yachtspeed-polarhist` re-ran in about 1 s and reported "0 cell-months still to fetch".

## 4. Results

### Alibi's real speed against the polar (polar-history)
- **Overall 84% of polar** (middle half 77-93%), over 2,173 h of clear sailing. Configured: 85%.
- The polar is Babiana's (Oyster 885 GT) ORC racing VPP: race crew, clean hull and a 624 m² asymmetric spinnaker.
- By wind angle (TWA): 40-70° 90%, 70-110° 86%, **110-150° 81% (1,080 h, the weak zone)**, 150-180° 87%.
- At 110-150° by wind speed: 8-12 kn 88%; 12-16 kn 80%; 16-20 kn 79%; 20+ kn 78%. At 150-180° in 8-12 kn she reaches 102%.
- **Interpretation, discussed with the owner:** she has an **asymmetric G2 gennaker in NorLon**, not a Code 0. The data suggests it is flown in light air and taken down as the wind passes 12-16 kn (an inference: sail usage is not recorded).
  - Keeping it up to about 18 kn, or adding a heavier A3-type sail for 16-22 kn, could lift the weak zone to about 88%. That would take her overall from about 84% to about 87%.
  - The trades are mostly 120-160° in 14-20 kn, so this matters for Antigua. Each point of speed is worth about 3-4 h over the crossing.
  - Offered but not yet run: a "gennaker kept up" passage case, using her measured % at each wind angle with 110-150° in 12-20 kn raised to 88%.

### Five routing options (strategies run, forecast issued 17 Nov 16:17 UTC, 85% polar)

Each option has 20 runs: the live forecast, then one past year's weather.

| Option | On time | Median d | 10-90% | Slowest | Sailing | Motor h | Fuel |
|---|---|---|---|---|---|---|---|
| A: fastest (router's choice) | 20/20 | 13.09 | 12.75-13.65 | 13.86 | 67% | 102 | 1,530 L |
| B: classic, via 22N 30W | 20/20 | 13.48 | 12.98-13.77 | 14.21 | 72% | 90 | 1,350 L |
| C: at most 30 h motoring in the first 72 h | 20/20 | 13.17 | 12.88-13.72 | 13.96 | 74% | 82 | 1,230 L |
| D: motor only below 7 kn | 20/20 | 13.55 | 13.16-14.56 | 14.64 | 91% | 32 | 480 L |
| E: at most 48 h motoring in total | 20/20 | 13.74 | 12.99-14.48 | 15.13 | 85% | 48 | 720 L |

- On past weather alone, every option is about half a day slower: A has a median of 13.7 days. E misses the deadline once (2020 weather, 19/20).
- Takeaways:
  - **C is nearly free:** about 2 h slower than A, with 20 h less motoring and 300 L less fuel. It is identical to A in 14 of 20 past years.
  - **D** is the sailing option: 91% under sail, about 11 h longer.
  - **B** costs about 9 h for no gain: it is slower than A in 17 of 20 years.
  - **E** is the riskiest.

### Polar sensitivity (single fastest route; forecasts issued 09:34 and 15:31 UTC)

| | 85% | 80% |
|---|---|---|
| Forecast, then history: median / slowest | 13.08 / 14.05 d | 13.63 / 14.37 d |
| History alone: median / slowest | 13.70 / 14.65 d | 14.11 / 16.20 d (2022 weather, 13 h in hand) |
| Motoring (forecast / history) | 132 / 118 h | 122 / 134 h |

At 75%: a median of 14.10 days with the forecast, slowest 14.90; still 20/20 on time.

### El Niño
- The ONI is **+2.46** for Aug-Oct 2026: a very strong El Niño, in the same class as 1997 and 2015.
- Past years are weighted w = max(0.1, exp(-((ONI - 2.46)/1)²/2)). Weights: 2015 1.00, 2023 0.86, 2009 0.52, 2018 0.37, 2006 0.29, 2019 0.22, 2014 0.21, the other 13 years 0.10 each. That is about 9.3 effective years.
- El Niño-like years against the rest (fastest route, history): median 13.8 against 13.5 days; median wind 10.6 against 11.5 kn; motoring 148 against 112 h.
- But 2015, the closest analogue, was the fastest year of all: 11.7 days in 18.3 kn.
- Practical reading: a slightly lighter-wind passage with more motoring, and lower late-season hurricane risk (El Niño raises wind shear). The deadline is not at risk.

### How the forecast works (as implemented)
- Four models: ECMWF IFS, ECMWF AIFS, GFS and ICON, blended out to 15 days from issue.
- **Skill weighting:** every day, the forecasts each model issued 1, 2, 3, 5 and 7 days earlier are checked against the consensus analysis (the mean of the four models' latest wind) at tracked yachts' positions.
  - Weights are proportional to 1/MSE over 30 days, clamped to 15-35% per model. A model gets an equal share until it has 12 checks.
  - This run (26,868 checks): AIFS 35% (at the cap), ECMWF 24-26%, ICON 21-25%, GFS 18-20%.
- **Handover:** the forecast is used alone until 2 days after issue. The share of each past year's weather then rises linearly to 100% at 15 days after issue.
  - For this run that means about 6% history at departure, 29% on day 3, 52% on day 6, 75% on day 9 and 100% from day 12.3.
- **Router:** isochrones on a 2.5° grid, 4 h steps, 5° heading steps, land mask.
- **Sea state:** a heuristic; following seas add up to +3%.

## 5. Deliverables (all in `Documents/Alibi/Passage 2026 Atlantic/Passage model 17 Nov/`)
- `Alibi_passage_model_and_routes.pdf`, 10 pages:
  - summary and option tables;
  - how the model works (router, polar calibration, motoring, skill weighting, handover, ENSO weighting);
  - El Niño section;
  - 85% vs 80% table (times only, no extra route maps);
  - one page per option, each with a map of 5 routes (fastest, 25th percentile, median, 75th percentile, slowest; thick lines sailing, thin lines motoring);
  - the original all-options chart.
- `route_option_A..E.jpg` and `alibi_5_options_5_routes.jpg`: the per-option maps (the owner found the original combined chart too hard to read).
- `climate_alibi_strategies.{md,json,jpg}` and `climate_alibi_antigua.{md,json,jpg}`: copies of the server reports.
- `scripts/strategy_panels.py`: draws the per-option maps. Run it with `~/ais-climate/.venv/bin/python scripts/strategy_panels.py <strategies.json> <out.jpg>` from `~/ais-climate`; it imports `yachtspeed.chart._basemap`.
- `scripts/build_pdf.py`: builds the PDF (needs reportlab). Usage: `python build_pdf.py <folder with json and jpgs> <out.pdf>`. Some text and the 85/80 and El Niño numbers are hard-coded from this run.

## 6. Open items / next steps
1. Optional: a "gennaker kept up" passage case (per-angle measured %, with the weak zone raised to 88%) to show the days saved.
2. Re-run the strategies with a fresh forecast nearer departure (Thu 19 / Fri 20 Nov). The handover moves later as the issue time gets closer to departure.
3. Decide on the option. The analysis points to C as the default, or D for a sailing passage.
4. Mac leftovers that can be deleted: `~/climate.db`, `~/history.db` (228 MB), `~/history_jobs.json`. `~/ais-climate` is worth keeping for future downloads.
5. The `climate_friday_route.txt` report on the server shows a `no_route` for a blend run from 16 Nov. It is stale and was not investigated.

## 7. Gotchas learned
- `history-fetch` and `climate-fetch` count their own API budget (150/min, 2,500/h for history-fetch). `history-fetch` exits rather than waits, so rerun it in a loop; it resumes safely.
- `climate-fetch` waits out the per-minute limit by itself (about 1 min each time).
- The report folder on the server is root/yachtspeed-only. Use `sudo cat` over ssh.
- Units started with `systemd-run` vanish from `systemctl list-units` once they finish. Check `journalctl -u <unit>` instead.

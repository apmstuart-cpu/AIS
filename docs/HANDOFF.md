# Handoff and pilot checklist

## Deployment location

* Host: the owner's Scaleway Ubuntu server (`ssh scaleway`).
* Code `/opt/yachtspeed`, venv `/opt/yachtspeed/.venv`, CLI `/usr/local/bin/yachtspeed`.
* Config `/etc/yachtspeed/config.toml`. Secret `/etc/yachtspeed/env` (root 0600, read by systemd only).
* Data `/var/lib/yachtspeed/yachtspeed.db` (SQLite WAL). Backups `/var/lib/yachtspeed/backups/`.
* Units `yachtspeed-collector`, `yachtspeed-enricher`, `yachtspeed-backup.timer`.

## Operating commands

```bash
yachtspeed status                                  # health at a glance
systemctl status yachtspeed-collector yachtspeed-enricher
journalctl -u yachtspeed-collector --since -1h     # JSON logs
sudo systemctl restart yachtspeed-collector        # after config changes
```

## Backup and restore

* Automatic daily at 03:17 UTC: `systemctl list-timers yachtspeed-backup.timer`.
* Manual: `yachtspeed backup`.
* **Off-host copy (recommended)**: backups currently sit on the same disk. Copy them off the
  host, e.g. to Scaleway Object Storage with `rclone copy /var/lib/yachtspeed/backups remote:bucket`
  from a cron job.
* Restore:
  ```bash
  sudo systemctl stop yachtspeed-collector yachtspeed-enricher
  yachtspeed restore /var/lib/yachtspeed/backups/yachtspeed-YYYYMMDDTHHMMSSZ.db.gz
  sudo systemctl start yachtspeed-collector yachtspeed-enricher
  ```
  Previous DB files are kept as `*.pre-restore`.

## Pilot acceptance steps (run on the server)

1. **Confirm identity**: once static data arrives, `yachtspeed status` shows her broadcast name
   and call sign, and flags any mismatch with 2GQX5 / 27 m. (`sudo yachtspeed discover ALIBI`
   remains available for other yachts.) If they don't match, set
   `verified = false` and investigate before using the data.
2. **Live parsing**: after the first reports, run `yachtspeed samples --limit 10` and check SOG
   against what the crew or MarineTraffic shows. Check that Class A rows have a nav status and
   Class B rows have NULL, and that `ais_second` matches the seconds of `provider_time_utc`.
3. **Restarts**: `sudo systemctl restart yachtspeed-collector`, then `sudo reboot`. Afterwards,
   `yachtspeed status` should show fresh heartbeats, `connects_24h` up by one each time, and the
   earlier reports still present.
4. **Sample export**: `yachtspeed export /tmp/alibi.csv --which all --yacht "ALIBI OF LONDON"`. Check that
   timestamps are consistent, weather is matched, and gaps are visible in `enrichment_status`
   and `missing_fields`.
5. **Future lookup**: `yachtspeed forecast --yacht "ALIBI OF LONDON" ...` returns `status: ok` with a median
   once there are 8 or more comparable rows, otherwise `insufficient_support`.

## Verified in the build environment

* 26 automated tests pass. They include a collector disconnect and reconnect against a local
  websocket server, and bucket de-duplication with separate provider and ingestion times.
* Open-Meteo live calls returned correct data: marine forecast, marine archive (2025) and
  historical-forecast wind. Current velocity arrives in km/h and is converted to knots. Hitting the
  provider's daily limit live paused that host until 00:05 UTC, as designed.
* Synthetic smoke test: about 4,300 rows. On chronologically held-out voyages, about 91% of rows
  were scored, with a model MAE of 0.70 kn against 1.79 kn for the per-yacht median baseline.
  This shows the pipeline works. It says nothing about real-world accuracy.

## Not yet verified / known limitations

* **No live AIS yet**: no AISStream key was available in the build environment, so payload
  shapes follow AISStream's documentation and are checked with `samples` on first run.
* **ALIBI OF LONDON: MMSI 235099768, call sign 2GQX5**, taken from a MarineTraffic listing (UK flag, 27 × 6 m, pleasure craft / sailing). The 235 prefix is a UK MMSI, which fits. Still to do: confirm against her own static broadcast; `yachtspeed status` flags any name, call sign or length mismatch. The other "ALIBI" MMSIs (232059724, 235088205, 228481000) are different 12–13 m boats.
* **She was off West Africa in November 2026**, outside the Mediterranean pilot box. A second, Atlantic box (0–50°N, 70°W–7°W) is now configured. Terrestrial AIS coverage off West Africa and mid-Atlantic is thin, so expect long gaps. This is exactly the case where satellite AIS or onboard telemetry is needed.
* **Offshore coverage**: AISStream is terrestrial-receiver based. Expect reports near the coast
  and gaps mid-passage, such as the Balearics–Sardinia crossing or Atlantic crossings. Measure
  this before relying on crossings: use the `reports_24h` and position gaps in exports. If it is
  sparse, evaluate satellite AIS (Spire, ORBCOMM, exactEarth/Spire Maritime) or onboard
  telemetry (Predictwind DataHub, Iridium tracker, NMEA logger).
* **Historical AIS**: AISStream has no history. Backfill needs CSV exports from a paid provider
  or the yacht's own logs, then `import-csv` and `enrich --backfill-since`.
* **Ocean currents** from the marine API are coarse: the archive is quantised to 45° and about
  0.1 kn. Currents are modelled, not observed.
* **Wind lead-time tests**: the Previous Runs API covers wind only. Waves and currents in
  lead-time backtests come from the archive, which is optimistic.
* **SOG ≠ sailing performance**: motoring is filtered only by the 2 kn and nav-status rules. A
  sailing polar needs engine state, heading and water speed, from onboard data.
* **Open-Meteo terms**: the free API is for non-commercial use, up to about 10,000 calls a day.
  Commercial use needs a paid plan. The default budget is 8,000 calls a day.
* **AISStream terms**: free and best-effort, with no SLA. Check its terms before any commercial use.
* `install.sh` is written for Ubuntu with systemd. It could not be run here (no systemd in the
  build container). Its first run on the server is its first real test.

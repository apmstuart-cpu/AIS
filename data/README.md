# Manually captured position fixes

`marinetraffic_fixes.csv` holds fixes read from MarineTraffic screenshots that the owner
takes and drops in a Dropbox folder. A scheduled Claude session reads new screenshots,
appends one row per new fix (de-duplicated on MMSI + fix time) and records the file in
`marinetraffic_processed.txt`. Fix time = screenshot time (from the file name, UTC) minus
"Position received ... ago". On the server, `yachtspeed-fixes.timer` pulls this repo and
imports the CSV (`import-csv`, source `csv:marinetraffic`); re-imports are de-duplicated.

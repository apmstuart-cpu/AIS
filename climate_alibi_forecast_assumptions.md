# Forecast and model assumptions

- **Forecast models:** ecmwf, aifs, gfs, icon, blended (wind averaged as vectors), issued 2026-11-20T21:27:55Z.
- **Model weights** from each model's past accuracy along the route (wind error against the models' consensus analysis, last 30 days), kept within 15-35 % each:
  - 1 d ahead: ecmwf 26%, aifs 35%, gfs 19%, icon 21%
  - 2 d ahead: ecmwf 26%, aifs 35%, gfs 19%, icon 20%
  - 3 d ahead: ecmwf 25%, aifs 35%, gfs 19%, icon 21%
  - 5 d ahead: ecmwf 25%, aifs 35%, gfs 20%, icon 20%
  - 7 d ahead: ecmwf 25%, aifs 32%, gfs 18%, icon 25%
- **Forecast into history:** the forecast's weight fades steadily from 2 days after issue to zero at 15 days; for this run from day 2.2 to day 15.2 of the passage. Each of 21 past years' weather (ERA5, 1997, 2006-2025) then takes over: 21 possible endings.
- **El Nino weighting:** ONI 2.46 (ASO 2026); past years weighted by how close their Oct-Dec ONI was (2015 (1.0), 1997 (0.993), 2023 (0.855) most).
- **Yacht:** polar measured x 1.0; 3 min per tack or gybe; motor at 8.5 kn below 10.0 kn of wind (option D: below 7 kn), at most 177 h of motoring within the fuel consumption limitation.
- **Point of sail:** never deeper than 150 deg off the wind under sail (broad reach, no dead running or crash gybes); she gybes instead.
- **Sea state:** ERA5 ocean waves for past years and wind-derived seas in the forecast; following seas add up to +3 % (surfing), head and beam seas slow her.
- **Current:** mean surface current for these dates over 2022-2025.
- **Not modelled:** squalls, gusts, crew fatigue, breakages, sail changes beyond the polar.

**Wind maps: drawn by this model from its own skill-weighted forecast blend, not taken from Windy.** Every 3 h for the first 10 days, area (17, -56, 33, -11), colours by wind strength, white streaks flowing with the wind; the coloured dots are each option's most typical routed course at that hour.

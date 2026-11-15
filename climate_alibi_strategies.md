# ALIBI OF LONDON: routing strategies

Leaving Sat 28 Nov 09:00 UTC, deadline 07 Dec 03:59 UTC. Forecast issued 28 Nov 09:29 UTC (ecmwf, aifs, gfs, icon, blended), its weight fading steadily from day 2.0 of the passage to zero by day 15.0, with each past year's weather taking over. Polar measured x1.0, motor at 8.5 kn below 10.0 kn of wind, at most 192 h. Status: complete.

Forecast model weights from past accuracy (18356 checks): 1 d ahead: ecmwf 25%, aifs 35%, gfs 19%, icon 20%; 2 d ahead: ecmwf 26%, aifs 35%, gfs 19%, icon 20%; 3 d ahead: ecmwf 25%, aifs 35%, gfs 20%, icon 21%; 5 d ahead: ecmwf 26%, aifs 35%, gfs 20%, icon 20%; 7 d ahead: ecmwf 25%, aifs 32%, gfs 18%, icon 25%

| Strategy | On time | Probability | El Nino-weighted | Median days | 10-90 % days | Slowest | Sailing | 10+ kn | Median motoring | Median fuel |
|---|---|---|---|---|---|---|---|---|---|---|
| A: fastest | 0/21 | 0% | 0% | 13.52 | 13.23-13.8 | 13.93 | 48% | 49% | 170.0 h | 2550 L |
| B: classic: south-west to 22N 30W first | 0/21 | 0% | 0% | 14.19 | 13.57-14.57 | 14.68 | 59% | 62% | 138.5 h | 2078 L |
| C: fuel-saving: at most 30 h motoring in the first 72 h | 0/21 | 0% | 0% | 13.49 | 13.2-13.72 | 14.43 | 52% | 51% | 156.0 h | 2340 L |
| D: more sailing: motor only below 7 kn of wind | 0/21 | 0% | 0% | 13.81 | 13.52-14.54 | 14.79 | 84% | 53% | 53.7 h | 806 L |
| E: most wind: maximise the time in 10+ kn of wind (no deeper than 140 deg) | 0/21 | 0% | 0% | 15.55 | 14.87-16.48 | 16.61 | 95% | 95% | 19.5 h | 292 L |
| F: via Cape Verde: round 17.5N 25.5W, north-west of Santo Antao (no deeper than 140 deg) | 0/21 | 0% | 0% | 15.72 | 15.2-16.4 | 16.48 | 78% | 78% | 85.1 h | 1276 L |

## In past years' own weather (no forecast)

| Strategy | On time | Median days | 10-90 % days | Slowest | Sailing | Median motoring | Median fuel |
|---|---|---|---|---|---|---|---|
| A: fastest | 0/21 | 13.89 | 12.95-14.82 | 15.44 | 81% | 68.0 h | 1020 L |
| B: classic: south-west to 22N 30W first | 0/21 | 14.23 | 13.47-15.05 | 15.22 | 77% | 79.5 h | 1192 L |
| C: fuel-saving: at most 30 h motoring in the first 72 h | 0/21 | 13.89 | 12.95-15.0 | 15.44 | 81% | 68.0 h | 1020 L |
| D: more sailing: motor only below 7 kn of wind | 0/21 | 13.99 | 13.13-15.43 | 15.72 | 94% | 21.9 h | 328 L |
| E: most wind: maximise the time in 10+ kn of wind (no deeper than 140 deg) | 0/21 | 14.63 | 13.66-16.47 | 17.27 | 93% | 24.3 h | 364 L |
| F: via Cape Verde: round 17.5N 25.5W, north-west of Santo Antao (no deeper than 140 deg) | 0/21 | 15.62 | 15.2-16.4 | 16.62 | 74% | 95.5 h | 1432 L |

Year by year in past weather (days; + = slower than A, the fastest):

| Year | A days | B days | B vs A | C days | C vs A | D days | D vs A | E days | E vs A | F days | F vs A |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1997 | 15.44 | 15.22 | -5 h | 15.44 | +0 h | 15.72 | +7 h | 17.27 | +44 h | 16.52 | +26 h |
| 2006 | 14.41 | 14.29 | -3 h | 14.41 | +0 h | 13.77 | -15 h | 15.37 | +23 h | 15.62 | +29 h |
| 2007 | 12.95 | 13.47 | +12 h | 12.95 | +0 h | 12.89 | -1 h | 13.71 | +18 h | 15.39 | +59 h |
| 2008 | 13.4 | 13.35 | -1 h | 13.42 | +0 h | 13.47 | +2 h | 13.66 | +6 h | 15.32 | +46 h |
| 2009 | 13.73 | 13.99 | +6 h | 13.73 | +0 h | 13.92 | +5 h | 14.57 | +20 h | 16.4 | +64 h |
| 2010 | 15.0 | 15.05 | +1 h | 15.0 | +0 h | 15.44 | +11 h | 15.31 | +7 h | 15.76 | +18 h |
| 2011 | 13.07 | 13.95 | +21 h | 13.08 | +0 h | 13.13 | +1 h | 14.95 | +45 h | 15.44 | +57 h |
| 2012 | 12.24 | 12.37 | +3 h | 12.24 | +0 h | 12.24 | +0 h | 12.83 | +14 h | 14.62 | +57 h |
| 2013 | 12.94 | 13.67 | +18 h | 12.94 | +0 h | 13.23 | +7 h | 13.93 | +24 h | 15.27 | +56 h |
| 2014 | 14.23 | 14.61 | +9 h | 14.23 | +0 h | 14.26 | +1 h | 14.41 | +4 h | 16.4 | +52 h |
| 2015 | 13.89 | 13.98 | +2 h | 13.89 | +0 h | 13.54 | -8 h | 14.63 | +18 h | 14.99 | +26 h |
| 2016 | 14.55 | 14.56 | +0 h | 15.43 | +21 h | 14.8 | +6 h | 16.57 | +48 h | 15.71 | +28 h |
| 2017 | 14.29 | 14.23 | -1 h | 14.79 | +12 h | 14.4 | +3 h | 15.67 | +33 h | 15.2 | +22 h |
| 2018 | 14.72 | 14.64 | -2 h | 14.72 | +0 h | 15.43 | +17 h | 16.27 | +37 h | 15.74 | +24 h |
| 2019 | 14.14 | 14.41 | +6 h | 14.14 | +0 h | 14.13 | -0 h | 14.96 | +20 h | 15.96 | +44 h |
| 2020 | 14.82 | 14.61 | -5 h | 14.82 | +0 h | 15.25 | +10 h | 15.32 | +12 h | 16.0 | +28 h |
| 2021 | 13.38 | 13.58 | +5 h | 13.38 | +0 h | 13.38 | +0 h | 13.52 | +3 h | 15.56 | +52 h |
| 2022 | 13.85 | 15.16 | +31 h | 13.85 | +0 h | 14.28 | +10 h | 14.35 | +12 h | 16.62 | +66 h |
| 2023 | 14.38 | 14.53 | +4 h | 14.38 | +0 h | 14.63 | +6 h | 16.47 | +50 h | 15.66 | +31 h |
| 2024 | 13.52 | 14.07 | +13 h | 13.78 | +6 h | 13.99 | +11 h | 14.53 | +24 h | 15.62 | +50 h |
| 2025 | 13.12 | 13.48 | +9 h | 13.12 | +0 h | 13.19 | +2 h | 14.39 | +30 h | 15.31 | +53 h |

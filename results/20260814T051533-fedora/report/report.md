# Ledgerline analysis report - run `20260814T051533-fedora`

## Knee fits (isotonic p99-vs-rate crossing 20 ms): pooled AND per language

The runner brackets the crossing on p99 POOLED over go and .NET probes, and that pooled knee is what selected the confirm rates. Pooling happens AFTER under-warmed exclusion, which is language-asymmetric, so what a "pooled" row contains is a fact about the data and is stated per row: where both languages survive, the fit is an ensemble crossing and not either language's knee; where only one survives, the fit IS that language's knee. The per-language rows refit the same ladder rungs one language at a time.

| cell | scope | knee rps @ 20ms | bootstrap interval | inferential? | rungs used | note |
| --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | pooled (go+.NET) | 1490 | [1330, 1491] | NOT a CI (see note) | 7 | fitted on p99 POOLED over go and .NET probes (11 warm Go probe(s) over 6 rung(s), 7 warm .NET probe(s) over 6 rung(s) of 7 fitted rungs); the rung composition is uneven, so this is the ensemble's crossing, not either language's knee. |
| headline.mixed.gc-default | go | no crossing | n/a | n/a | 6 (-1) | no crossing within the measured ladder (max rung 1493 rps, p99 17.54 ms); not extrapolated. |
| headline.mixed.gc-default | dotnet | 1468 | [1340, 1468] | NOT a CI (see note) | 6 (-1) | CI invalid: crossing bracket has 1 rep(s) < min_reps=10 (ladder stage, reps_per_rung=3) |
| headline.mixed.gc-generous | pooled -> .NET probes only | 1178 | [1178, 1178] | NOT a CI (see note) | 6 | nominally pooled, but after under-warmed exclusion the fitted rungs carry .NET probes ONLY (0 warm Go probe(s) over 0 rung(s), 8 warm .NET probe(s) over 6 rung(s) of 6 fitted rungs): this fit IS .NET's knee, and it is what selected this cell's confirm rates. |
| headline.mixed.gc-generous | go *posture* | no crossing | n/a | n/a | 6 | POSTURE ONLY: every ladder probe for go was flagged under-warmed, so this fit runs over flagged probes and is not a gated measurement. no crossing within the measured ladder (max rung 1244 rps, p99 1.62 ms); not extrapolated. |
| headline.mixed.gc-generous | dotnet | 1178 | [1178, 1178] | NOT a CI (see note) | 6 | CI invalid: crossing bracket has 1 rep(s) < min_reps=10 (ladder stage, reps_per_rung=3) |
| variant.ado | pooled (go+.NET) | 10345 | [9811, 10360] | NOT a CI (see note) | 18 | fitted on p99 POOLED over go and .NET probes (22 warm Go probe(s) over 14 rung(s), 27 warm .NET probe(s) over 16 rung(s) of 18 fitted rungs); the rung composition is uneven, so this is the ensemble's crossing, not either language's knee. |
| variant.ado | go | no crossing | n/a | n/a | 14 (-4) | no crossing within the measured ladder (max rung 9244 rps, p99 2.64 ms); not extrapolated. |
| variant.ado | dotnet | 10334 | [9793, 10346] | NOT a CI (see note) | 16 (-2) | CI invalid: crossing bracket has 2 rep(s) < min_reps=10 (ladder stage, reps_per_rung=3) |
| variant.dapper | pooled (go+.NET) | 10679 | [9804, 10912] | NOT a CI (see note) | 18 | fitted on p99 POOLED over go and .NET probes (21 warm Go probe(s) over 15 rung(s), 29 warm .NET probe(s) over 16 rung(s) of 18 fitted rungs); the rung composition is uneven, so this is the ensemble's crossing, not either language's knee. |
| variant.dapper | go | no crossing | n/a | n/a | 15 (-3) | no crossing within the measured ladder (max rung 11093 rps, p99 5.49 ms); not extrapolated. |
| variant.dapper | dotnet | 10177 | [9739, 10177] | NOT a CI (see note) | 16 (-2) | CI invalid: crossing bracket has 1 rep(s) < min_reps=10 (ladder stage, reps_per_rung=3) |
| variant.prepared-parity | pooled (go+.NET) | 11246 | [10137, 12056] | NOT a CI (see note) | 19 | fitted on p99 POOLED over go and .NET probes (26 warm Go probe(s) over 16 rung(s), 30 warm .NET probe(s) over 19 rung(s) of 19 fitted rungs); the rung composition is uneven, so this is the ensemble's crossing, not either language's knee. |
| variant.prepared-parity | go | no crossing | n/a | n/a | 16 (-3) | no crossing within the measured ladder (max rung 11093 rps, p99 8.50 ms); not extrapolated. |
| variant.prepared-parity | dotnet | 10426 | [9536, 11278] | NOT a CI (see note) | 19 | CI invalid: crossing bracket has 2 rep(s) < min_reps=10 (ladder stage, reps_per_rung=3) |


- **headline.mixed.gc-generous: the "pooled" knee is a .NET-only fit.** After under-warmed exclusion its 6 fitted rungs carry 0 warm Go probe(s), 8 warm .NET probe(s). This cell's confirm rates, and therefore its operating rate and everything aggregated at it below, were selected by a fit that saw one language only.


The bracketed interval is the 2.5/97.5 percentile of 2000 isotonic refits that resample the ladder's per-rung reps with replacement. It is NOT a valid bootstrap CI here: the SLO-crossing bracket carries as few as 1 measurement-grade rep(s), against the pre-registered minimum of `ladder.confirm_reps` = 10 (the ladder runs `reps_per_rung: 3`, and under-warmed exclusion thins the bracket further). Read it as a spread indicator over a handful of reps. An inferential CI needs the crossing bracket re-run at confirm-stage rep counts, which this run did not do.


## The latency promise at the offered rate (p99 vs 20 ms SLO)

Both stacks were held to the same OFFERED RATE, never to the same latency promise. A rate that is sub-knee for the pooled fit can be supra-knee for one of the two languages, so each language's own measured p99 is printed against the 20 ms SLO here.

THE VERDICT RULE: the `meets 20 ms?` column compares the MEAN of the aggregated windows' p99 values against 20 ms. That is the same rung-mean rule the ladder bracketed on, and it is lenient for a tail promise: a language can be marked `yes` while individual windows miss badly. So the worst window and the count of windows whose OWN p99 was under 20 ms are printed in the same row: read those three columns together, never the verdict alone.

| cell | stage | offered rps | language | mean p99 (ms) | worst window p99 (ms) | meets 20 ms? (mean rule) | windows under 20 ms | windows (used/total) | why dropped |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | confirm | 1244 | .NET | 22.85 | 58.30 | NO | 2/4 | 4/10 | 6 under-warmed |
| headline.mixed.gc-default | confirm | 1244 | Go | 7.18 | 23.82 | yes | 4/5 | 5/10 | 5 under-warmed |
| headline.mixed.gc-default | confirm | 1493 | .NET | 34.33 | 69.60 | NO | 2/6 | 6/10 | 4 under-warmed |
| headline.mixed.gc-default | confirm | 1493 | Go | 4.47 | 13.24 | yes | 4/4 | 4/10 | 6 under-warmed |
| headline.mixed.gc-default | soak | 1244 | .NET | 33.35 | 33.35 | NO | 0/1 | 0/1 *posture* | 1 under-warmed |
| headline.mixed.gc-default | soak | 1244 | Go | 6.61 | 6.61 | yes | 1/1 | 0/1 *posture* | 1 under-warmed |
| headline.mixed.gc-generous | confirm | 1037 | .NET | 2.08 | 2.09 | yes | 10/10 | 0/10 *posture* | 10 under-warmed |
| headline.mixed.gc-generous | confirm | 1037 | Go | 1.55 | 3.57 | yes | 10/10 | 0/10 *posture* | 10 under-warmed |
| headline.mixed.gc-generous | confirm | 1244 | .NET | 15.87 | 33.66 | yes | 3/5 | 5/10 | 5 under-warmed |
| headline.mixed.gc-generous | confirm | 1244 | Go | 3.86 | 22.30 | yes | 9/10 | 0/10 *posture* | 10 under-warmed |
| headline.mixed.gc-generous | soak | 1037 | .NET | 2.08 | 2.08 | yes | 1/1 | 0/1 *posture* | 1 under-warmed |
| headline.mixed.gc-generous | soak | 1037 | Go | 19.59 | 19.59 | yes | 1/1 | 0/1 *posture* | 1 under-warmed |
| variant.ado | confirm | 9244 | .NET | 4.10 | 4.55 | yes | 8/8 | 8/10 | 2 under-warmed |
| variant.ado | confirm | 9244 | Go | 2.44 | 2.44 | yes | 1/1 | 1/10 | 7 under-warmed, 2 rate fidelity |
| variant.ado | confirm | 11093 | .NET | 23.71 | 42.20 | NO | 2/3 | 3/10 | 7 under-warmed |
| variant.ado | confirm | 11093 | Go | 3.77 | 4.95 | yes | 2/2 | 2/10 | 6 under-warmed, 2 rate fidelity |
| variant.ado | soak | 9244 | .NET | 15.40 | 15.40 | yes | 1/1 | 1/1 | - |
| variant.ado | soak | 9244 | Go | 8.56 | 8.56 | yes | 1/1 | 0/1 *posture* | 1 under-warmed |
| variant.dapper | confirm | 9244 | .NET | 22.27 | 46.47 | NO | 2/4 | 4/10 | 6 under-warmed |
| variant.dapper | confirm | 9244 | Go | 2.51 | 2.51 | yes | 1/1 | 1/10 | 9 under-warmed |
| variant.dapper | confirm | 11093 | .NET | 40.54 | 58.80 | NO | 2/10 | 0/10 *posture* | 9 under-warmed, 1 rate fidelity |
| variant.dapper | confirm | 11093 | Go | 4.00 | 4.00 | yes | 1/1 | 1/10 | 8 under-warmed, 1 rate fidelity |
| variant.dapper | soak | 9244 | .NET | 53.80 | 53.80 | NO | 0/1 | 0/1 *posture* | 1 under-warmed |
| variant.dapper | soak | 9244 | Go | 9.58 | 9.58 | yes | 1/1 | 1/1 | - |
| variant.prepared-parity | confirm | 11093 | .NET | 23.73 | 39.30 | NO | 3/8 | 8/10 | 2 under-warmed |
| variant.prepared-parity | confirm | 11093 | Go | 15.19 | 15.19 | yes | 1/1 | 1/10 | 9 under-warmed |
| variant.prepared-parity | confirm | 13312 | .NET | 103.11 | 148.23 | NO | 0/10 | 0/10 *posture* | 6 error rate, 4 rate fidelity |
| variant.prepared-parity | confirm | 13312 | Go | 16.30 | 20.41 | yes | 3/4 | 4/10 | 5 under-warmed, 1 rate fidelity |
| variant.prepared-parity | soak | 11093 | .NET | 24.00 | 24.00 | NO | 0/1 | 1/1 | - |
| variant.prepared-parity | soak | 11093 | Go | 12.11 | 12.11 | yes | 1/1 | 0/1 *posture* | 1 under-warmed |


- **headline.mixed.gc-default** at 1244 rps offered: .NET mean p99 22.9 ms (MISSES the 20 ms promise on the mean rule; 2/4 windows individually under it, worst 58.3 ms); Go mean p99 7.2 ms (meets the 20 ms promise on the mean rule; 4/5 windows individually under it, worst 23.8 ms).
- **headline.mixed.gc-generous** at 1037 rps offered: .NET mean p99 2.1 ms (meets the 20 ms promise on the mean rule; 10/10 windows individually under it, worst 2.1 ms, posture-only windows); Go mean p99 1.5 ms (meets the 20 ms promise on the mean rule; 10/10 windows individually under it, worst 3.6 ms, posture-only windows).
- **variant.ado** at 9244 rps offered: .NET mean p99 4.1 ms (meets the 20 ms promise on the mean rule; 8/8 windows individually under it, worst 4.5 ms); Go mean p99 2.4 ms (meets the 20 ms promise on the mean rule; 1/1 windows individually under it, worst 2.4 ms).
- **variant.dapper** at 9244 rps offered: .NET mean p99 22.3 ms (MISSES the 20 ms promise on the mean rule; 2/4 windows individually under it, worst 46.5 ms); Go mean p99 2.5 ms (meets the 20 ms promise on the mean rule; 1/1 windows individually under it, worst 2.5 ms).
- **variant.prepared-parity** at 11093 rps offered: .NET mean p99 23.7 ms (MISSES the 20 ms promise on the mean rule; 3/8 windows individually under it, worst 39.3 ms); Go mean p99 15.2 ms (meets the 20 ms promise on the mean rule; 1/1 windows individually under it, worst 15.2 ms).


## CPU-ms per request vs offered rate (is the cost rate-independent?)

A per-request cost quoted at one operating point is only a stack property if it does not move with rate. It does move. Each row is a ladder rung; a value is that rung's TOTAL cgroup CPU delta divided by the TOTAL requests its windows serviced (the same request-weighted rule the roll-up below uses). `*` = no measurement-grade window at that rung for that language, so the flagged windows were used (posture only).


**headline.mixed.gc-default**

| offered rps | Go CPU-ms/req | Go windows (used/total) | .NET CPU-ms/req | .NET windows (used/total) | .NET : Go |
| --- | --- | --- | --- | --- | --- |
| 500 | 0.1324* | 0/3 | 0.6290 | 1/3 | 4.75x |
| 600 | 0.1309 | 1/3 | 0.6350 | 1/3 | 4.85x |
| 720 | 0.1300 | 3/3 | 0.6238* | 0/3 | 4.80x |
| 864 | 0.1292 | 2/3 | 0.6208 | 1/3 | 4.81x |
| 1037 | 0.1291 | 2/3 | 0.6166 | 1/3 | 4.78x |
| 1244 | 0.1294 | 2/3 | 0.6062 | 1/3 | 4.68x |
| 1493 | 0.1288 | 1/3 | 0.5879 | 2/3 | 4.56x |


Coefficient of variation across the 7 rungs: Go 1.0%, .NET 2.6%. .NET:Go ratio spans 4.56x to 4.85x across the ladder; 2 rung-values are posture-only.


**headline.mixed.gc-generous**

| offered rps | Go CPU-ms/req | Go windows (used/total) | .NET CPU-ms/req | .NET windows (used/total) | .NET : Go |
| --- | --- | --- | --- | --- | --- |
| 500 | 0.1431* | 0/3 | 0.6631 | 1/3 | 4.63x |
| 600 | 0.1369* | 0/3 | 0.6358 | 2/3 | 4.64x |
| 720 | 0.1320* | 0/3 | 0.6119 | 2/3 | 4.64x |
| 864 | 0.1274* | 0/3 | 0.6131 | 1/3 | 4.81x |
| 1037 | 0.1271* | 0/3 | 0.5945 | 1/3 | 4.68x |
| 1244 | 0.1263* | 0/3 | 0.5901 | 1/3 | 4.67x |


Coefficient of variation across the 6 rungs: Go 5.1%, .NET 4.4%. .NET:Go ratio spans 4.63x to 4.81x across the ladder; 6 rung-values are posture-only.


**variant.ado**

| offered rps | Go CPU-ms/req | Go windows (used/total) | .NET CPU-ms/req | .NET windows (used/total) | .NET : Go |
| --- | --- | --- | --- | --- | --- |
| 500 | 0.1413 | 2/3 | 0.5602 | 2/3 | 3.96x |
| 600 | 0.1361 | 2/3 | 0.5427* | 0/3 | 3.99x |
| 720 | 0.1297 | 1/3 | 0.5224 | 1/3 | 4.03x |
| 864 | 0.1263 | 2/3 | 0.5127* | 0/3 | 4.06x |
| 1037 | 0.1257* | 0/3 | 0.5019 | 2/3 | 3.99x |
| 1244 | 0.1250* | 0/3 | 0.4875 | 1/3 | 3.90x |
| 1493 | 0.1238 | 3/3 | 0.4549 | 3/3 | 3.68x |
| 1792 | 0.1227* | 0/3 | 0.4517 | 2/3 | 3.68x |
| 2150 | 0.1215 | 2/3 | 0.4287 | 1/3 | 3.53x |
| 2580 | 0.1194 | 1/3 | 0.4182 | 2/3 | 3.50x |
| 3096 | 0.1186 | 1/3 | 0.4041 | 1/3 | 3.41x |
| 3715 | 0.1175 | 1/3 | 0.4007 | 3/3 | 3.41x |
| 4458 | 0.1169 | 1/3 | 0.3847 | 1/3 | 3.29x |
| 5350 | 0.1152 | 1/3 | 0.3740 | 1/3 | 3.25x |
| 6420 | 0.1143 | 2/3 | 0.3551 | 1/3 | 3.11x |
| 7704 | 0.1131 | 2/3 | 0.3361 | 2/3 | 2.97x |
| 9244 | 0.1118 | 1/3 | 0.3154 | 2/3 | 2.82x |
| 11093 | 0.1036* | 0/3 | 0.2868 | 2/3 | 2.77x |


Coefficient of variation across the 18 rungs: Go 7.4%, .NET 18.7%. .NET:Go ratio spans 2.77x to 4.06x across the ladder; 6 rung-values are posture-only.


**variant.dapper**

| offered rps | Go CPU-ms/req | Go windows (used/total) | .NET CPU-ms/req | .NET windows (used/total) | .NET : Go |
| --- | --- | --- | --- | --- | --- |
| 500 | 0.1408 | 1/3 | 0.5661 | 1/3 | 4.02x |
| 600 | 0.1349 | 1/3 | 0.5472* | 0/3 | 4.06x |
| 720 | 0.1300 | 2/3 | 0.5272 | 2/3 | 4.06x |
| 864 | 0.1266 | 2/3 | 0.5185 | 2/3 | 4.09x |
| 1037 | 0.1259* | 0/3 | 0.5085 | 2/3 | 4.04x |
| 1244 | 0.1253 | 2/3 | 0.4919 | 2/3 | 3.92x |
| 1493 | 0.1241 | 1/3 | 0.4617 | 3/3 | 3.72x |
| 1792 | 0.1223 | 1/3 | 0.4561* | 0/3 | 3.73x |
| 2150 | 0.1215 | 1/3 | 0.4357 | 2/3 | 3.59x |
| 2580 | 0.1200* | 0/3 | 0.4311 | 1/3 | 3.59x |
| 3096 | 0.1184 | 1/3 | 0.4174 | 2/3 | 3.53x |
| 3715 | 0.1183 | 1/3 | 0.4071 | 1/3 | 3.44x |
| 4458 | 0.1156 | 2/3 | 0.3944 | 2/3 | 3.41x |
| 5350 | 0.1157 | 2/3 | 0.3748 | 2/3 | 3.24x |
| 6420 | 0.1147* | 0/3 | 0.3579 | 3/3 | 3.12x |
| 7704 | 0.1134 | 2/3 | 0.3398 | 1/3 | 3.00x |
| 9244 | 0.1119 | 1/3 | 0.3220 | 1/3 | 2.88x |
| 11093 | 0.1100 | 1/3 | 0.2900 | 2/3 | 2.64x |


Coefficient of variation across the 18 rungs: Go 6.7%, .NET 18.5%. .NET:Go ratio spans 2.64x to 4.09x across the ladder; 5 rung-values are posture-only.


**variant.prepared-parity**

| offered rps | Go CPU-ms/req | Go windows (used/total) | .NET CPU-ms/req | .NET windows (used/total) | .NET : Go |
| --- | --- | --- | --- | --- | --- |
| 500 | 0.1405* | 0/3 | 0.6480 | 1/3 | 4.61x |
| 600 | 0.1340 | 1/3 | 0.6324 | 2/3 | 4.72x |
| 720 | 0.1293 | 3/3 | 0.6084 | 2/3 | 4.71x |
| 864 | 0.1259 | 3/3 | 0.5940 | 3/3 | 4.72x |
| 1037 | 0.1254 | 2/3 | 0.5890 | 1/3 | 4.70x |
| 1244 | 0.1253 | 1/3 | 0.5747 | 1/3 | 4.59x |
| 1493 | 0.1240 | 3/3 | 0.5480 | 1/3 | 4.42x |
| 1792 | 0.1225 | 1/3 | 0.5523 | 1/3 | 4.51x |
| 2150 | 0.1210 | 2/3 | 0.5346 | 1/3 | 4.42x |
| 2580 | 0.1184 | 1/3 | 0.5313 | 1/3 | 4.49x |
| 3096 | 0.1174 | 1/3 | 0.5189 | 2/3 | 4.42x |
| 3715 | 0.1166 | 1/3 | 0.5007 | 2/3 | 4.29x |
| 4458 | 0.1153* | 0/3 | 0.4759 | 1/3 | 4.13x |
| 5350 | 0.1148 | 2/3 | 0.4529 | 2/3 | 3.95x |
| 6420 | 0.1137 | 2/3 | 0.4316 | 1/3 | 3.80x |
| 7704 | 0.1120 | 1/3 | 0.4067 | 2/3 | 3.63x |
| 9244 | 0.1113 | 1/3 | 0.3772 | 2/3 | 3.39x |
| 11093 | 0.1092 | 1/3 | 0.3435 | 2/3 | 3.14x |
| 13312 | 0.1043* | 0/3 | 0.3076 | 2/3 | 2.95x |


Coefficient of variation across the 19 rungs: Go 7.4%, .NET 19.3%. .NET:Go ratio spans 2.95x to 4.72x across the ladder; 3 rung-values are posture-only.


## Stationarity (per-second p99, Mann-Kendall + Theil-Sen band)

| cell | stationarity |
| --- | --- |
| headline.mixed.gc-default | all 44 series stationary |
| headline.mixed.gc-generous | all 38 series stationary |
| variant.ado | all 110 series stationary |
| variant.dapper | 2/110 series non-stationary |
| variant.prepared-parity | 1/116 series non-stationary |


## Paired go-vs-.NET p99 verdict (confirm stage, RCB-paired by block)

One verdict PER OFFERED RATE: confirm runs at both bracket rates, and a verdict pooled over both would average two different operating points. The interval is the paired-t interval on the per-block differences at its stated confidence level; the equivalence margin is the separate, pre-registered +-band the interval is compared against. The verdict word is that equivalence decision (interval not contained in +-margin -> direction of the mean difference); an interval that ALSO spans 0% is not a significant difference at its confidence level, so read the interval, not only the word.

| cell | offered rps | metric | blocks (n) | Go | .NET | diff % | 95% CI (paired t) | equiv. margin | verdict | effect (d_z) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | 1244 | p99_ms | 2 | 12.6 | 30.37 | -58.49% | [-758.01, +641.02] | +-5% | Go better | -0.75 |
| headline.mixed.gc-default | 1493 | p99_ms | 3 | 5.463 | 30.56 | -82.12% | [-366.76, +202.52] | +-5% | Go better | -0.72 |


No paired verdict for: headline.mixed.gc-generous, variant.ado, variant.dapper, variant.prepared-parity; a block contributes a pair only when BOTH languages are measurement-grade in it, and in the cell(s) listed one language has no warm confirm window at all (see the posture note below).


## Resource cost at the operating rate (SUT cgroup v2 ground truth)

Steady-state SUT cgroup CPU + memory per serviced request at the offered rate(s) [1037, 1244, 9244, 11093] rps (selected as rate <= the POOLED knee), aggregated over the confirm + soak probe windows. Under-warmed / gate-excluded windows are EXCLUDED, the same exclusion the knee fit applies; the counts used vs excluded are a row of the table. `.NET : Go` > 1 means .NET spends more. Per-request CPU is NOT rate-independent (see the CPU-vs-rate section), so these are values AT these rates, not stack constants.

THE AGGREGATION RULE: `CPU-ms / request` is REQUEST-WEIGHTED: the total cgroup CPU delta of the windows in the roll-up divided by the total requests they serviced, so the request count in the table really is its denominator. The memory rows are plain means over the same windows (a memory level is not a per-request quantity, so there is nothing to weight it by).


**Stage-mix asymmetry in headline.mixed.gc-default.** The windows behind the two CPU figures do not have the same stage composition: Go 5 confirm vs .NET 4 confirm (the missing windows were flagged under-warmed, not omitted by choice). A soak window is the run's longest probe and services several times a confirm window's requests, so the two figures aggregate different stage mixes. Measured effect on the published ratio: the equal-weight-per-window alternative gives 4.649x against the published request-weighted 4.649x, so the weighting choice does not carry this cell's headline, but it is the reader's to check, not ours to leave implicit.


**Stage-mix asymmetry in variant.ado.** The windows behind the two CPU figures do not have the same stage composition: Go 1 confirm vs .NET 8 confirm, 1 soak (the missing windows were flagged under-warmed, not omitted by choice). A soak window is the run's longest probe and services several times a confirm window's requests, so the two figures aggregate different stage mixes. Measured effect on the published ratio: the equal-weight-per-window alternative gives 2.833x against the published request-weighted 2.808x, so the weighting choice does not carry this cell's headline, but it is the reader's to check, not ours to leave implicit.


**Stage-mix asymmetry in variant.dapper.** The windows behind the two CPU figures do not have the same stage composition: Go 1 confirm, 1 soak vs .NET 4 confirm (the missing windows were flagged under-warmed, not omitted by choice). A soak window is the run's longest probe and services several times a confirm window's requests, so the two figures aggregate different stage mixes. Measured effect on the published ratio: the equal-weight-per-window alternative gives 2.862x against the published request-weighted 2.876x, so the weighting choice does not carry this cell's headline, but it is the reader's to check, not ours to leave implicit.


**Stage-mix asymmetry in variant.prepared-parity.** The windows behind the two CPU figures do not have the same stage composition: Go 1 confirm vs .NET 8 confirm, 1 soak (the missing windows were flagged under-warmed, not omitted by choice). A soak window is the run's longest probe and services several times a confirm window's requests, so the two figures aggregate different stage mixes. Measured effect on the published ratio: the equal-weight-per-window alternative gives 3.108x against the published request-weighted 3.091x, so the weighting choice does not carry this cell's headline, but it is the reader's to check, not ours to leave implicit.

| cell | metric | Go | .NET | .NET : Go |
| --- | --- | --- | --- | --- |
| headline.mixed.gc-default | CPU-ms / request | 0.128 | 0.595 | 4.65x |
| headline.mixed.gc-default | anon memory p99 (MB, in-window) | 21 | 107 | 5.10x |
| headline.mixed.gc-default | cgroup memory.peak (MB, container lifetime) | 250 | 367 | 1.47x |
| headline.mixed.gc-default | windows used / flagged-excluded | 5 / 6 | 4 / 7 | - |
| headline.mixed.gc-default | stage mix of the windows used | 5 confirm | 4 confirm | - |
| headline.mixed.gc-default | requests serviced (the CPU denominator) | 746400 | 597117 | - |
| headline.mixed.gc-generous | CPU-ms / request | 0.127* | 0.614* | 4.85x |
| headline.mixed.gc-generous | anon memory p99 (MB, in-window) | 6619* | 212* | 0.03x |
| headline.mixed.gc-generous | cgroup memory.peak (MB, container lifetime) | 6820* | 503* | 0.07x |
| headline.mixed.gc-generous | windows used / flagged-excluded | 11 / 0 (ALL 11 flagged; used as posture) | 11 / 0 (ALL 11 flagged; used as posture) | - |
| headline.mixed.gc-generous | stage mix of the windows used | 10 confirm, 1 soak | 10 confirm, 1 soak | - |
| headline.mixed.gc-generous | requests serviced (the CPU denominator) | 1866599 | 1866600 | - |
| variant.ado | CPU-ms / request | 0.112 | 0.313 | 2.81x |
| variant.ado | anon memory p99 (MB, in-window) | 24 | 75 | 3.10x |
| variant.ado | cgroup memory.peak (MB, container lifetime) | 1268 | 1346 | 1.06x |
| variant.ado | windows used / flagged-excluded | 1 / 10 | 9 / 2 | - |
| variant.ado | stage mix of the windows used | 1 confirm | 8 confirm, 1 soak | - |
| variant.ado | requests serviced (the CPU denominator) | 1109283 | 14420666 | - |
| variant.dapper | CPU-ms / request | 0.111 | 0.318 | 2.88x |
| variant.dapper | anon memory p99 (MB, in-window) | 26 | 91 | 3.42x |
| variant.dapper | cgroup memory.peak (MB, container lifetime) | 1281 | 1340 | 1.05x |
| variant.dapper | windows used / flagged-excluded | 2 / 9 | 4 / 7 | - |
| variant.dapper | stage mix of the windows used | 1 confirm, 1 soak | 4 confirm | - |
| variant.dapper | requests serviced (the CPU denominator) | 6655691 | 4437125 | - |
| variant.prepared-parity | CPU-ms / request | 0.110 | 0.339 | 3.09x |
| variant.prepared-parity | anon memory p99 (MB, in-window) | 32 | 171 | 5.33x |
| variant.prepared-parity | cgroup memory.peak (MB, container lifetime) | 1276 | 1464 | 1.15x |
| variant.prepared-parity | windows used / flagged-excluded | 1 / 10 | 9 / 2 | - |
| variant.prepared-parity | stage mix of the windows used | 1 confirm | 8 confirm, 1 soak | - |
| variant.prepared-parity | requests serviced (the CPU denominator) | 1331174 | 17305255 | - |


Per-language cross-check for **headline.mixed.gc-default**: the windows above were selected by `rate <= the POOLED knee`, which is not by construction any language's own latency promise. Against each language's OWN knee: Go never crossed the SLO inside the ladder (top rung 1493 rps at p99 17.54 ms), so no per-language knee bounds its rows; .NET's own knee is 1468 rps, at or above every aggregated rate.


Per-language cross-check for **headline.mixed.gc-generous**: the windows above were selected by `rate <= the POOLED knee`, which is not by construction any language's own latency promise. Against each language's OWN knee: Go never crossed the SLO inside the ladder (top rung 1244 rps at p99 1.62 ms), so no per-language knee bounds its rows; .NET's own knee is 1178 rps, at or above every aggregated rate.


Per-language cross-check for **variant.ado**: the windows above were selected by `rate <= the POOLED knee`, which is not by construction any language's own latency promise. Against each language's OWN knee: Go never crossed the SLO inside the ladder (top rung 9244 rps at p99 2.64 ms), so no per-language knee bounds its rows; .NET's own knee is 10334 rps, at or above every aggregated rate.


Per-language cross-check for **variant.dapper**: the windows above were selected by `rate <= the POOLED knee`, which is not by construction any language's own latency promise. Against each language's OWN knee: Go never crossed the SLO inside the ladder (top rung 11093 rps at p99 5.49 ms), so no per-language knee bounds its rows; .NET's own knee is 10177 rps, at or above every aggregated rate.


Per-language cross-check for **variant.prepared-parity**: the windows above were selected by `rate <= the POOLED knee`, which is not by construction any language's own latency promise. Against each language's OWN knee: Go never crossed the SLO inside the ladder (top rung 11093 rps at p99 8.50 ms), so no per-language knee bounds its rows; .NET's own knee is 10426 rps, below the aggregated rate(s) [11093] rps, so every .NET window in this roll-up is supra-knee for .NET.


- **headline.mixed.gc-generous / Go: all 11 probe windows for this language were flagged under-warmed** (failed gates: gc_steady 11x, p999_flat 1x, p99_flat 2x). `gc_steady` failed in 11 of 11 windows and this cell sets `GOGC=off` for Go, so there is no GC cadence to settle and the gate cannot be satisfied by construction. The remaining flagged gate(s), `p999_flat` 1x, `p99_flat` 2x, are NOT explained by that knob. These numbers are therefore reported as POSTURE EVIDENCE, not as a gated measurement, and the row is marked `*`.
- **headline.mixed.gc-generous / .NET: all 11 probe windows for this language were flagged under-warmed** (failed gates: gc_steady 11x, p999_flat 1x, p99_flat 1x). No knob declared for this cell explains these failures, so no cause is claimed here; the gate counts above are the whole of the evidence. These numbers are therefore reported as POSTURE EVIDENCE, not as a gated measurement, and the row is marked `*`.


`anon memory p99` is the p99 of `memory.stat anon` sampled inside the measurement windows. `cgroup memory.peak` is the kernel's high-water mark of `memory.current` over the CONTAINER'S WHOLE LIFETIME: anonymous memory plus page cache plus kernel memory, including warmup, so it is not an in-window RSS figure and is not comparable to the anon p99 above it.


Pool warmth at the end of warmup (open connections / configured max): headline.mixed.gc-default: Go 24/24, .NET 24/24; headline.mixed.gc-generous: Go 24/24, .NET 24/24; variant.ado: Go 24/24, .NET 24/24; variant.dapper: Go 24/24, .NET 24/24; variant.prepared-parity: Go 24/24, .NET 24/24.


Resource-accounting validity, over the 57 aggregated confirm + soak windows in the table above (NOT over the ladder windows, which are not aggregated here): CPU throttle counters zero; cgroup OOM events zero. A throttled window would deflate CPU-ms/request; an OOM window would invalidate the memory figures.


The two quantities the CPU rows divide: total cgroup CPU over the aggregated windows, and the requests those windows serviced: headline.mixed.gc-default: Go 746400 requests (0 non-2xx) over 95527 CPU-ms, .NET 597117 (0 non-2xx) over 355304 CPU-ms; headline.mixed.gc-generous: Go 1866599 requests (0 non-2xx) over 236223 CPU-ms, .NET 1866600 (0 non-2xx) over 1146730 CPU-ms; variant.ado: Go 1109283 requests (0 non-2xx) over 123830 CPU-ms, .NET 14420666 (0 non-2xx) over 4519605 CPU-ms; variant.dapper: Go 6655691 requests (0 non-2xx) over 735487 CPU-ms, .NET 4437125 (0 non-2xx) over 1410116 CPU-ms; variant.prepared-parity: Go 1331174 requests (0 non-2xx) over 145993 CPU-ms, .NET 17305255 (1 non-2xx) over 5867327 CPU-ms. Dividing the second into the first reproduces the `CPU-ms / request` row exactly; that is the aggregation rule stated above.


## Not captured in this run (scope)

- **Warmup gates declared in `spec/slo.yaml` but NOT implemented:** `hot_set_touched`, `major_faults_asserted_equal`. The runner evaluates `gc_steady`, `p999_flat`, `p99_flat`, `pools_warm` (`ledgerbench.warmup.IMPLEMENTED_GATES`) plus the separate `per_endpoint_min_calls` spec key, so a probe recorded as warm was never checked against the 2 gate(s) named above. Their absence is invisible in the per-probe warmup records; it is stated here instead. Cross-check against this run's own records: the gates its flagged windows report failing are `gc_steady`, `p999_flat`, `p99_flat`, none of which is in the unimplemented list.

- **Effective working set and cache residency.** The load replays a FIXED pre-generated target list of 1000000 requests (manifest `working_set.target_count`, measured from the generated file), drawn Zipf-hot from 5000000 seeded invoices / 1000000 seeded customers. The distinct entity ids it touches are a fraction of the seeded corpus and the list replays against the cache TTL at the operating rates above, so the read path is more cache-resident than the constructed target hit rate of 0.80 implies. The REALIZED Redis hit rate this run was 0.807 (manifest `cache_stats`, INFO keyspace deltas over measured windows).

- **DB-CPU-per-request attribution.** Only the two SUT containers are cgroup-sampled; the PostgreSQL CPU delta that would split app cost from query-plan cost is not captured (descoped at scope freeze), so it is omitted rather than estimated. The CO-safety of the load rests on the per-probe achieved==offered evidence (manifest `achieved_vs_offered`).

- **/runtime-stats GC counters.** The 1 Hz runtime-stats poller drives the warmup gates live; its per-window snapshots are not flushed to disk (descoped), so GC collection counts / pause totals are unavailable; only the `gc_steady` warmup gate (pass/fail) and the warmed pool size survive.

- **Hysteresis, p999 order-statistic CIs, isolation runs, and PG/vegeta/dockerd cgroup deltas.** Not captured; removed from scope at the freeze (see docs/METHODOLOGY.md).

- **Illustrative $/M-request cost.** Not published as a dollar figure: it would only scale the CPU ratio above by a chosen cloud vCPU price, and that ratio is itself rate-dependent (see the CPU-vs-rate section).


# docs/figures/

Every PNG numbered `01` and up is drawn by [`bench/figures.py`](../../bench/figures.py)
from the committed artifacts of run `20260814T051533-fedora`. The script reads
`report/fits.json` and `ledger.json`, and refits the knee curves with the run's own
isotonic code (`analysis.pava.isotonic_fit`). No number in any of those figures is typed
by hand, so a figure cannot drift from the run it describes.

`00-cover.png` is the exception: it is a hand-made title card with no data in it, so
`make figures` neither draws nor overwrites it.

Regenerate them all:

```sh
make figures
# or: cd bench && uv run --with matplotlib python figures.py
```

`matplotlib` is deliberately not a repo dependency: nothing in the measured path draws,
so it is pulled in ad hoc with `--with`.

| file | what it shows |
|---|---|
| `00-cover.png` | title card (hand-made, carries no measurement) |
| `01-the-rig.png` | the topology: cores, containers, cgroups, load generator |
| `02-cpu-per-request.png` | CPU-ms per request against offered rate, both languages |
| `03-memory.png` | steady-state anonymous memory per language |
| `04-latency-wall.png` | p99 against offered rate, and where each stack crosses 20 ms |
| `05-data-layers.png` | the .NET knee under EF Core, Dapper, raw ADO.NET and EF + prepared |
| `06-how-to-choose.png` | the decision guide, not a measurement |

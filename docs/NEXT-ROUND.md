# Next round: what I found after publishing, and what I will change

Written 2026-10-20. Next year, with newer Go and .NET releases, I will most probably run this
benchmark again. Before that I will fold in the changes below. This file records what I found
after publication, so the next run starts from it and so anyone reading the published numbers
knows which of them hold.

## 1. The host: the kernel's preferred-core packing (ITMT)

**What it is.** On this machine (Ryzen 9 7950X) amd-pstate ranks the cores unevenly, from the
firmware's per-core performance values, and from that ranking the kernel turns on ITMT, x86's
asym packing. The scheduler then keeps pulling running work onto the higher-ranked CPUs. Inside
this run's SUT cpuset (0-5) the ranking reads 236, 226, 236, 221, 231, 216 (read on this machine
on 2026-10-20). Turbo boost off, which `infra/host-setup.sh` applies, makes it worse: no core can
boost, so the ranking prefers cores for headroom they do not have.

**How I found it.** In a later benchmark on this same machine, a service with a two-worker async
runtime on 2 CPUs had latency-collapse episodes: both workers sat runnable on the higher-ranked
CPU while the other one idled, for up to about a second. Turning ITMT off
(`/sys/kernel/debug/x86/sched_itmt_enabled` set to 0), with nothing else changed, removed the
episodes. A Go service on the same host did not stall with ITMT on or off.

**Was it on in this run?** Almost certainly, but this run cannot prove it. It ran on Fedora 43,
where amd-pstate's active mode and preferred-core support are the defaults for this CPU;
`host-setup.sh` never touches ITMT; and the run recorded no host state at all
(`manifest.host_state.source: not_measured`).

**What the data shows.** This run's SUT had 6 CPUs, not 2, and at the headline rates it used
less than one of them, so it is not the same setup. The windows are still episodic, in both
stacks:

- `headline.mixed.gc-default`, confirm at 1,244 and 1,493 rps: each .NET window is either about
  2.5 ms p99 or 25 to 70 ms, with almost nothing in between, while .NET used 0.75 to 0.9 cores of
  its 6. Go shows the same shape, milder: most windows near 1.5 ms, a few at 4 to 32 ms.
- In the variant cells some Go windows collapse to 72 to 300 ms p99, and in two of them vegeta
  got only 40% to 48% of the offered rate.

Go stalls here too, and Go did not stall under ITMT in the later benchmark, so ITMT is not the
whole story. The committed artifacts cannot separate the causes: no host state, no scheduler
counters, no GC counters on disk, and a broken per-second series (section 3).

**What will settle it.** An ITMT on/off comparison of `headline.mixed.gc-default` and
`variant.prepared-parity` at the rates around their crossings, alternating the order. This
machine now runs Ubuntu, so that comparison measures the on/off difference, not the published
numbers again. I will add the result here.

## 2. Recount: which published numbers depend on dropped windows

The published numbers use only the windows that passed the warm-up gates. 368 of the 618
windows were flagged under-warmed and 22 more were excluded (rate fidelity, error rate). Go
lost more of its windows than .NET: of 100 confirm windows per stack, 19 Go and 38 .NET ones
were used. Of Go's 14 confirm windows over 20 ms, 12 were dropped, 6 of them because vegeta
could not hold the offered rate.

I re-ran this repo's own `ledgerbench analyze` on two edited copies of the committed ledger.
On the unedited ledger it reproduces `report/fits.json` and `report/report.md` byte for byte, so
every difference below comes from the window set alone.

- **A** clears every under-warmed flag. Section 3 explains why those flags are unreliable.
- **B** also puts back the 22 excluded windows.

| README claim | Published | A | B | Holds? |
|---|---|---|---|---|
| .NET held 20 ms to | 10,426 rps | 10,882 | 10,882 | yes, slightly higher |
| Go never crossed 20 ms in any ladder | no crossing | crosses at 12,181 (prepared-parity) | crosses at 9,023 (prepared-parity), 9,715 (ado) | **no** |
| Capacity gap at least 1.28x | 1.28x | 1.12x | 1.02x to 1.22x (confirm stage) | **no** |
| CPU-ms per request at 11,093 rps, .NET:Go | 3.09x | 3.11x | 3.11x | yes |
| Anonymous memory at 11,093 rps, .NET:Go | 5.33x | 5.33x | 5.33x | yes |
| Per-request CPU is not a stack constant (.NET:Go at 600 and 11,093 rps; Go's CoV) | 4.72x, 3.14x; 7.4% | 4.71x, 3.14x; 7.2% | 4.71x, 3.14x; 7.5% | yes |
| Dapper or ADO.NET move the knee under 2.5% | -2.4%, -0.9% | Dapper -7.5%, ADO -0.3% | Dapper -17.1%, ADO -0.3% | **ADO yes, Dapper no** |
| Max Auto Prepare moves EF Core's knee 7x | 1,468 to 10,426 | about 9x | about 9x | **effect yes, "7x" no** |

Notes on the rows that change:

- **The capacity gap.** The confirm stage, with 10 windows per rate, is the steadier reading. At
  11,093 rps Go meets the promise (11.2 ms mean p99) and .NET misses it (22.7 ms) in all three
  versions. At 13,312 rps Go misses too once every window counts (27.3 ms). So in
  `variant.prepared-parity` Go's crossing lies between 11,093 and 13,312 rps, which puts the gap
  between 1.02x and 1.22x over .NET's 10,882. B's ladder fit places Go at 9,023, below .NET, but
  a few ladder windows where vegeta fell behind pull it down, so I read it as a bound, not a
  better estimate.
- **The EF Core knee.** The published 1,468 came from the 1,493 rps rung, where the two counted
  windows read 40.3 and 2.5 ms; the third, dropped, read 2.5 ms. With all three the ladder never
  crosses up to 1,493. The confirm stage still misses at 1,244 rps (20.7 ms with all 10 windows)
  and the ladder meets at 1,037, so EF Core's default crossing lies between 1,037 and 1,244 rps:
  8.7x to 10.5x below prepared-parity's 10,882.

**What this means for the write-up.** The cost story holds: Go spends about 3.1x less CPU and
5.3x less memory per request, .NET's per-request cost falls with load, and EF Core's unprepared
path costs a lot. The capacity story is weaker than published: Go does cross 20 ms, and its
headroom over .NET is somewhere around 1.0x to 1.2x, not "at least 1.28x".

## 3. Other problems in this harness

- **The per-second p99 series is broken.** `per_second_quantiles_from_ndjson` in
  `bench/ledgerbench/vegeta.py` assumes vegeta's `.bin` is time-ordered. Vegeta writes results
  in completion order, and `timestamp` is the issue time, so every out-of-order record starts a
  new bucket. One 120 s window produced up to 25,355 "seconds". The warm-up flatness gates
  (`p99_flat`, `p999_flat`) read this series, so the gates that decided "under-warmed" were
  reading fragments.
- **One window can decide a knee.** The ladder runs 3 reps per rung, and in an episodic system a
  single collapsed window moves the rung mean across 20 ms (the EF Core knee above).
- **Rate-fidelity drops hide stalls.** When the SUT stalls, vegeta cannot hold the rate and the
  window is excluded. That removes exactly the windows where the latency promise failed worst.
- **Already known before this file:** host state was never read back, GC counters were polled
  but never written to disk, and the targets file replays in order against the cache TTL.

## 4. What I will change for the next run

**Host**

- Turn ITMT off for both stacks at every run start and resume, read it back, and restore it at
  the end. Booting with `amd_prefcore=disable` is the other way to do it.
- Record amd-pstate's mode, the preferred-core state, the per-CPU ranking, kernel, BIOS and
  microcode in the manifest.
- Since real servers may run with ITMT on, run a small ITMT-on sensitivity check and publish it
  beside the headline.
- Read back every tuned setting (governor, boost, C-states, THP, IRQs, cpusets) at start and on
  every resume, and refuse to measure on a mismatch.

**Measurement**

- Fix the per-second bucketer (bucket by issue second, emit sorted) and publish per-second p99
  beside pooled p99 for every window, so episodes are visible instead of averaged away.
- Keep every window. The headline follows pre-registered rules, and the every-window result is
  published next to it, so nobody has to trust the exclusions.
- Treat a window where the generator cannot hold the rate as a failure of the promise. Check the
  generator's own headroom separately, so a generator limit is never confused with a SUT stall.
- More reps near the crossing; the confirm stage, with 10 or more windows, decides the knee.
- Record what explains a stall: per-CPU scheduler counters, GC pauses per window, connection
  pool wait time, PostgreSQL wait events.
- Several full runs on different nights, so a result that replicates can be told apart from one
  that does not.

**Process**

- Pre-register the rules before any measured data and get a hostile review before the freeze.
  No service change for performance after the first measured data.
- The then-current Go, .NET, PostgreSQL and kernel versions, pinned and recorded.

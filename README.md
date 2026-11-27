# LMCache Fleet

<!-- Every CI lane, in one row. Smoke Test and Released Check run on push to
     main (Released Check also on a weekly schedule), so both are pinned with
     ?branch=main. The lint lanes are PR-only: they have no push-to-main and no
     cron trigger, so they have no run on main at all, and shields renders
     "no status" for a pinned badge.

     The lowercase "passing" is shields', not a typo: for a dynamic badge the
     message comes from the upstream API and shields has no case option --
     messageCase, textCase, upperCase and an explicit message= are all ignored.
     Capitalising it would mean an endpoint badge fed by JSON we publish, which
     would then only refresh when a lane runs, and a stale "Passing" over a
     broken lane is worse than a lowercase honest one. -->
[![Smoke Test](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-smoke-test.yml?branch=main&label=smoke-test&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-smoke-test.yml)
[![Docker Lint](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-docker-lint.yml?label=docker-lint&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-docker-lint.yml)
[![Shell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-shell-check.yml?label=shell-check&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-shell-check.yml)
[![Spell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-spell-check.yml?label=spell-check&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-spell-check.yml)
[![Ruff](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-ruff.yml?label=ruff&style=flat-square&logo=ruff&logoColor=white)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-ruff.yml)
[![Python Check](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-python-check.yml?label=python-check&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-python-check.yml)
[![Package Check](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-package-check.yml?label=package-check&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-package-check.yml)
[![Released Check](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-released-check.yml?branch=main&label=released-check&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-released-check.yml)

<!-- What the fleet produces, rather than whether CI is green: the dashboard
     the smoke-test lane screenshotted on its last run on main, republished to
     gh-pages because an artifact URL is per-run and expires, and the plugin as
     published. The Grafana label is static on purpose — the smoke-test badge
     above already reports whether that run passed. -->
[![Live Dashboard](https://img.shields.io/badge/Live%20Dashboard-Grafana-orange?style=flat-square&logo=grafana&logoColor=white)](https://sbates130272.github.io/lmcache-fleet/)
[![PyPI](https://img.shields.io/pypi/v/lmcache-l2-sim?style=flat-square&label=lmcache-l2-sim&color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/lmcache-l2-sim/)

[![License](https://img.shields.io/github/license/sbates130272/lmcache-fleet?label=License&style=flat-square)](LICENSE)
[![LMCache](https://img.shields.io/badge/LMCache-v0.5.8-blue?style=flat-square)](https://github.com/LMCache/LMCache/releases/tag/v0.5.8)
[![CPU Image](https://img.shields.io/badge/CPU%20Image-1.68%20GB-brightgreen?style=flat-square&logo=docker&logoColor=white)](docker/Dockerfile)
[![Last Commit](https://img.shields.io/github/last-commit/sbates130272/lmcache-fleet?label=Last%20Commit&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/commits/main)

This repository contains the tools needed to generate and deploy a
programmable-sized LMCache MP cluster: N cache servers (L1 DRAM + L2 fs adapter),
one coordinator building the fleet-wide key directory, and per-server one-shot
stimulators (no vLLM) that store synthetic KV chunks and, separately, fetch
them back. Eviction past the L1 watermark spills blocks to L2, an optional L2
capacity evicts from there in turn, and the coordinator directory tracks both
placements.

## Usage

```bash
make up NUM=4 SEQ=20            # generate, start fleet + prometheus + grafana, print the URL
make stim NUM=4 SEQ=20          # store KV blocks (stores only, nothing read back)
make restore NUM=4 SEQ=20       # fetch blocks back, from L1 and through L2
make trace NUM=4 TRACE=synth    # replay a token trace of agent sessions instead
make report NUM=4               # directory: per-instance L1/L2 placements
make check NUM=4                # same directory, as a pass/fail assertion
make dash                       # Grafana URL (compose picks a free host port)
make shot SHOT=x.png WINDOW=5m  # screenshot the dashboard (needs playwright)
```

The compose file is generated into `docker/`, so plain `docker compose ...` from
the repo root needs `export COMPOSE_FILE=docker/docker-compose.yml` first. The
make targets pass `-f` themselves.

## Layout

```
.github/     CI: one smoke-test lane that runs a real fleet, three lint lanes
docker/      Dockerfile for the slim CPU-only image; generated docker-compose.yml
scripts/     gen_compose.py (writes the compose file), stim.py (the stimulator),
             traces.py and tracelib.py (token traces), report.sh, check_fleet.py,
             check_tracelib.py
monitoring/  prometheus config (generated), directory exporter, Grafana provisioning
plugins/     lmcache_l2_sim, the out-of-tree dedup + latency L2 adapter
data/        bind mounts: L2 blobs, Prometheus TSDB, Grafana state, exporter
             state, coordinator checkpoint; token traces and replay logs
```

## The Image

One image, built here rather than pulled: [docker/Dockerfile](docker/Dockerfile)
puts LMCache v0.5.8 on `python:3.12-slim` and comes out at **1.68 GB**. `make up`
builds it on demand, so a fresh clone needs no extra step; `make image` builds it
on its own if you want that.

This fleet never touches a GPU — the servers run `--mode cpu` and there is no
vLLM — so the image carries no GPU runtime at all. Two things make that work.
LMCache's dependency list requires `cupy-cuda13x`, `cuda-python`,
`cufile-python` and `nvtx` unconditionally, but nothing on the paths this demo
uses imports them, so the wheel is installed with `--no-deps` and the genuinely
needed dependencies are listed explicitly. And `lmcache_native.so` links
`libc10.so` and `libtorch_cpu.so` and nothing else, so a CPU-only torch
satisfies it. That linkage is ABI-sensitive: the `TORCH_VERSION` build arg must
match the torch the wheel was compiled against, or `import lmcache` fails with
an undefined symbol. The build asserts this rather than shipping a broken image.

Verified on a full fleet cycle: coordinator and servers healthy, stimulators
exit 0, 116 keys tracked with 80 L1 and 160 L2 placements, and a clean log.

`LMCACHE_IMAGE` still overrides the tag if you want to point the fleet at some
other build, but nothing here is tested against one.

## Dummy L2: Many KV Blocks with Almost No Disk

`L2_DEDUP=1` wraps the `fs` L2 adapter with [plugins/lmcache_l2_sim](plugins/),
an out-of-tree adapter loaded through LMCache's built-in `plugin` type (no image
rebuild). The server stores exactly as before and still believes every key is
its own file, but once each write completes the file is replaced by a hardlink
to one shared canonical file of that size.

```bash
L2_DEDUP=1 make up NUM=2 SEQ=40
L2_DEDUP=1 make stim NUM=2 SEQ=40
```

Same run, stock vs dedup (2 servers, `L1_GIB=0.05 SEQ=40`):

| | stock `fs` | `L2_DEDUP=1` |
| --- | --- | --- |
| `.data` filenames per server | 80 | 80 |
| disk used per server (`du`) | 81 MB | 1.1 MB |
| coordinator keys tracked | 116 | 116 |
| coordinator L2 placements | 160 | 160 |

`ls -l` still shows every file at its full size; only `du` tells them apart.

L2 latency can be modelled too, with or without dedup:
`L2_READ_DELAY_MS`, `L2_WRITE_DELAY_MS`, their `_PER_MB` variants, and
`L2_DELAY_JITTER`. `L2_LINK_MODE=symlink` makes the sharing visible in `ls -l`.

**What is fake.** An L2 read returns the shared block, not the KV stored under
that key. The store path, key directory, eviction, and L2 traffic are all real;
L2 *contents* are not. `gen_compose.py` therefore drops the stimulators' `--url`
checksum verification whenever dedup is on. Do not point a real model at it.

See [plugins/README.md](plugins/README.md) for the full parameter list, using the
adapter outside this demo, and the upstreaming note.

## L2 Capacity and Eviction

By default L2 grows without bound. `L2_CAPACITY_GIB` gives each server an L2
capacity, and LMCache then evicts from L2 once usage crosses a watermark:

```bash
L2_DEDUP=1 L1_GIB=0.05 L2_CAPACITY_GIB=0.1 make run NUM=2 SEQ=120
```

Each server stores 240 blocks (240 MiB logical) against 0.1 GiB. Verified on
that run: eviction fires 35 times per server, each ends holding 80 L2 blocks
(78% of capacity), the coordinator's directory agrees at 80 L2 placements, and
the exporter counts the other 160 as removed. `L2_EVICT_WATERMARK` (0.8),
`L2_EVICT_RATIO` (0.2) and `L2_EVICT_POLICY` (`LRU` or `ARC`) tune it.

Capacity is counted in **logical** bytes — what the server believes it stored.
Under dedup that is the modelled size, not the megabyte of real disk, so a
terabyte tier can be sized and filled on a laptop. Without dedup the same knob
bounds real bytes: the run above ends at 81 MB on disk per server.

Setting it changes three things in the generated `--l2-adapter` spec, and all
three are needed. Get one wrong and nothing is evicted:

- The backing adapter becomes `fs_native`. Plain `fs` declares no capacity, so
  LMCache logs `does not support global eviction` and skips it.
- `max_capacity_gb` goes on that backing spec.
- The `eviction` block goes on the **outermost** spec. Inside the plugin's
  `inner` it parses cleanly and is then ignored, with usage running straight
  past the capacity and no warning from LMCache.

Where to watch it: `make report` prints each server's usage against capacity
(read from `/status` — the servers export L2 usage to Prometheus, but not the
capacity or watermark). On the dashboard, "L2 Bytes per Server" flattens at the
watermark instead of climbing, "Block Churn per Minute" grows an "L2 Removed"
line, and "Oldest L2 Block per Server" stops rising.

One gap: blocks left on disk by an earlier run of the same server are not
counted towards its usage, so eviction never reclaims them. `make clean`
between runs when capacity matters.

## Storing and Restoring

Stores and restores are separate jobs, so the write side and the read side of
the cache can be driven apart:

```bash
L2_DEDUP=1 L1_GIB=0.05 make run NUM=2 SEQ=60     # fleet up, stim-N store their windows
L2_DEDUP=1 L1_GIB=0.05 make restore NUM=2 SEQ=60 # restore-N fetch blocks back
```

`stim-N` looks each sequence in its window up and stores what is missing.
`restore-N` looks sequences from the same window up and retrieves what is
there, the way an engine pulls a prefix back, and stores nothing. A block still
in L1 is served from L1; one that has spilled is loaded from L2 into L1 first,
which in turn evicts something else from L1. Run `make restore` after the
stores, or alongside them. Pass it the same variables as the run it follows:
the compose file is regenerated, and a changed server definition would restart
the servers.

Both are [scripts/stim.py](scripts/stim.py), which drives the client inside
`lmcache bench server` one operation at a time. The bench itself is not used
because it is a sanity test with one fixed case: store a sequence, then read
the same one straight back. Every block is then read while it is still in L1,
L2 never serves a read, and the two rates cannot be separated. `STIM_OP=verify`
brings that case back when the point is to check that bytes round-trip.

| Variable | Default | |
| --- | --- | --- |
| `RESTORE_ORDER` | `random` | `random` samples the window with replacement; `sequential` walks it, oldest first |
| `RESTORE_COUNT` | `SEQ` | requests each `restore-N` sends |
| `RESTORE_INTERVAL` | `0.2` | seconds between them; `STIM_INTERVAL` is the store side |
| `RESTORE_MIN_HIT_RATE` | `0` | a `restore-N` exits non-zero below this fraction of chunks found |

Each stimulator ends with one JSON line in its log — requests, chunks hit,
stored and retrieved, hit rate, and p50/p99 latency per operation:

```bash
docker compose -f docker/docker-compose.yml --profile restore logs | grep '{"op"'
```

A restore cannot find what L2 eviction has already taken, so with
`L2_CAPACITY_GIB` set the hit rate is below 1 by design. On the dashboard,
"Chunks Retrieved by Clients/Second" is the read side as a whole, "L2 to L1
Loads/Second" is the part that had to come from L2, and "Lookup Hit
Tokens/Second by Tier" splits the hits by tier.

Each server has its own L2 directory, so a restore only finds what *its*
server stored: `restore-N` reads the window `stim-N` wrote.

## Replaying Agent Traces

The windows above are synthetic: every block is its own 512-token prompt, so
nothing is reused that the stimulator did not ask for twice. `make trace`
replays a **token trace** instead, where requests are whole prompts of real
length, arriving when a session sent them, and reuse is whatever the sessions
had in common.

```bash
make trace-synth NAME=synth                      # an artificial trace, or
make trace-exgentic NAME=exgentic SESSIONS=12    # real agent sessions, tokenized
make trace-analyze TRACE=exgentic/tokens         # what a perfect cache could reuse
L2_DEDUP=1 L1_GIB=0.02 make up NUM=2
L2_DEDUP=1 L1_GIB=0.02 TRACE=exgentic/tokens make trace NUM=2
make trace-report                                # hit rates, per client and fleet
```

Each request is looked up, the prefix that hit is retrieved, and the rest is
stored — what an engine does with a prompt. LMCache keys a chunk by a hash
chained over every token before it, so two requests share chunks exactly as far
as their prompts share a prefix; the stimulator only has to send the right
tokens.

**Where traces come from.** Published agent traces are mostly anonymised: the
corpus aiperf's AgentX scenario replays carries block ids and timings, but no
text. `trace-exgentic` uses one that keeps the text,
[Exgentic/agent-llm-traces-v2](https://huggingface.co/datasets/Exgentic/agent-llm-traces-v2)
(10,056 sessions, with messages, tool definitions and timestamps), in two steps:

1. `traces.py convert` writes it as aiperf `mooncake_trace` JSONL — full
   `messages` and `tools` per request. That file is the interchange: aiperf
   replays the same one against a real engine with
   `aiperf profile --custom-dataset-type mooncake_trace`, and any other corpus
   converted to it tokenizes the same way.
2. `traces.py tokenize` renders each request through the chat template of
   `TOKENIZER` (default `Qwen/Qwen2.5-7B-Instruct`; use the model you would
   serve) and writes the token trace. Requests are stored as deltas against the
   previous one in their session, so 12 sessions of 110k-token prompts — 33.8M
   prompt tokens — take 1.5M tokens on disk.

`trace-synth` skips the text and writes a token trace directly: one system
prompt every session shares, a context each group shares, turns that grow, and
occasional compaction. `SYNTH_ARGS="--sessions 200 --groups 8"` and so on;
`traces.py synth --help` lists them.

**What it measures.** The stimulator remembers every chunk it stored, so a hit
is attributed: to the same session, to another session on this server, or
*remote* — a chunk this server never stored, which can only have come from a
tier the servers share. A miss on a chunk it did store is *lost*: evicted
since. The summary line adds two histograms, `hit_gap_s` and `lost_gap_s`: how
long a chunk had sat since it was last touched when it hit, and when it was
gone. One JSON line per request goes to `data/trace-runs/<TRACE_RUN>/`, which
`make trace-report` totals.

`trace-analyze` is the same accounting with no fleet: an unbounded cache, by
routing and by whether the servers share a tier. It is the ceiling a run is
measured against. The 12 real sessions above come out 99.0% reusable, and 77.7%
of it from *another* session: every one opens with the same 110k tokens of
tool definitions.

| Variable | Default | |
| --- | --- | --- |
| `TRACE` | | token trace to replay, a directory under `data/traces` |
| `TRACE_ROUTE` | `session` | what stays on one server: a `session`, a `group` (sessions that share context), or nothing — `request` moves every request to the next server |
| `TRACE_LANES` | `8` | sessions in flight across the fleet |
| `TRACE_SPEEDUP` | `1` | divide the trace's timing by this |
| `TRACE_IDLE_CAP` | `60` | longest gap kept between a session's requests, in trace seconds |
| `TRACE_DURATION`, `TRACE_SESSIONS` | `0` | stop after this many replay seconds, or sessions |
| `TRACE_SHAPE` | `(2,8192,16,1,8):float16:1` | KV shape for trace clients |

The schedule is open-loop: it is computed from the trace alone, so every
`trace-N` derives the same one and keeps the requests routed to its server.
A client that cannot keep up does not slow the trace down; it falls behind, and
`lag_ms_p50`/`p99` in its summary say by how much. Lower `TRACE_SPEEDUP` if
they are large.

`TRACE_SHAPE` is small on purpose. At the default `SHAPE_SPEC` a chunk is 1 MiB
and a 110k-token prompt would be 430 MiB of KV; at the trace shape a chunk is
8 KiB. Size `L1_GIB` and `L2_CAPACITY_GIB` against that. Its 8192 blocks of 16
tokens bound a prompt at 131,072 tokens; longer ones are truncated and counted.

## A Shared L2 Tier

By default each server has its own L2 and a block is only ever found by the
server that stored it. `L2_SHARED=1` adds a second tier that servers share:

```bash
L2_DEDUP=1 L2_SHARED=1 make up NUM=4                  # local L2, then one shared L2
L2_DEDUP=1 L2_SHARED=1 SHARE_GROUPS=2 make up NUM=4   # two shared L2s, two servers each
L2_DEDUP=1 L2_SHARED=only make up NUM=4               # the shared tier alone
```

LMCache takes `--l2-adapter` more than once. Every key is stored to every
adapter, and a lookup is served by the first adapter, in the order given, that
holds it. So this is not a cascade — nothing is demoted from one tier to the
next. The local tier is listed first (a server's own NVMe) and the shared one
second (`data/l2-shared-N`, mounted into every server of its group and marked
`"shared": true`), so a block comes back from local disk when this server has
it and from the shared tier when only another server stored it.

`SHARE_GROUPS` is the knob for *how* shared: servers are dealt across that many
shared directories, so `1` is the whole fleet sharing and `NUM` is nobody
sharing. Measured on 4 servers with `TRACE_ROUTE=request`, the routing that
gives a server no history of its own to lean on (40 synthetic sessions, 58,016
chunks; `trace-analyze` puts the ceilings at 80.1% and 94.0%):

| `SHARE_GROUPS` | fleet hit rate | hits from another server |
| --- | --- | --- |
| 4 (none share) | 80.1% | 0% |
| 2 | 88.5% | 8.3% |
| 1 (all share) | 93.6% | 13.5% |

The gap to 94.0% is a race the model keeps: a block is written to L2 after it
is in L1, so a request that reaches a second server a moment later misses it.

The shared tier takes the same simulation as the local one: `L2_DEDUP` applies
to both, and `L2_SHARED_READ_DELAY_MS`, `L2_SHARED_WRITE_DELAY_MS` and
`L2_SHARED_CAPACITY_GIB` are its own latency and capacity. Three things to know:

- **Capacity is per server.** Each server counts only the bytes it wrote, so
  `L2_SHARED_CAPACITY_GIB` bounds each server's contribution, not the
  directory. A block one server evicts is simply a miss for the others.
- **Temp files.** Both fs adapters write `<key>.tmp` and rename it, so two
  servers storing one block at once rename each other's file away and the
  loser logs a `FileNotFoundError`. The generated spec gives each server its
  own `relative_tmp_dir`, which avoids it; a hand-written shared spec needs the
  same.
- **The directory counts a shared block once**, under whichever server reported
  it first. The exporter labels those placements `backend="...-shared"`, so the
  shared tier is a separate series from each server's local L2. LMCache's own
  per-server L2 metrics name an adapter by its type, so with two tiers of one
  type they are a single series.

## Surviving a Coordinator Restart

The coordinator's key directory is derived state, rebuilt from the cache
events the servers report, so losing it costs hit rate rather than
correctness. But a restart otherwise starts cold, and on a fleet that has been
storing for an hour that means the dashboard and `make report` read empty
until the servers report again.

`COORD_CHECKPOINT` (on by default) points `--checkpoint-path` at
`data/coordinator/checkpoint.json`, written every
`COORD_CHECKPOINT_INTERVAL` seconds (60; 0 writes only on a clean stop).
`--metadata-path` goes beside it for operator-set state — L2 pins and
per-`cache_salt` quotas — which is written when it changes rather than on a
timer, so the file only appears once something sets one.

Verified by storing 80 keys, killing the coordinator with `SIGKILL` so there
was no clean stop, and restarting it: the directory came back at 80 keys.

Both files live under `data/`, so `make clean` removes them with the rest of a
run's state rather than leaving a stale checkpoint for the next fleet to
resume from. `COORD_CHECKPOINT=0` turns it off and the coordinator keeps
nothing.

## When One Coordinator Is Not Enough

A coordinator is one process, and it decodes and applies every cache event on
one event loop. A server posts its events about once a second, gives the post
two seconds, and does not retry: a post that times out is dropped, and a late
one that still arrives is rejected as a duplicate of the newer one already
applied. So an overloaded coordinator does not slow down, it stops being right,
and spends its core decoding posts it will throw away.

Measured on 100 servers replaying 2M agent sessions (92 requests a second,
about 850 events a request) against a single coordinator: 81 batches a second
applied, 300 decoded and rejected, 579 never received, 47,000 events a second
dropped by servers, every server's view incomplete. Cache hits were unaffected
— the coordinator is not on the request path — but every panel built on the
key directory under-reported.

`NUM_COORD` deals the servers across that many coordinators:

```bash
L2_DEDUP=1 NUM_COORD=2 make up NUM=20     # two coordinators, ten servers each
```

LMCache v0.5.8 has no coordinator clustering, so these are pods, not a cluster:
each coordinator tracks its own servers and knows nothing of the rest, with its
own directory exporter and its own host port, counting up from `COORD_PORT`.
Prometheus labels every series with its `coordinator`, and the dashboard's
fleet totals sum across them. The same trace at the same load per server, ten
servers to a coordinator: 65 batches a second applied by each, none rejected,
missing or dropped, event loop blocked 4% of the time.

Three things to know:

- **Totals are sums of pods.** A key held under two coordinators is counted
  twice in "Keys Tracked", and "Most Reused Blocks" lists each pod's top ten.
- **`make report` and `make check` read the first coordinator only.**
- **A shared L2 is tracked per pod.** Each coordinator records the shared
  placements its own servers report. `NUM_COORD` equal to `SHARE_GROUPS` gives
  every shared directory exactly one coordinator.

Two more knobs for a large fleet. `KV_EVENT_LOG_SIZE` (default 0 here) is the
servers' KV-event channel: a log and a token cache kept for engines that
subscribe to it, which nothing in this fleet does, and which otherwise grows by
about a quarter of a GiB an hour per server until its bounds are reached.
`DIRECTORY_PAGE_SIZE` (default 1000, at most 10000) is how many keys the
exporter reads at a time; on a directory of tens of thousands of keys, larger
pages cost the coordinator fewer passes over it. `DIRECTORY_POLL` can then be
minutes: Prometheus reads the exporter's last finished poll at most a minute
apart regardless, so the panels do not go stale between polls.

## Above the Coordinators: Fleet Aggregators

Coordinators that each track their own servers leave nobody who can say how
many distinct blocks the fleet holds, which are reused most, or which
coordinator to ask about one. Collecting their keys in one place is not the
answer: that is the directory the fleet outgrew, one level up.

Instead each directory exporter publishes a **summary** of its coordinator's
directory at `/summary`, built so that the summary of several directories is
computed from their summaries alone
([fleet_summary.py](monitoring/exporter/fleet_summary.py)): counts, which add;
HyperLogLog sketches of block hashes, which merge by taking the larger of each
register, so a block under ten coordinators counts once; the hundred most
accessed blocks, whose counts add; and a Bloom filter of block hashes, which
merges by OR. A **fleet aggregator**
([fleet_aggregator.py](monitoring/exporter/fleet_aggregator.py)) reads its
children's summaries, merges them, and serves the result in the same form.

That last part is what makes it a tree rather than a tier. An aggregator's
children can be aggregators, each level holds a bounded summary per child
however many servers lie beneath it, and merging is associative, so the root
reports the same numbers however the coordinators are grouped.
[check_fleet_summary.py](scripts/check_fleet_summary.py) asserts exactly that —
eight directories merged flat, as a binary tree, unevenly and as a chain eight
deep, each level over the wire, all agree — and runs in CI.

```bash
L2_DEDUP=1 NUM_COORD=4 make up NUM=8                # one aggregator over four coordinators
L2_DEDUP=1 NUM_COORD=4 AGG_FANOUT=2 make up NUM=8   # two over pairs, and a root over those
curl -s "localhost:9106/locate?hash=<chunk hash>"   # which coordinators may hold a block
```

Aggregators run whenever `NUM_COORD` is more than one. `AGG_FANOUT` is children
per aggregator: 0 puts one over everything, and anything from 2 up builds
levels until one root remains, always the service `aggregator`, published on
`AGG_PORT`. The dashboard's "Whole Fleet" row reads the root.

Measured on 8 servers under 4 coordinators with two levels of aggregator,
against every key read directly from the four coordinators: 26,070 blocks
added up, exact; 24,166 distinct, reported as 24,209 (0.18% over); the five
most reused identical, each correctly found under all four coordinators; and
`/locate` naming exactly the right coordinators for a block under all four,
under one, and under none, asking only the children that could hold it.

What it does not do:

- **Distinct counts are estimates**, about 0.4% off. "Copies beyond the first"
  is the difference of two large numbers and carries that error whole: 1,861
  reported against 1,904.
- **The most-reused list can miss a block** that is outside every coordinator's
  own hundred but large in total.
- **`/locate` is a hint.** A filter can name a coordinator that does not hold
  the block (about 1% of the time at 870,000 blocks under the root, more past
  that; `SUMMARY_BLOOM_BITS` sizes it, the same at every level), never the
  reverse. Check with the coordinator, as with any directory answer.
- **It is as fresh as its slowest path**: an exporter's poll plus one
  aggregator interval per level. A child that stops answering keeps its last
  summary and is counted as stale.

**Kafka instead?** LMCache can carry cache events over Kafka rather than HTTP
posts, and it is the better transport, not an alternative to this. Tried on
four servers with the coordinator frozen for 30 seconds mid-run: over HTTP it
lost 1,436 batches and ended with 2,212 of 2,400 L2 placements and every
server's view incomplete; over Kafka it ended with all 2,400 and no gaps,
because a late coordinator reads what it missed instead of the servers
dropping it. Two coordinators in one consumer group do split the stream
between them. But the split follows Kafka's partitioning, not which
coordinator a server registered with, so a coordinator ends up holding
placements for servers it has never heard of; and each coordinator still holds
only its share, with nothing above it. Kafka fixes loss. It does not add the
view across coordinators, which is what the aggregators are for. Nothing here
generates a Kafka fleet yet; note that LMCache needs `confluent-kafka<2.16`,
and with 2.16.2 the coordinator's `/metrics` handler blocks its event loop for
good on the first scrape.

## Monitoring the Fleet

`make up` also starts Prometheus and Grafana on the fleet network. Grafana is the
only service published past loopback. gen_compose picks the lowest free port in
`GRAFANA_PORTS` (default `3000-3010`) and writes it as a fixed mapping, so it
lands somewhere free on a busy node *and* stays put across restarts — handing
compose the range instead lets docker's allocator walk forward past
recently-used ports, so the URL drifts every time. If our grafana is already
running, its current port wins, so regenerating never moves it. `GRAFANA_PORT`
pins one exactly, and `make dash` always prints the live one. There is no login — anonymous access is
on and the fleet dashboard is the home page.

Prometheus scrapes `/metrics` on the coordinator (`:9300`) and on every MP server
(`:8080` — *not* the server's `--prometheus-port 9090`, which does not bind when
metrics are already on the HTTP app). `make targets` lists them with their health.

**What the run is, and whether it is healthy.** The dashboard opens on a
table describing the fleet it is showing — tier sizes and eviction, what the
L2 simulator is doing, the workload — and a row of health tiles under it:
servers up and down, the coordinator, how busy it is, how late and how lossy
the cache-event stream is, and how stale the directory inventory is. None of
the settings are in any LMCache metric, so gen_compose writes them as the
labels of one constant series, `lmcache_fleet_run_info`, in a generated
Prometheus rule (`monitoring/rules.yml`); the make targets send Prometheus a
SIGHUP so a regenerated description is picked up. A rule rather than the
exporter, because the description is most wanted when the exporter and the
coordinator are the things in trouble. The coordinator tiles are the ones to
watch on a large fleet: it is a single process, and once it falls behind the
servers time out, drop events, and the directory panels quietly under-report.

The rest of the dashboard pairs two views, because they are not
interchangeable:

- *Coordinator view* — keys tracked, placements by tier, fleet bytes by tier, and
  **L1 keys per server** from
  `lmcache_coordinator_key_directory_server_l1_keys{instance_id=...}`. The
  coordinator identifies servers by UUID, so the dashboard joins through the
  servers' `target_info` (which carries the same UUID next to the scrape's
  friendly `server` label) to show `lmcache-server-N` instead.
- *Per-server view* — resident L1 chunks (`l1_write_chunks_total -
  l1_evicted_chunks_total`), L1 bytes, L1 usage ratio against the eviction
  watermark, L2 bytes, and write/eviction rates, each split by server. This comes
  from the servers' own metrics, labelled `server` by the scrape config.

- *Directory inventory* — a small sidecar,
  [monitoring/exporter/directory_exporter.py](monitoring/exporter/directory_exporter.py),
  polls the coordinator's `GET /directory/keys` every `DIRECTORY_POLL` seconds
  and publishes `lmcache_directory_*` metrics. These feed the dashboard's lower
  section: L2 blocks and logical bytes **per server**, blocks by age, the oldest
  block per server, how many servers hold each key, churn per minute, and keys
  by access count.

Block age is measured by the exporter, not by LMCache: the directory carries no
timestamps, so a block's age is the time since the exporter first saw it. Blocks
already present when the exporter first runs start at zero. First-seen times are
checkpointed to `data/exporter/first_seen.json` every `STATE_SAVE_INTERVAL`
seconds, so restarting the exporter no longer resets ages.

Age bands are configurable with `AGE_BOUNDS`, a comma-separated list of
durations — `AGE_BOUNDS=1m,5m,15m,1h` for a short run, `1h,6h,1d,7d` for a soak.
The default is `5m,30m,1h,2h,4h,8h`. Bucket *labels* are derived from the bounds,
so the two cannot drift apart.

The exporter also publishes a real Prometheus histogram,
`lmcache_directory_placement_age_seconds_bucket`, which is what makes the view
dynamic: the **age quantiles** and **age distribution heatmap** panels compute
their banding at query time and follow whatever `AGE_BOUNDS` you set. The named
stacked panel is the readable form, but its colours assume the default seven
bands. Bounds are deliberately not auto-scaled to the observed ages: labels that
changed mid-run would churn the Prometheus series and silently mix datapoints
that used different bucket definitions.

The dashboard has a **Server** variable at the top: pick one server, several, or
All. It filters every panel whose metric carries a `server` label — the
per-server L1/L2 panels and the directory inventory. Fleet-wide coordinator
panels (placements by tier, keys by replica count, access counts) have no such
label and always show the whole fleet.

The two L1 panels are deliberately redundant: one reads the coordinator's event-
sourced directory, the other reads the servers directly. They should agree, and
a divergence means a directory consumer has drifted from what the fleet holds.

The coordinator's own *metrics* have no per-server view of L2:
`key_directory_placements` and `placement_bytes` carry only a `tier` label, on
`main` as well as v0.5.8. The per-server L2 numbers therefore come from the
directory exporter, which reads them from the directory listing instead.

**The most reused blocks, named.** Everything else the exporter publishes is
an aggregate, because a series per key over a directory of this size would be
a cardinality disaster. The "Most Reused Blocks" table is the exception: the
top `TOP_REUSED` keys (10; 0 disables) by access count, each with its chunk
hash, every server and tier holding a copy, and the age of its oldest copy.
Ties break by hash so the ranking does not shuffle between polls. The hash and
location labels still churn as the ranking moves, which is why it stays small.

**Restarted servers.** A server takes a new instance id every time it starts.
When one leaves, the coordinator drops its L1 placements but keeps its L2 ones
on purpose — L2 outlives the process — so the directory goes on listing L2
blocks owned by ids that no longer exist. The exporter's per-server metrics
cover registered instances only, and what departed ones left behind is
published as `lmcache_directory_departed_*` under the server name the instance
had, charted in the last row of the dashboard. A restart therefore adds no
series and no legend entry. Nothing evicts those blocks: no running server
counts them towards its L2 usage, so they stay until `make clean`. A restart
is also not counted as L2 churn, which leaves "L2 Removed" meaning evicted or
deleted.

## Reporting on the Fleet

`report.sh` output (verified on 2 servers, L1_GIB=0.05, SEQ=40, 512 tokens/req):

```
fleet: 2 registered instance(s)
unique keys tracked: 80
  <instance-1>  l1 via dram : 40
  <instance-1>  l2 via fs   : 80
  <instance-2>  l1 via dram : 40
  <instance-2>  l2 via fs   : 80
```

plus real `.data` files under `data/l2-server-*/`.

`make check` reports the same directory as a pass/fail assertion instead of a
listing: every server registered, keys tracked, and *each* server holding both
L1 and L2 placements. The per-server split is the part worth asserting — a
fleet where one server absorbed every store still shows healthy totals.

Both stimulators print a progress line every `--progress-every` seconds (15,
0 to disable) and one JSON summary when they finish — requests, chunks hit,
stored and retrieved, hit rate and per-operation p50/p99:

```bash
make logs                     # follows every service, stimulators included
docker compose -f docker/docker-compose.yml --profile stim --profile restore \
  logs | grep '{"op"'         # just the summaries
```

`make logs` names both profiles explicitly: a service behind a compose profile
is invisible to `compose logs` otherwise, which is how the stimulators —
the only place those summaries appear — used to be filtered out.

## Quieter Logs

Prometheus scrapes `/metrics`, compose probes `/healthcheck`, the servers
heartbeat and the exporter pages `/directory/keys`, all every few seconds and
all logged by uvicorn at INFO. Measured over three minutes on a two-server
fleet: ~330 lines from the coordinator alone, ~72 from each server.

LMCache hardcodes `access_log=True` with no flag to turn it down, so
[docker/quiet/sitecustomize.py](docker/quiet/sitecustomize.py) goes on
`PYTHONPATH` instead — Python imports a module of that name at interpreter
startup, early enough to attach a filter to the access logger. Only polled
paths returning under 400 are dropped, so a failing healthcheck or a 404 on
`/metrics` still prints.

`QUIET_ACCESS_LOG=0` turns it off, `QUIET_ACCESS_PATHS` overrides the list
(an entry starting with `*` matches as a suffix, which is how heartbeats go
without taking `POST /instances` with them), and `GRAFANA_LOG_LEVEL` sets
Grafana's own.

## Continuous Integration

Eight lanes, all on GitHub-hosted `ubuntu-24.04`. Six are cheap PR gates —
codespell against `.codespellrc`, shellcheck over `scripts/*.sh`, hadolint
plus `docker compose config` on the Dockerfile and the *generated* compose file
(the stock, `L2_CAPACITY_GIB` and `L2_SHARED` variants, since a capacity swaps
the backing adapter and a shared tier adds a second one), ruff and the Python
lane below, and the packaging one after.

The `L2_SHARED` variant asserts what would otherwise break without a sound:
that the local tier is listed before the shared one, since a lookup takes the
first adapter holding the key; that the shared one is marked shared; and that
each server has its own temp directory in it.

`lmcache-fleet - Ruff` lints every tracked `.py` against
[ruff.toml](ruff.toml). Both the ruff version and the rule selection are
pinned, because ruff widens its defaults between releases and neither should
be able to fail the lane on code nobody touched. `E501` is deliberately out of
the selection, as it is from ruff's own default `E` subset: line length is a
formatter's job and this repo runs no formatter. Two rules are switched off
with the reasons recorded in the config — `SIM105`, because the two
`try/except/pass` sites are hot loops where `contextlib.suppress` would
allocate per iteration, and `I001` for the plugin, which keeps LMCache's own
import grouping so it stays upstreamable.

`lmcache-fleet - Python Check` asserts the committed dashboard JSON still
matches `build-dashboard.py`, and unit-checks the access-log filter. The first
is the reason the lane exists: the dashboard is generated *and* committed, so
editing the builder without regenerating leaves it silently on the old panels
with a clean-looking diff. The filter check matters for the same reason — a
broken `sitecustomize.py` does not stop a container starting, so the smoke
test would stay green while the logs quietly went back to being noisy.

It also runs [scripts/check_tracelib.py](scripts/check_tracelib.py). Every
`trace-N` computes the replay schedule and the routing on its own and keeps its
share, so the fleet replays a trace exactly once only if both are deterministic
and the shares partition it. When they are not, requests are sent twice or
never and the run still exits 0 with a plausible hit rate.

`lmcache-fleet - Package Check` runs `make dist` and `make publish-precheck`,
then exercises both halves of the publish path without touching PyPI: it
asserts `make publish` refuses without `CONFIRM=1`, and then runs
`make publish CONFIRM=1` against a stub index on loopback that accepts and
discards what it receives. A version uploaded to PyPI can never be replaced,
so a malformed `twine upload` is worth catching anywhere other than on the one
attempt that counts.

The fifth, `lmcache-fleet - Smoke Test`, runs a real fleet on a GitHub runner,
in one of two shapes:

```bash
# on a PR -- gates the merge, so it stays small and quick
L2_DEDUP=1 L1_GIB=0.05 make run NUM=2 SEQ=40
# on main -- gates nothing, and is where the published dashboard comes from
L2_DEDUP=1 L1_GIB=0.05 L2_CAPACITY_GIB=0.2 make run NUM=4 SEQ=200
```

A PR runs its shape twice, side by side: once with L2 unbounded and once with
`L2_CAPACITY_GIB=0.05`, because the two are different code paths end to end — a
different backing adapter, an eviction controller that either exists or does
not. `main` runs only the capped one, so the published dashboard shows L2
turning over. In the capped runs `make check` also requires every server to
have actually evicted and to sit within its capacity; both tiers fill whether
or not a capacity is enforced, so without that the run would pass on a fleet
that ignored it.

The PR shape is the verified recipe above; measured at 4m46s cold, of which
2m31s was the image build, so roughly 2m40s once the layer cache is warm. The
`main` shape is the only place the fleet is exercised past two servers, and it
runs long enough that the dashboard has a real curve on it rather than a spike.

The image is built in-workflow behind a buildx `type=gha` cache, which means
the Dockerfile is continuously tested rather than only when someone remembers.
`L1_GIB=0.05` makes the sequences overflow L1, so the run exercises the eviction
and L2 spill paths rather than just starting cleanly. And `L2_DEDUP=1` routes
L2 through the sim adapter, so the spill costs ~1 MB per server instead of
hundreds — the runner's disk never becomes the constraint.

The lane then asserts on the key directory with `make check` and screenshots
the live Grafana dashboard. That image is the only evidence the whole
monitoring chain worked: Prometheus scraped the exporter, the exporter read
the coordinator, and the panels rendered it. A green check cannot show that.

The dashboard window is set to the fleet's actual lifetime rather than a fixed
range, so the charts are full rather than a spike at the right edge — and since
Grafana prints the range it is showing, the screenshot states its own period.
`make shot` takes the same `WINDOW` if you are capturing one by hand.

Every run uploads it as a build artifact, so a PR's own dashboard can be
inspected before merge. Runs on `main` additionally publish it to
[**the dashboard page**](https://sbates130272.github.io/lmcache-fleet/) on the
`gh-pages` branch — artifact URLs are per-run and expire, so that is what the
README badge can point at and what always shows the current dashboard. On
failure the lane uploads fleet logs and a container listing instead.

A second job in the same lane covers the other workload and the other
topology — a token trace replayed against servers with a shared L2:

```bash
make trace-synth NAME=synth
L2_DEDUP=1 L1_GIB=0.02 L2_SHARED=1 make up NUM=2
L2_DEDUP=1 L1_GIB=0.02 L2_SHARED=1 TRACE=synth TRACE_ROUTE=request \
  TRACE_SPEEDUP=20 TRACE_MIN_HIT_RATE=0.9 make trace NUM=2
make trace-report REPORT_ARGS=--require-remote
```

It is a job of its own because `L2_SHARED` changes every server's definition,
and compose would recreate the fleet the other legs are asserting on. The trace
is synthetic and seeded, so it needs no download and its numbers repeat. With
`TRACE_ROUTE=request` the most two servers can hit from local L2s alone is
88.8%, and a run with the shared tier measures 93.4%, so the 0.9 floor fails
unless the shared tier is serving blocks. `--require-remote` then asks for the
direct evidence: a hit on a chunk the client never stored on its own server.

## Tunables (Environment Variables)

The demo reads around seventy-five environment variables. Rather than keep a table
here that drifts from the code, ask for the list:

```bash
make env-vars
```

It prints them grouped — Fleet, Workload, Trace replay, L2 simulation, L2
sharing, Monitoring, Runtime and the exporter's own — each with the default read straight out of the source,
so the listing cannot fall out of step. A variable the code reads but nobody
has described shows up under "undocumented" rather than going unmentioned.

`NUM` and `SEQ` are make variables; everything else is an environment variable:

```bash
L2_DEDUP=1 L1_GIB=0.05 make run NUM=2 SEQ=6000
```

To force visible L2 spill on a small host: `L1_GIB=0.05 SEQ=40`.

## Key Space

One stimulator per server, but no longer storing identical keys. `lmcache bench
server` derives its KV deterministically from the sequence number, so two
stimulators given the same `--start/--end` store byte-identical keys — which is
what the fleet used to do, making every server a replica of the same working set.

Its only key selector is a contiguous half-open `[--start, --end)` range; there
is no `--seed` and no way to hand it a scattered set. So `gen_compose.py` holds
the PRNG and decides where each stimulator's contiguous window lands inside a
shared key space:

- `TOTAL` — total unique blocks the fleet may address. A sequence covers
  `NUM_TOKENS/CHUNK_SIZE` blocks, so this is converted to sequences before
  reaching the bench.
- `SEQ` — how many sequences each stimulator stores, i.e. its window width.
- `SEED` — seeds the placement. Each stimulator is seeded as `SEED:i`, so a
  server's window stays put when `NUM_SERVERS` changes. Re-running with the same
  seed reproduces the layout exactly.
- `RANGE_LO` / `RANGE_HI` — optional block sub-range every window must land
  inside, for pinning the fleet onto part of a larger key space.

Windows may overlap, and the overlap is the interesting part: shared blocks are
replicated across servers while the rest are unique to one, so the coordinator's
directory becomes a real map instead of "everything, everywhere". `gen_compose.py`
prints each window and the resulting coverage:

```text
key space: TOTAL=160 blocks (80 sequences x 2 blocks), seed=0, range=[0,160)
  stim-1: sequences [12,52)  blocks [24,104)
  stim-2: sequences [30,70)  blocks [60,140)
  fleet covers 116 unique blocks; 44 blocks replicated
```

For disjoint windows, raise `TOTAL` relative to `NUM_SERVERS × SEQ`; for heavy
replication, lower it or narrow `RANGE_LO`/`RANGE_HI`.

## Gotchas

- `lmcache bench server --mode cpu --transfer-mode lmcache_driven` uses POSIX
  SHM → the stimulator MUST share the server's IPC namespace
  (`ipc: service:lmcache-server-N`). Fallback: `--transfer-mode engine_driven`
  (inline ZMQ transport, slower but namespace-agnostic).
- The `(…)` in `--kvcache-shape-spec` breaks compose's command tokenizer —
  keep it double-quoted.
- Named volumes for the fs L2 adapter are root-owned → PermissionError from the
  non-root container uid. Use bind mounts instead, created by gen_compose and
  owned by you. Services pin `user: <your uid>:<your gid>`, so the directories
  only need mode 755 and the `.data` blobs come back owned by you; gen_compose
  falls back to 777 only if `RUN_UID` is overridden to another user. The image
  defaults to uid 1000 (`lmcache`), which only happens to be right when your own
  uid is 1000. A non-1000 uid has no `/etc/passwd` entry
  and lands on `HOME=/`, so the services set `HOME=/tmp`.
- Don't use `container_name:` here — it is node-global, and names like
  `coordinator` collide with unrelated stacks. Compose's project prefix
  (`lmcache-fleet-demo-*`) keeps the fleet to itself; service DNS names on the
  `fleet` network are unaffected.
- **L2 accounting only covers the current process lifetime.** Nothing scans the
  L2 backend at startup, so `.data` files left in `data/l2-server-*/` by an
  earlier run are invisible: `lmcache_mp_l2_usage_bytes{l2_name="fs"}`, the
  coordinator's `tier="l2"` placements, and `GET /cache/objects?tier=l2` all read
  zero against a disk full of blobs. They populate normally once stores and
  evictions happen while the current server and coordinator are up. If L2 reads
  zero, check whether the spill predates the processes before concluding L2 is
  broken — and `make clean` between runs to keep the two in step.
- Bench clients allocate `n_kv*n_layers` tensors of NB*BS*H*D*2B each and
  migrate all of them to SHM — the old default shape (1024 blocks × 16 × 8 × 128
  × 32 layers) was ~2 GB per client and OOM-killed 3 servers on a 7 GB host.
- Coordinator directory inspection: `GET /instances`, `GET /directory/keys?limit=&offset=`;
  per-server L1/L2 contents: `GET :818x/cache/objects?tier=l1|l2`.

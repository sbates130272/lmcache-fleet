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
             report.sh, check_fleet.py
monitoring/  prometheus config (generated), directory exporter, Grafana provisioning
plugins/     lmcache_l2_sim, the out-of-tree dedup + latency L2 adapter
data/        bind mounts: L2 blobs, Prometheus TSDB, Grafana state, exporter state
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

The **LMCache Fleet** dashboard pairs the two views, because they are not
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

## Continuous Integration

Five lanes, all on GitHub-hosted `ubuntu-24.04`. Four are cheap PR gates —
codespell against `.codespellrc`, shellcheck over `scripts/*.sh`, hadolint
plus `docker compose config` on the Dockerfile and the *generated* compose file
(both the stock and `L2_DEDUP=1` variants, since dedup takes a different branch
in the generator), and the packaging lane below.

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

## Tunables (Environment Variables)

The demo reads around forty environment variables. Rather than keep a table
here that drifts from the code, ask for the list:

```bash
make env-vars
```

It prints them grouped — Fleet, Workload, L2 simulation, Monitoring, Runtime
and the exporter's own — each with the default read straight out of the source,
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

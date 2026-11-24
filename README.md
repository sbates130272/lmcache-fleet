# LMCache Fleet

<!-- The smoke-test lane runs on push to main as well as on PRs, so its badge
     is pinned with ?branch=main. The three lint lanes are PR-only: they have
     no push-to-main and no cron trigger, so they have no run on main at all,
     and shields renders "no status" for a pinned badge. -->
[![Smoke Test](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-smoke-test.yml?branch=main&label=smoke-test&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-smoke-test.yml)
[![Docker Lint](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-docker-lint.yml?label=docker-lint&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-docker-lint.yml)
[![Shell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-shell-check.yml?label=shell-check&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-shell-check.yml)
[![Spell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/lmcache-fleet/lmcache-fleet-spell-check.yml?label=spell-check&style=flat-square)](https://github.com/sbates130272/lmcache-fleet/actions/workflows/lmcache-fleet-spell-check.yml)

[![License](https://img.shields.io/github/license/sbates130272/lmcache-fleet?style=flat-square)](LICENSE)
[![LMCache](https://img.shields.io/badge/LMCache-v0.5.8-blue?style=flat-square)](https://github.com/LMCache/LMCache/releases/tag/v0.5.8)
[![Image](https://img.shields.io/badge/CPU%20image-1.67%20GB-brightgreen?style=flat-square&logo=docker&logoColor=white)](docker/Dockerfile)
[![Last Commit](https://img.shields.io/github/last-commit/sbates130272/lmcache-fleet?style=flat-square)](https://github.com/sbates130272/lmcache-fleet/commits/main)

This repository contains the tools needed to generate and deploy a
programmable-sized LMCache MP cluster: N cache servers (L1 DRAM + L2 fs adapter),
one coordinator building the fleet-wide key directory, and per-server one-shot
stimulators (`lmcache bench server --mode cpu`, no vLLM) that STORE synthetic KV
chunks. Eviction past the L1 watermark spills blocks to L2; the coordinator
directory tracks both placements.

## Usage

```bash
make up NUM=4 SEQ=20            # generate, start fleet + prometheus + grafana, print the URL
make stim NUM=4 SEQ=20          # make stores
make report NUM=4               # directory: per-instance L1/L2 placements
make check NUM=4                # same directory, as a pass/fail assertion
make dash                       # Grafana URL (compose picks a free host port)
make shot SHOT=grafana.png      # screenshot the dashboard (needs playwright)
```

The compose file is generated into `docker/`, so plain `docker compose ...` from
the repo root needs `export COMPOSE_FILE=docker/docker-compose.yml` first. The
make targets pass `-f` themselves.

## Layout

```
.github/     CI: one smoke-test lane that runs a real fleet, three lint lanes
docker/      Dockerfile for the slim CPU-only image; generated docker-compose.yml
scripts/     gen_compose.py (writes the compose file), report.sh, check_fleet.py
monitoring/  prometheus config (generated), directory exporter, Grafana provisioning
plugins/     lmcache_l2_sim, the out-of-tree dedup + latency L2 adapter
data/        bind mounts: L2 blobs, Prometheus TSDB, Grafana state, exporter state
```

## A slim, CPU-only image

The published `lmcache/standalone` is ~14.8 GB: a full CUDA toolchain on
`nvcr.io/nvidia/cuda-dl-base`, for a fleet that never touches a GPU.
[docker/Dockerfile](docker/Dockerfile) builds a CPU-only alternative on
`python:3.12-slim`:

```bash
docker build -f docker/Dockerfile -t lmcache-fleet-demo/lmcache:0.5.8-cpu .
LMCACHE_IMAGE=lmcache-fleet-demo/lmcache:0.5.8-cpu make up NUM=2
```

**1.67 GB against 14.8 GB**, verified on a full fleet cycle: coordinator and
servers healthy, stimulators exit 0, 116 keys tracked with 80 L1 and 160 L2
placements — matching the published image exactly — and zero errors or CUDA
banners in the logs. It is opt-in: `LMCACHE_IMAGE` still defaults to
`lmcache/standalone`.

Two things make it possible. LMCache's dependency list requires `cupy-cuda13x`,
`cuda-python`, `cufile-python` and `nvtx` unconditionally, but nothing on the
paths this demo uses imports them — so the wheel is installed with `--no-deps`
and the genuinely needed dependencies are listed explicitly. And while
`lmcache_native.so` links `libc10.so` and `libtorch_cpu.so`, it links nothing
CUDA, so a CPU-only torch satisfies it. That linkage is ABI-sensitive: the
`TORCH_VERSION` build arg must match the torch the wheel was compiled against,
or `import lmcache` fails with an undefined symbol. The build asserts this
rather than shipping a broken image.

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

Same run, stock vs dedup (2 servers, `L1_GB=0.05 SEQ=40`):

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

## Reporting on the Fleet

`report.sh` output (verified on 2 servers, L1_GB=0.05, SEQ=40, 512 tokens/req):

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

Four lanes, all on GitHub-hosted `ubuntu-24.04`. Three are cheap PR gates —
codespell against `.codespellrc`, shellcheck over `scripts/*.sh`, and hadolint
plus `docker compose config` on the Dockerfile and the *generated* compose file
(both the stock and `L2_DEDUP=1` variants, since dedup takes a different branch
in the generator).

The fourth, `lmcache-fleet - Smoke Test`, runs a real two-server fleet on a
GitHub runner in roughly two minutes:

```bash
L2_DEDUP=1 L1_GB=0.05 make run NUM=2 SEQ=40
```

Three things make that fit. The slim CPU-only image is **built** in-workflow
with a buildx `type=gha` cache rather than pulling the 14.8 GB published one —
about 40s on a warm cache, and it means the Dockerfile is continuously tested.
`L1_GB=0.05` makes 40 sequences overflow L1, so the run exercises the eviction
and L2 spill paths rather than just starting cleanly. And `L2_DEDUP=1` routes
L2 through the sim adapter, so the spill costs ~1 MB per server instead of
~81 MB — the runner's disk never becomes the constraint.

The lane then asserts on the key directory with `make check` and uploads a
screenshot of the live Grafana dashboard as a build artifact. That artifact is
the only evidence that the whole monitoring chain worked: Prometheus scraped
the exporter, the exporter read the coordinator, and the panels rendered it. A
green check cannot show that; the image can. On failure it uploads fleet logs
and a container listing instead.

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
L2_DEDUP=1 L1_GB=0.05 make run NUM=2 SEQ=6000
```

To force visible L2 spill on a small host: `L1_GB=0.05 SEQ=40`.

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
- The published `lmcache/standalone` image is ~14.8 GB and its `ENTRYPOINT` is
  `/opt/nvidia/nvidia_entrypoint.sh`: CUDA banners, GPU-driver probes, and a
  CPU-capability check that misfires on this AMD host and `sleep 2`s on every
  container start. Those stray `ERROR: ... AVX instruction set` lines in the
  logs come from it, not from LMCache. gen_compose sets `entrypoint: [""]` so
  nothing runs before `lmcache`; see the slim image above to drop the CUDA base
  entirely.
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

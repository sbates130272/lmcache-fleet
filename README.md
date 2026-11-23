# LMCache Fleet Demo (v0.5.8)

Programmable-size LMCache MP cluster: N cache servers (L1 DRAM + L2 fs adapter),
one coordinator building the fleet-wide key directory, and per-server one-shot
stimulators (`lmcache bench server --mode cpu`, no vLLM) that STORE synthetic KV
chunks. Eviction past the L1 watermark spills blocks to L2; the coordinator
directory tracks both placements.

## Usage

```bash
python3 gen_compose.py 4        # or: NUM_SERVERS=4 SEQ=20 L1_GB=8 python3 gen_compose.py
docker compose up -d            # coordinator + N servers (healthchecks gate stimulators)
docker compose --profile stim up -d $(seq 1 4 | sed 's/^/stim-/')   # make stores
./report.sh 4                   # directory: per-instance L1/L2 placements
```

## What you should see

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

## Tunables (env)

| var | default | meaning |
|---|---|---|
| NUM_SERVERS | 3 | fleet size |
| NUM_COORD | 1 | coordinators; must be 1 (v0.5.8 has no coordinator clustering) |
| RUN_UID / RUN_GID | your uid/gid | uid:gid every container runs as |
| COMPOSE_PROJECT_NAME | lmcache-fleet-demo | scopes the network and container names |
| SEQ | 6 | distinct synthetic sequences per stimulator |
| NUM_TOKENS | 512 | tokens per store request (multiple of CHUNK_SIZE) |
| CHUNK_SIZE | 256 | chunk size (must match across servers/coordinator) |
| L1_GB | 1 | per-server L1 DRAM tier size |
| SHAPE_SPEC | (2,64,16,4,64):float16:4 | bench KV shape; bigger → faster L1 fill |
| SHM_SIZE | 512m | /dev/shm per server + stimulator |
| EVICT_WATERMARK | 0.8 | L1 fraction that triggers L2 spill |

To force visible L2 spill on a small host: `L1_GB=0.05 SEQ=40`.

## Gotchas (learned the hard way)

- `lmcache bench server --mode cpu --transfer-mode lmcache_driven` uses POSIX
  SHM → the stimulator MUST share the server's IPC namespace
  (`ipc: service:lmcache-server-N`). Fallback: `--transfer-mode engine_driven`
  (inline ZMQ transport, slower but namespace-agnostic).
- The `(…)` in `--kvcache-shape-spec` breaks compose's command tokenizer —
  keep it double-quoted.
- Named volumes for the fs L2 adapter are root-owned → PermissionError from the
  non-root container uid. Use pre-`chmod 777` bind mounts (gen_compose does this).
  Services also pin `user: <your uid>:<your gid>` so the `.data` blobs come back
  owned by you; the image defaults to uid 1000 (`lmcache`), which only happens to
  be right when your own uid is 1000. A non-1000 uid has no `/etc/passwd` entry
  and lands on `HOME=/`, so the services set `HOME=/tmp`.
- Don't use `container_name:` here — it is node-global, and names like
  `coordinator` collide with unrelated stacks. Compose's project prefix
  (`lmcache-fleet-demo-*`) keeps the fleet to itself; service DNS names on the
  `fleet` network are unaffected.
- Bench clients allocate `n_kv*n_layers` tensors of NB*BS*H*D*2B each and
  migrate all of them to SHM — the old default shape (1024 blocks × 16 × 8 × 128
  × 32 layers) was ~2 GB per client and OOM-killed 3 servers on a 7 GB host.
- Coordinator directory inspection: `GET /instances`, `GET /directory/keys?limit=&offset=`;
  per-server L1/L2 contents: `GET :818x/cache/objects?tier=l1|l2`.

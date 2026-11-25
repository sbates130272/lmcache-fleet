# lmcache-l2-sim

An L2 adapter for LMCache MP that models a large L2 tier without spending the
disk. It wraps the stock `fs` (or `fs_native`) adapter and adds two things:

- **Dedup.** Writes run through the inner adapter unchanged. When a store
  completes, each file it wrote is replaced by a hardlink (or symlink) to one
  canonical file of the same byte size. Every key keeps its own filename and
  its true apparent size; the tier holds one block of real bytes per distinct
  block size.
- **Latency.** Store and load completions can be delayed by a fixed amount, a
  per-MiB amount, or both, with optional seeded jitter.

**This is for capacity modelling and diagnostics, not production.** An L2 read
returns the canonical block for the key's size, which is a structurally valid
block but not the KV that was stored under that key. Anything that checks KV
contents (for example `lmcache bench server --url ...` checksum verification)
will fail for data served from L2, and a model fed these blocks would produce
wrong output.

Developed against LMCache v0.5.8.

## Use it out of tree

No LMCache changes or image rebuild needed. Make the package importable by the
server, then select it through LMCache's built-in `plugin` adapter type.

```bash
pip install lmcache-l2-sim                  # into the environment LMCache runs in
pip install ./plugins/lmcache-l2-sim        # or from a clone, from the repo root
# or, without installing: mount this directory and set PYTHONPATH to it
```

```bash
lmcache server ... --l2-adapter '{
  "type": "plugin",
  "module_path": "lmcache_l2_sim.l2_sim_adapter",
  "class_name": "L2SimAdapter",
  "adapter_params": {
    "inner": {"type": "fs", "base_path": "/data/l2"},
    "read_delay_ms": 5
  }
}'
```

## Parameters

All go in `adapter_params`. Only `inner` is required.

| field | default | meaning |
| --- | --- | --- |
| `inner` | required | spec of the wrapped adapter, with its own `type` |
| `dedup` | `true` | collapse stored files onto one canonical file per byte size; `false` gives latency only |
| `link_mode` | `"hardlink"` | `"hardlink"` or `"symlink"` |
| `canonical_dir` | `<base_path>/.dedup` | where canonical files live |
| `write_delay_ms` | `0` | fixed delay added to each store completion |
| `write_delay_ms_per_mb` | `0` | store delay per MiB written |
| `read_delay_ms` | `0` | fixed delay added to each load completion |
| `read_delay_ms_per_mb` | `0` | load delay per MiB read |
| `delay_jitter` | `0` | fraction in [0, 1]; each delay is scaled by a uniform factor in [1 - jitter, 1 + jitter] |
| `seed` | `0` | seed for the jitter stream |

Notes:

- **Hardlinks** look exactly like real files: `ls -l` shows the true size and
  `du` counts the content once. **Symlinks** make the sharing visible, at the
  cost of `ls -l` showing a link. Symlink targets are relative, so they
  resolve the same from inside a container and from the host.
- Hardlinks need `canonical_dir` on the same filesystem as the inner
  `base_path`, which the default guarantees.
- Dedup is per byte size. A run with mixed block sizes keeps one canonical
  file per size.
- Dedup needs an inner adapter with a `base_path` (`fs` or `fs_native`). With
  `dedup: false` the adapter wraps any inner adapter for latency only.
- A delay is added to the completion LMCache observes; the underlying I/O runs
  at its own pace. Lookups are not delayed.
- Usage and eviction accounting are the inner adapter's, so LMCache sees
  logical bytes as if nothing were deduplicated.
- `GET /status` on the server reports the adapter's counters under `dedup`:
  files collapsed, bytes saved, collapse errors, completions currently held.

## L2 eviction

The adapter reports the inner adapter's capacity and usage, so LMCache's L2
eviction works through it. Usage is logical bytes, which means a capacity can
be modelled and filled under dedup while the disk holds almost nothing.

```bash
lmcache server ... --l2-adapter '{
  "type": "plugin",
  "module_path": "lmcache_l2_sim.l2_sim_adapter",
  "class_name": "L2SimAdapter",
  "adapter_params": {
    "inner": {"type": "fs_native", "base_path": "/data/l2", "max_capacity_gb": 100}
  },
  "eviction": {"eviction_policy": "LRU", "trigger_watermark": 0.8, "eviction_ratio": 0.2}
}'
```

Two things decide whether anything is evicted:

- **The inner adapter must declare a capacity.** `fs_native` does, through
  `max_capacity_gb`. Plain `fs` does not, and LMCache then logs `does not
  support global eviction` and skips the adapter.
- **The `eviction` block belongs on the outermost spec**, beside `"type":
  "plugin"`. LMCache builds its eviction controller from the spec
  `--l2-adapter` parses and never looks inside `inner`. A block only there
  parses cleanly and does nothing, with usage running past the capacity and no
  warning from LMCache; this adapter logs a note when it sees one.

`fs_native` warns that its capacity "will NOT bound disk usage" when its own
spec has no `eviction` block. Under this adapter that warning is a false alarm
if the outer block is present; repeating the block inside `inner` silences it
and is otherwise harmless.

## One directory shared by several servers

Several LMCache servers can point this adapter at one directory — a shared
filesystem standing in for a shared L2 — and a block one of them stored is then
a hit for the others. Needs 0.1.2 or later.

```bash
lmcache server ... --l2-adapter '{
  "type": "plugin",
  "module_path": "lmcache_l2_sim.l2_sim_adapter",
  "class_name": "L2SimAdapter",
  "adapter_params": {
    "inner": {"type": "fs", "base_path": "/mnt/shared-l2",
              "relative_tmp_dir": ".tmp-server-1", "shared": true}
  },
  "shared": true
}'
```

- **Give each server its own `relative_tmp_dir`.** This is LMCache's, not this
  adapter's: both fs adapters write `<key>.tmp` and rename it into place, so
  two servers storing the same block at once rename each other's file away and
  the loser logs a `FileNotFoundError`. The block is there either way, but the
  log is not quiet.
- **`"shared": true` goes on the outermost spec**, like `eviction`, and for the
  same reason. It tells the coordinator the domain is mounted by several
  servers, so it records one placement for a block instead of one per server.
- **The canonical directory is shared too**, which is what you want: one
  canonical file per block size for the whole directory. Before 0.1.2 the
  temporary links used while collapsing were named by a per-process counter,
  so two servers could pick the same name; they now carry a per-adapter token.
- **A capacity is counted per server.** Each server's `fs_native` counts the
  bytes it wrote, so `max_capacity_gb` bounds each server's contribution to
  the directory rather than the directory. A block one server evicts is a miss
  for the rest.

A server can also list two adapters, its own directory first and the shared one
second: LMCache stores every key to both and serves a lookup from the first
that holds it.

## Tests

The tests need LMCache importable, so they run inside an image that has it:

```bash
make test-plugins          # from the repo root
```

or directly:

```bash
docker run --rm -v "$PWD/plugins:/plugins:ro" -e HOME=/tmp \
  -e PYTHONPATH=/plugins/lmcache-l2-sim --entrypoint "" \
  lmcache-fleet-demo/lmcache:0.5.8-cpu \
  /opt/venv/bin/python -m pytest /plugins/lmcache-l2-sim/tests -q
```

They cover config parsing and rejection, the plugin loader's config-class
discovery, adapter-type registration, the delay arithmetic including jitter
bounds, and the collapse itself: hardlink and symlink modes, per-size
separation, idempotence, missing files, and the reported counters.

## Upstreaming

The module is written to drop into LMCache unchanged, modelled on the in-tree
`fault_inject` adapter: copy `lmcache_l2_sim/l2_sim_adapter.py` to
`lmcache/v1/distributed/l2_adapters/`. It already registers itself as adapter
type `dedup`, so in tree the spec is simply
`{"type": "l2_sim", "inner": {...}, ...}`. The in-tree change would also need
the module added to the factory's lazy-import list, and tests.

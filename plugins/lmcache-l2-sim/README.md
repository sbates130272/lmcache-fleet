# lmcache-l2-sim

An L2 adapter for LMCache MP that models a large, slow L2 tier without
spending the disk. It wraps the stock `fs` (or `fs_native`) adapter and adds
two things:

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
pip install ./plugins            # into the environment LMCache runs in
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
- A delay is added to the completion LMCache observes; it does not slow the
  underlying I/O. Lookups are not delayed.
- Usage and eviction accounting are the inner adapter's, so LMCache sees
  logical bytes as if nothing were deduplicated.
- `GET /status` on the server reports the adapter's counters under `dedup`:
  files collapsed, bytes saved, collapse errors, completions currently held.

## Tests

The tests need LMCache importable, so they run inside an image that has it:

```bash
make test-plugins          # from the repo root
```

or directly:

```bash
docker run --rm -v "$PWD/plugins:/plugins:ro" -e HOME=/tmp \
  -e PYTHONPATH=/plugins/lmcache-l2-sim --entrypoint "" \
  lmcache/standalone:v0.5.8 \
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

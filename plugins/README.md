# LMCache plugins

Out-of-tree LMCache extensions, one directory per publishable distribution.
Each is independent: its own `pyproject.toml`, its own version, installable on
its own.

| directory | distribution | what it does |
| --- | --- | --- |
| [lmcache-l2-sim](lmcache-l2-sim/) | `lmcache-l2-sim` | Simulates a large, slow L2 tier: collapses stored files onto one canonical file per size, and injects configurable store/load latency |

## Using them in this demo

`scripts/gen_compose.py` mounts this whole directory at `/opt/lmcache-plugins`
and builds `PYTHONPATH` from every subdirectory that has a `pyproject.toml`, so
a new plugin needs no compose changes — drop the directory in and regenerate.

Plugins are selected per adapter through LMCache's built-in `plugin` type, which
resolves by dotted module path:

```json
{"type": "plugin",
 "module_path": "lmcache_l2_sim.l2_sim_adapter",
 "class_name": "L2SimAdapter",
 "adapter_params": {"inner": {"type": "fs", "base_path": "/data/l2"}}}
```

## Adding a plugin

```
plugins/
  <distribution-name>/        e.g. lmcache-slow-l2
    pyproject.toml            name = "lmcache-slow-l2", packages = ["lmcache_slowfs"]
    README.md                 what it does, parameters, caveats
    <import_package>/         e.g. lmcache_slowfs/ -- must be unique across plugins,
      __init__.py             since module_path resolves by dotted name
      ...
```

Keep the import package name distinct from the distribution name's hyphens
(`lmcache-l2-sim` ships `lmcache_l2_sim`), and distinct from every other
plugin here — two plugins exporting the same module name would shadow each
other on `PYTHONPATH`.

## Publishing

Each directory builds and uploads on its own:

```bash
cd plugins/lmcache-l2-sim
rm -rf dist                      # stale artifacts would be uploaded too
python3 -m build
twine check dist/*
twine upload --repository testpypi dist/*    # rehearse first
twine upload dist/*
```

A name can only be used once on PyPI, so check `twine check` passes and the
TestPyPI upload installs before claiming the real name.

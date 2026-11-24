# LMCache plugins

Out-of-tree LMCache extensions, one directory per publishable distribution.
Each is independent: its own `pyproject.toml`, its own version, installable on
its own.

| directory | distribution | what it does |
| --- | --- | --- |
| [lmcache-dedup-l2](lmcache-dedup-l2/) | `lmcache-dedup-l2` | L2 adapter that dedups stored files onto one canonical file per size, and can add configurable store/load latency |

## Using them in this demo

`scripts/gen_compose.py` mounts this whole directory at `/opt/lmcache-plugins`
and builds `PYTHONPATH` from every subdirectory that has a `pyproject.toml`, so
a new plugin needs no compose changes — drop the directory in and regenerate.

Plugins are selected per adapter through LMCache's built-in `plugin` type, which
resolves by dotted module path:

```json
{"type": "plugin",
 "module_path": "lmcache_dedup.dedup_l2_adapter",
 "class_name": "DedupL2Adapter",
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
(`lmcache-dedup-l2` ships `lmcache_dedup`), and distinct from every other
plugin here — two plugins exporting the same module name would shadow each
other on `PYTHONPATH`.

## Publishing

Each directory builds and uploads on its own:

```bash
cd plugins/lmcache-dedup-l2
python -m build
twine upload dist/*
```

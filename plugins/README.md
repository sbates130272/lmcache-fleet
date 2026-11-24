# LMCache plugins

Out-of-tree LMCache extensions, one directory per publishable distribution.
Each is independent: its own `pyproject.toml`, its own version, installable on
its own.

| directory | distribution | what it does |
| --- | --- | --- |
| [lmcache-l2-sim](lmcache-l2-sim/) | `lmcache-l2-sim` | Simulates a large L2 tier: collapses stored files onto one canonical file per size, and injects configurable store/load latency |

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
  <distribution-name>/        e.g. lmcache-l2-trace
    pyproject.toml            name = "lmcache-l2-trace", packages = ["lmcache_l2_trace"]
    README.md                 what it does, parameters, caveats
    <import_package>/         e.g. lmcache_l2_trace/ -- must be unique across plugins,
      __init__.py             since module_path resolves by dotted name
      ...
```

Keep the import package name distinct from the distribution name's hyphens
(`lmcache-l2-sim` ships `lmcache_l2_sim`), and distinct from every other
plugin here — two plugins exporting the same module name would shadow each
other on `PYTHONPATH`.

## Publishing

Each directory is its own distribution, built and uploaded on its own. From the
repo root:

```bash
make dist                                  # build + twine check, every plugin
make dist PLUGIN=lmcache-l2-sim            # just one
make publish-precheck                      # dist, then install-and-import each wheel
make publish CONFIRM=1                     # upload to PyPI
```

`dist` removes stale `dist/` and `*.egg-info` first — artifacts from an earlier
build, or from an earlier package name, would otherwise be uploaded too. The
targets need `build` and `twine` (`pip install --user build twine`), plus `uv`
for the precheck, and discover plugins by wildcard, so a new directory with a
`pyproject.toml` is picked up with no Makefile change.

`publish-precheck` installs each freshly built wheel into a throwaway `uv` venv
and imports its top-level packages. A wheel can build and pass `twine check`
and still be unimportable — a wrong `packages =`, a module left out of the
distribution — and that is only discoverable from an installed copy. It imports
the top level only: the adapter module needs LMCache, which `make test-plugins`
covers inside the image.

`publish` runs `publish-precheck` first and then demands `CONFIRM=1`, because
the upload cannot be taken back: a version on PyPI can be yanked but never
replaced, and a project name is claimed once.

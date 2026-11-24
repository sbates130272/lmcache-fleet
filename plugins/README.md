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
targets need `build` and `twine`, plus `uv` for the precheck, and discover
plugins by wildcard, so a new directory with a `pyproject.toml` is picked up
with no Makefile change.

`build` has to be *importable* by the `python3` that runs `python3 -m build`,
not merely on `PATH`, so a venv is the portable way to get these:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install build twine
```

pipx will not do for `build` — it isolates the module and exposes only the
`pyproject-build` entry point. Nor will `pip install --user` on a stock Debian
or Ubuntu, whose `python3` is externally managed and refuses it.

`publish-precheck` installs each freshly built wheel into a throwaway `uv` venv
and imports its top-level packages. A wheel can build and pass `twine check`
and still be unimportable — a wrong `packages =`, a module left out of the
distribution — and that is only discoverable from an installed copy. It imports
the top level only: the adapter module needs LMCache, which `make test-plugins`
covers inside the image.

`publish` runs `publish-precheck` first and then demands `CONFIRM=1`, because
the upload cannot be taken back: a version on PyPI can be yanked but never
replaced, and a project name is claimed once.

CI covers all of this on every PR. The `lmcache-fleet - Package Check` lane
builds and prechecks the distributions, asserts `publish` refuses without
`CONFIRM=1`, and then runs the real upload against a stub index on loopback —
so a malformed `twine upload` surfaces there rather than on the one attempt
that counts.

## After publishing

```bash
make test-released                     # latest on PyPI
make test-released VERSION=0.1.0       # a specific release
```

This installs each distribution from PyPI into the lmcache image and runs the
repo's own test suite against *that* copy. `make test-plugins` tests the
working tree, which cannot tell you whether the published artifact is whole —
a module left out of the wheel, or a release cut from different source. Only
`tests/` is mounted into the container, so the working-tree package is not
present to shadow the installed one.

The `lmcache-fleet - Released Check` lane runs it weekly and on every push to
main that touches `plugins/`. The schedule is the point: this is the one lane
whose result can change with no commit behind it, when a release is yanked or
something underneath it moves.

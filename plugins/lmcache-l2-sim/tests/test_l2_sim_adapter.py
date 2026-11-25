# SPDX-License-Identifier: Apache-2.0
"""Tests for the dedup + latency L2 adapter.

Needs LMCache importable, so run them inside the demo's image:

    make test-plugins

or directly:

    docker run --rm -v "$PWD/plugins:/plugins:ro" -e HOME=/tmp \\
      -e PYTHONPATH=/plugins/lmcache-l2-sim --entrypoint "" \\
      lmcache-fleet-demo/lmcache:0.5.8-cpu \\
      /opt/venv/bin/python -m pytest /plugins/lmcache-l2-sim/tests -q
"""

# Standard
import os
import stat

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.key_codec import encode_fs_filename
from lmcache_l2_sim.l2_sim_adapter import (
    L2SimAdapter,
    L2SimAdapterConfig,
)

BLOCK = b"\xab" * 4096
# An inner with no base_path, spelled out fully so it is our dedup check that
# rejects it rather than the mock adapter's own config validation.
MOCK_INNER_SPEC = {"inner": {"type": "mock", "max_size_gb": 1, "mock_bandwidth_gb": 1}}


def fs_spec(base):
    return {"type": "fs", "base_path": str(base)}


def make_key(index, model="test-model"):
    """An ObjectKey whose fs filename is stable for a given index."""
    return ObjectKey(
        chunk_hash=bytes([index % 256]) * 32,
        model_name=model,
        kv_rank=0,
        object_group_id=0,
        cache_salt="",
    )


def write_block(base, key, payload=BLOCK):
    """Put a file where the fs adapter would have written it."""
    path = base / encode_fs_filename(key)
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------- config ----


def test_config_parses_every_field(tmp_path):
    config = L2SimAdapterConfig.from_dict({
        "inner": fs_spec(tmp_path),
        "dedup": True,
        "link_mode": "symlink",
        "canonical_dir": str(tmp_path / "canon"),
        "write_delay_ms": 10,
        "write_delay_ms_per_mb": 2.5,
        "read_delay_ms": 7,
        "read_delay_ms_per_mb": 1,
        "delay_jitter": 0.25,
        "seed": 42,
    })
    assert config.link_mode == "symlink"
    assert config.canonical_dir == str(tmp_path / "canon")
    assert (config.write_delay_ms, config.write_delay_ms_per_mb) == (10.0, 2.5)
    assert (config.read_delay_ms, config.read_delay_ms_per_mb) == (7.0, 1.0)
    assert config.delay_jitter == 0.25
    assert config.seed == 42
    assert type(config.inner_config).__name__ == "FSL2AdapterConfig"


def test_eviction_inside_inner_is_called_out(tmp_path, monkeypatch):
    # First Party
    from lmcache_l2_sim import l2_sim_adapter

    notes = []
    monkeypatch.setattr(l2_sim_adapter.logger, "info", lambda msg, *a: notes.append(msg))
    eviction = {"eviction_policy": "LRU"}

    L2SimAdapterConfig.from_dict({"inner": fs_spec(tmp_path)})
    assert notes == []

    config = L2SimAdapterConfig.from_dict({"inner": {**fs_spec(tmp_path), "eviction": eviction}})
    assert len(notes) == 1 and "outermost" in notes[0]
    # Still parsed, so an inner adapter that wants it for itself gets it.
    assert config.inner_config.eviction_config.eviction_policy == "LRU"


def test_capacity_and_eviction_come_from_the_inner(tmp_path):
    """What L2 eviction needs from this adapter: the inner's declared capacity."""
    capped = {"type": "fs_native", "base_path": str(tmp_path), "max_capacity_gb": 0.5}
    adapter = L2SimAdapter(L2SimAdapterConfig.from_dict({"inner": capped}))
    try:
        assert adapter.supports_global_eviction is True
        assert adapter.get_usage().total_capacity_bytes == int(0.5 * 1024**3)
    finally:
        adapter.close()

    uncapped = L2SimAdapter(L2SimAdapterConfig.from_dict({"inner": fs_spec(tmp_path)}))
    try:
        # Plain fs declares no capacity, so LMCache skips it for eviction.
        assert uncapped.supports_global_eviction is False
    finally:
        uncapped.close()


def test_config_defaults(tmp_path):
    config = L2SimAdapterConfig.from_dict({"inner": fs_spec(tmp_path)})
    assert config.dedup is True
    assert config.link_mode == "hardlink"
    assert config.canonical_dir is None
    assert config.write_delay_ms == config.read_delay_ms == 0.0
    assert config.delay_jitter == 0.0


@pytest.mark.parametrize("spec, message", [
    ({}, "inner"),
    ({"inner": "fs"}, "inner"),
    ({"inner": {"base_path": "/x"}}, "type"),
    ({"inner": {"type": "fs", "base_path": "/x"}, "link_mode": "copy"}, "link_mode"),
    ({"inner": {"type": "fs", "base_path": "/x"}, "read_delay_ms": -1}, "read_delay_ms"),
    ({"inner": {"type": "fs", "base_path": "/x"}, "write_delay_ms_per_mb": -0.1},
     "write_delay_ms_per_mb"),
    ({"inner": {"type": "fs", "base_path": "/x"}, "delay_jitter": 1.5}, "delay_jitter"),
    ({"inner": {"type": "fs", "base_path": "/x"}, "seed": "nope"}, "seed"),
    ({"inner": {"type": "fs", "base_path": "/x"}, "dedup": "yes"}, "dedup"),
    ({"inner": {"type": "fs", "base_path": "/x"}, "canonical_dir": ""}, "canonical_dir"),
])
def test_config_rejects_bad_input(spec, message):
    with pytest.raises(ValueError, match=message):
        L2SimAdapterConfig.from_dict(spec)


def test_help_lists_the_fields():
    text = L2SimAdapterConfig.help()
    for field in ("inner", "dedup", "link_mode", "canonical_dir",
                  "write_delay_ms", "read_delay_ms", "delay_jitter", "seed"):
        assert field in text


def test_plugin_loader_finds_the_config_class():
    """The `plugin` adapter resolves <class_name> + 'Config' by convention."""
    # First Party
    from lmcache.v1.distributed.l2_adapters.plugin_l2_adapter import (
        PluginL2AdapterConfig,
        _resolve_config_class,
    )
    # Standard
    import importlib

    module = importlib.import_module("lmcache_l2_sim.l2_sim_adapter")
    plugin_config = PluginL2AdapterConfig.from_dict({
        "module_path": "lmcache_l2_sim.l2_sim_adapter",
        "class_name": "L2SimAdapter",
        "adapter_params": {"inner": {"type": "fs", "base_path": "/x"}},
    })
    resolved = _resolve_config_class(module, plugin_config, L2SimAdapter)
    assert resolved is L2SimAdapterConfig


def test_registered_as_an_adapter_type():
    """Importing the module registers `l2_sim`, which is what an in-tree copy needs."""
    # First Party
    from lmcache.v1.distributed.l2_adapters.factory import get_all_registered_names

    assert "l2_sim" in get_all_registered_names()


# ----------------------------------------------------------------- delay ----


@pytest.fixture
def adapter(tmp_path, request):
    """A built adapter over an fs inner, torn down after the test."""
    params = {"inner": fs_spec(tmp_path)}
    params.update(getattr(request, "param", {}))
    built = L2SimAdapter(L2SimAdapterConfig.from_dict(params))
    yield built
    built.close()


def test_delay_is_zero_when_unconfigured(adapter):
    assert adapter._delay_seconds(0.0, 0.0, 10 * 1024 * 1024) == 0.0


def test_delay_combines_fixed_and_per_mb(adapter):
    # 100ms fixed + 10ms per MiB over 2 MiB = 120ms
    assert adapter._delay_seconds(100.0, 10.0, 2 * 1024 * 1024) == pytest.approx(0.120)


@pytest.mark.parametrize("adapter", [{"delay_jitter": 0.5, "seed": 7}], indirect=True)
def test_jitter_stays_within_bounds(adapter):
    seen = {adapter._delay_seconds(100.0, 0.0, 0) for _ in range(200)}
    assert all(0.05 <= value <= 0.15 for value in seen)
    assert len(seen) > 1, "jitter should vary between calls"


# ----------------------------------------------------------------- dedup ----


def test_collapse_hardlinks_same_size_onto_one_inode(adapter, tmp_path):
    keys = [make_key(i) for i in range(5)]
    paths = [write_block(tmp_path, key) for key in keys]
    assert len({path.stat().st_ino for path in paths}) == 5, "distinct files to start"

    adapter._collapse(keys)

    inodes = {path.stat().st_ino for path in paths}
    assert len(inodes) == 1, "every file should share one inode"
    for path in paths:
        assert path.stat().st_size == len(BLOCK), "apparent size is preserved"
        assert stat.S_ISREG(os.lstat(path).st_mode), "hardlinks stay regular files"
    canonical = list((tmp_path / ".dedup").iterdir())
    assert [p.name for p in canonical] == [f"{len(BLOCK)}.canon"]


def test_collapse_keeps_content_readable(adapter, tmp_path):
    keys = [make_key(i) for i in range(3)]
    paths = [write_block(tmp_path, key) for key in keys]
    adapter._collapse(keys)
    for path in paths:
        assert path.read_bytes() == BLOCK, "the first payload becomes the shared one"


@pytest.mark.parametrize("adapter", [{"link_mode": "symlink"}], indirect=True)
def test_collapse_symlink_mode(adapter, tmp_path):
    keys = [make_key(i) for i in range(3)]
    paths = [write_block(tmp_path, key) for key in keys]
    adapter._collapse(keys)

    # Every key becomes a symlink, including the one whose bytes were adopted
    # as the canonical content -- so the directory is uniform.
    assert all(stat.S_ISLNK(os.lstat(p).st_mode) for p in paths)
    for link in paths:
        target = os.readlink(link)
        assert not os.path.isabs(target), "relative, so it resolves on host and in container"
        assert link.read_bytes() == BLOCK
    canonical = tmp_path / ".dedup" / f"{len(BLOCK)}.canon"
    assert stat.S_ISREG(os.lstat(canonical).st_mode), "the canonical file holds the bytes"


def test_collapse_separates_by_size(adapter, tmp_path):
    small, large = make_key(1), make_key(2)
    write_block(tmp_path, small, b"x" * 512)
    write_block(tmp_path, large, b"y" * 1024)
    adapter._collapse([small, large])
    names = sorted(p.name for p in (tmp_path / ".dedup").iterdir())
    assert names == ["1024.canon", "512.canon"], "one canonical file per distinct size"


def test_collapse_is_idempotent(adapter, tmp_path):
    keys = [make_key(i) for i in range(3)]
    paths = [write_block(tmp_path, key) for key in keys]
    adapter._collapse(keys)
    first = {path.stat().st_ino for path in paths}
    adapter._collapse(keys)
    assert {path.stat().st_ino for path in paths} == first
    assert adapter._collapse_errors == 0


def test_two_adapters_can_share_one_directory(tmp_path):
    """Servers mounting one shared L2 dedup into the same canonical directory."""
    spec = {"inner": fs_spec(tmp_path)}
    first = L2SimAdapter(L2SimAdapterConfig.from_dict(spec))
    second = L2SimAdapter(L2SimAdapterConfig.from_dict(spec))
    try:
        assert first._tmp_token != second._tmp_token
        mine, theirs = [make_key(i) for i in range(4)], [make_key(i) for i in range(4, 8)]
        paths = [write_block(tmp_path, key) for key in mine + theirs]
        first._collapse(mine)
        second._collapse(theirs)
        assert len({os.stat(p).st_ino for p in paths}) == 1
        assert first._collapse_errors == second._collapse_errors == 0
        assert not list((tmp_path / ".dedup").glob("*.tmp"))
    finally:
        first.close()
        second.close()


def test_collapse_skips_missing_files(adapter, tmp_path):
    """A key evicted between the store completing and the collapse is not an error."""
    present, absent = make_key(1), make_key(2)
    write_block(tmp_path, present)
    adapter._collapse([present, absent])
    assert adapter._collapse_errors == 0
    assert adapter._files_collapsed >= 0


def test_collapse_counters_and_status(adapter, tmp_path):
    keys = [make_key(i) for i in range(4)]
    for key in keys:
        write_block(tmp_path, key)
    adapter._collapse(keys)

    status = adapter.report_status()["dedup"]
    assert status["enabled"] is True
    assert status["link_mode"] == "hardlink"
    assert status["canonical_files_created"] == 1
    assert status["collapse_errors"] == 0
    # Three of the four files had their own bytes released.
    assert status["bytes_saved"] == 3 * len(BLOCK)


@pytest.mark.parametrize("adapter", [{"dedup": False}], indirect=True)
def test_dedup_disabled_leaves_files_alone(adapter, tmp_path):
    keys = [make_key(i) for i in range(3)]
    paths = [write_block(tmp_path, key) for key in keys]
    assert adapter._dedup is False
    assert not (tmp_path / ".dedup").exists()
    assert len({path.stat().st_ino for path in paths}) == 3


def test_dedup_requires_an_inner_with_base_path():
    with pytest.raises(ValueError, match="base_path"):
        L2SimAdapterConfig.from_dict(MOCK_INNER_SPEC)


def test_latency_only_works_over_any_inner():
    config = L2SimAdapterConfig.from_dict({
        "inner": dict(MOCK_INNER_SPEC["inner"]),
        "dedup": False,
        "read_delay_ms": 5,
    })
    assert config.dedup is False
    assert config.read_delay_ms == 5.0


# -------------------------------------------------------------- plumbing ----


def test_event_fds_are_our_own_not_the_inners(adapter):
    """Delayed completions are released by us, so the controllers must watch our fds."""
    assert adapter.get_store_event_fd() == adapter._store_efd.fileno()
    assert adapter.get_load_event_fd() == adapter._load_efd.fileno()
    # Lookup is never delayed, so it passes straight through.
    assert adapter.get_lookup_and_lock_event_fd() == \
        adapter._inner.get_lookup_and_lock_event_fd()


def test_usage_and_tags_delegate_to_the_inner(adapter):
    assert adapter.get_usage() == adapter._inner.get_usage()
    assert adapter.get_memory_tags() == adapter._inner.get_memory_tags()
    assert adapter.supports_global_eviction == adapter._inner.supports_global_eviction


def test_status_includes_the_inners_status(adapter):
    status = adapter.report_status()
    assert "dedup" in status
    assert "base_path" in status, "inner status is preserved alongside ours"

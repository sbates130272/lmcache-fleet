# SPDX-License-Identifier: Apache-2.0
"""
Dedup + latency L2 adapter: model a large, slow L2 tier without the disk.

Wraps a real inner L2 adapter (``fs`` or ``fs_native``) and changes two things
about it, both for capacity modelling and diagnostics, never for production:

- **Dedup.** The inner adapter's write path runs unchanged. Once a store task
  completes, each file it wrote is replaced by a hardlink (or symlink) to one
  canonical file of the same byte size. Every key keeps its own unique
  filename and its true apparent size, but the tier holds one block's worth of
  bytes per distinct block size. The filesystem adapters store raw tensor
  bytes with no per-key header, so the canonical content loads back as a
  structurally valid block for any key of that size. Its *contents* are wrong
  for every key but the first, which is the trade this adapter exists to make.
- **Latency.** Store and load completions are withheld for a configurable
  delay (fixed, per-MB, with optional seeded jitter) to model a slower medium.
  Nothing blocks: the adapter owns its event fds and signals them when a held
  completion comes due.

It can be used two ways without changing this file:

- in tree, as ``{"type": "dedup", "inner": {...}, ...}``;
- out of tree, through the plugin loader, as ``{"type": "plugin",
  "module_path": "lmcache_dedup.dedup_l2_adapter", "class_name":
  "DedupL2Adapter", "adapter_params": {"inner": {...}, ...}}``.
"""

# Future
from __future__ import annotations

# Standard
from pathlib import Path
from typing import TYPE_CHECKING, Optional
import os
import random
import select
import stat
import threading
import time

if TYPE_CHECKING:
    # First Party
    from lmcache.lmcache_native import Bitmap
    from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
    from lmcache.v1.distributed.internal_api import (
        L1MemoryDesc,
        L2AdapterListener,
        L2StoreResult,
    )
    from lmcache.v1.memory_management import MemoryObj

# First Party
from lmcache.logging import init_logger
from lmcache.v1.distributed.api import MemoryTags
from lmcache.v1.distributed.key_codec import encode_fs_filename
from lmcache.v1.distributed.l2_adapters.base import (
    AdapterUsage,
    L2AdapterInterface,
    L2TaskId,
)
from lmcache.v1.distributed.l2_adapters.config import (
    L2AdapterConfigBase,
    register_l2_adapter_type,
)
from lmcache.v1.distributed.l2_adapters.factory import (
    register_l2_adapter_factory,
)
from lmcache.v1.platform import create_event_notifier
from lmcache.v1.platform.event_notifier import consume_fd

logger = init_logger(__name__)

_LINK_MODES = ("hardlink", "symlink")
_DEFAULT_CANONICAL_SUBDIR = ".dedup"
# Upper bound on how long the pump sleeps with nothing held, so a missed wakeup
# costs a bounded delay rather than a hang.
_PUMP_IDLE_TIMEOUT_MS = 200
_MB = 1024 * 1024


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------


def _non_negative_number(d: dict, name: str) -> float:
    """Return ``d[name]`` as a non-negative float, defaulting to 0."""
    value = d.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return float(value)


class DedupL2AdapterConfig(L2AdapterConfigBase):
    """
    Config for the dedup + latency L2 adapter.

    JSON fields:
    - inner (dict, required): full adapter spec for the wrapped adapter, with
      its own "type" (e.g. {"type": "fs", "base_path": "/data/l2"}).
    - dedup (bool): collapse stored files onto one canonical file per byte
      size. Default true. Needs an inner adapter with a ``base_path`` (``fs``
      or ``fs_native``); set false to use the adapter for latency only.
    - link_mode (str): "hardlink" (default) or "symlink". Hardlinks are
      indistinguishable from real files and keep the apparent size; symlinks
      make the sharing visible in ``ls -l``.
    - canonical_dir (str): where canonical files live. Default
      ``<inner base_path>/.dedup``. Must be on the same filesystem as the
      inner ``base_path`` for hardlinks.
    - write_delay_ms / read_delay_ms (float): fixed delay added to every store
      / load completion. Default 0.
    - write_delay_ms_per_mb / read_delay_ms_per_mb (float): delay added per MiB
      transferred, to model bandwidth rather than latency. Default 0.
    - delay_jitter (float): fraction in [0, 1]; each delay is scaled by a
      uniform factor in [1 - jitter, 1 + jitter]. Default 0.
    - seed (int): seed for the jitter stream, so runs reproduce. Default 0.
    """

    def __init__(
        self,
        inner_config: L2AdapterConfigBase,
        dedup: bool = True,
        link_mode: str = "hardlink",
        canonical_dir: Optional[str] = None,
        write_delay_ms: float = 0.0,
        write_delay_ms_per_mb: float = 0.0,
        read_delay_ms: float = 0.0,
        read_delay_ms_per_mb: float = 0.0,
        delay_jitter: float = 0.0,
        seed: int = 0,
    ) -> None:
        self.inner_config = inner_config
        self.dedup = dedup
        self.link_mode = link_mode
        self.canonical_dir = canonical_dir
        self.write_delay_ms = write_delay_ms
        self.write_delay_ms_per_mb = write_delay_ms_per_mb
        self.read_delay_ms = read_delay_ms
        self.read_delay_ms_per_mb = read_delay_ms_per_mb
        self.delay_jitter = delay_jitter
        self.seed = seed

    @classmethod
    def from_dict(cls, d: dict) -> "DedupL2AdapterConfig":
        """Parse a dedup adapter spec, building the inner config too.

        The ``"inner"`` sub-dict is dispatched through the L2 adapter config
        registry (lazy-importing its module if needed), mirroring
        ``parse_args_to_l2_adapters_config`` so the inner adapter's own
        shared / eviction / persist / serde keys are honored.

        Args:
            d: The adapter spec dict (see the class docstring for fields).

        Returns:
            A populated ``DedupL2AdapterConfig``.

        Raises:
            ValueError: If ``inner`` is missing or malformed, the inner type
                is unknown, or any other field fails validation.
        """
        inner = d.get("inner")
        if not isinstance(inner, dict):
            raise ValueError("'inner' must be an adapter spec dict with a 'type' field")
        inner_type = inner.get("type")
        if not isinstance(inner_type, str):
            raise ValueError("'inner' adapter spec must include a string 'type'")

        # First Party
        from lmcache.v1.distributed.l2_adapters.config import (  # noqa: PLC0415
            get_l2_adapter_config_class,
        )

        inner_cls = get_l2_adapter_config_class(inner_type)
        inner_config = inner_cls.from_dict(inner)
        inner_config.shared = bool(inner.get("shared", False))
        inner_config.eviction_config = cls._parse_eviction_config(inner)
        inner_config.persist_config = cls._parse_persist_config(inner)
        inner_config.serde_config = cls._parse_serde_config(inner)

        dedup = d.get("dedup", True)
        if not isinstance(dedup, bool):
            raise ValueError("dedup must be a boolean")

        link_mode = d.get("link_mode", "hardlink")
        if link_mode not in _LINK_MODES:
            raise ValueError(f"link_mode must be one of {list(_LINK_MODES)}")

        canonical_dir = d.get("canonical_dir")
        if canonical_dir is not None and (
            not isinstance(canonical_dir, str) or not canonical_dir
        ):
            raise ValueError("canonical_dir must be a non-empty string")

        if dedup and not isinstance(getattr(inner_config, "base_path", None), str):
            raise ValueError(
                "dedup needs an inner adapter with a 'base_path' (fs or "
                f"fs_native); '{inner_type}' has none. Set dedup=false to use "
                "this adapter for latency only."
            )

        jitter = _non_negative_number(d, "delay_jitter")
        if jitter > 1.0:
            raise ValueError("delay_jitter must be in [0, 1]")

        seed = d.get("seed", 0)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")

        return cls(
            inner_config=inner_config,
            dedup=dedup,
            link_mode=link_mode,
            canonical_dir=canonical_dir,
            write_delay_ms=_non_negative_number(d, "write_delay_ms"),
            write_delay_ms_per_mb=_non_negative_number(d, "write_delay_ms_per_mb"),
            read_delay_ms=_non_negative_number(d, "read_delay_ms"),
            read_delay_ms_per_mb=_non_negative_number(d, "read_delay_ms_per_mb"),
            delay_jitter=jitter,
            seed=seed,
        )

    @classmethod
    def help(cls) -> str:
        """Return a human-readable description of the config fields."""
        return (
            "Dedup + latency L2 adapter config fields (modelling/diagnostic "
            "use only):\n"
            "- inner (dict, required): wrapped adapter spec, e.g. "
            '{"type": "fs", "base_path": "/data/l2"}\n'
            "- dedup (bool): collapse stored files onto one canonical file "
            "per byte size (default true)\n"
            '- link_mode (str): "hardlink" (default) or "symlink"\n'
            "- canonical_dir (str): canonical file directory "
            "(default <base_path>/.dedup)\n"
            "- write_delay_ms, read_delay_ms (float): fixed delay per "
            "store / load (default 0)\n"
            "- write_delay_ms_per_mb, read_delay_ms_per_mb (float): delay "
            "per MiB transferred (default 0)\n"
            "- delay_jitter (float): fractional jitter in [0, 1] (default 0)\n"
            "- seed (int): jitter seed (default 0)\n"
            'Example: {"type": "dedup", "inner": {"type": "fs", '
            '"base_path": "/data/l2"}, "read_delay_ms": 5}'
        )

    def get_wanted_l1_memory_tags(self) -> tuple[MemoryTags, ...]:
        """Return the wrapped adapter's wanted L1 memory tags."""
        return self.inner_config.get_wanted_l1_memory_tags()


# -----------------------------------------------------------------------------
# Adapter
# -----------------------------------------------------------------------------


class DedupL2Adapter(L2AdapterInterface):
    """Decorator over an inner L2 adapter that dedups its files and adds latency.

    Lookup, unlock, delete, listing, usage and the listener funnel delegate to
    ``inner`` untouched. Store and load are submitted to ``inner`` unchanged,
    but their completions are collected by a pump thread, which collapses the
    files a store wrote and then releases each completion after its delay.
    """

    # Lets the plugin loader find the config class by attribute as well as by
    # the ``<class_name>Config`` naming convention.
    config_class_name = "DedupL2AdapterConfig"

    def __init__(
        self,
        config: DedupL2AdapterConfig,
        l1_memory_desc: "Optional[L1MemoryDesc]" = None,
    ) -> None:
        """Build the inner adapter from ``config`` and start the pump.

        Takes the config rather than a prebuilt inner adapter so that the same
        constructor serves the in-tree factory and the plugin loader, which
        calls ``adapter_cls(config, l1_memory_desc=...)``.

        Args:
            config: The parsed adapter config, including the inner spec.
            l1_memory_desc: Optional L1 memory descriptor forwarded to the
                inner adapter's factory.

        Raises:
            OSError: If the canonical directory cannot be created.
        """
        # First Party
        from lmcache.v1.distributed.l2_adapters.factory import (  # noqa: PLC0415
            create_l2_adapter_from_registry,
        )

        # Usage and eviction are delegated to the inner adapter, so the base
        # class's byte accounting (capacity 0) is intentionally unused here.
        super().__init__(max_capacity_bytes=0)
        self._inner = create_l2_adapter_from_registry(
            config.inner_config, l1_memory_desc
        )
        self._config = config

        self._dedup = config.dedup
        self._link_mode = config.link_mode
        self._base_path: Optional[Path] = None
        self._canonical_dir: Optional[Path] = None
        if self._dedup:
            self._base_path = Path(config.inner_config.base_path)  # type: ignore[attr-defined]
            self._canonical_dir = Path(
                config.canonical_dir
                or self._base_path / _DEFAULT_CANONICAL_SUBDIR
            )
            self._canonical_dir.mkdir(parents=True, exist_ok=True)

        self._rng = random.Random(config.seed)
        self._lock = threading.Lock()
        # task_id -> keys of an in-flight store, for the collapse.
        self._store_keys: dict[L2TaskId, list[ObjectKey]] = {}
        # task_id -> bytes an in-flight load will transfer, for the delay.
        self._load_bytes: dict[L2TaskId, int] = {}
        # Completions the inner adapter has produced but whose delay is still
        # running: (release_time, task_id, result).
        self._held_stores: list[tuple[float, L2TaskId, L2StoreResult]] = []
        self._held_loads: list[tuple[float, L2TaskId, Bitmap]] = []
        # Completions whose delay has passed, waiting for the controller.
        self._ready_stores: dict[L2TaskId, L2StoreResult] = {}
        self._ready_loads: dict[L2TaskId, Bitmap] = {}

        self._files_collapsed = 0
        self._bytes_saved = 0
        self._canonical_files = 0
        self._collapse_errors = 0
        self._tmp_counter = 0

        # The controllers poll OUR fds; the pump polls the inner adapter's.
        self._store_efd = create_event_notifier()
        self._load_efd = create_event_notifier()
        self._wake_efd = create_event_notifier()
        self._stop = threading.Event()
        self._pump = threading.Thread(
            target=self._pump_loop, name="dedup-l2-pump", daemon=True
        )
        self._pump.start()

        logger.warning(
            "DedupL2Adapter ACTIVE (dedup=%s link_mode=%s canonical_dir=%s "
            "write_delay_ms=%g+%g/MiB read_delay_ms=%g+%g/MiB jitter=%g seed=%d) "
            "wrapping %s -- L2 contents are NOT the stored KV; modelling/"
            "diagnostic use only.",
            self._dedup,
            self._link_mode,
            self._canonical_dir,
            config.write_delay_ms,
            config.write_delay_ms_per_mb,
            config.read_delay_ms,
            config.read_delay_ms_per_mb,
            config.delay_jitter,
            config.seed,
            type(self._inner).__name__,
        )

    # -- delay ----------------------------------------------------------------

    def _delay_seconds(self, fixed_ms: float, per_mb_ms: float, nbytes: int) -> float:
        """Return the delay for one task, with jitter applied.

        Caller must hold ``self._lock`` (the jitter stream is shared).
        """
        delay_ms = fixed_ms + per_mb_ms * (nbytes / _MB)
        if delay_ms <= 0.0:
            return 0.0
        jitter = self._config.delay_jitter
        if jitter > 0.0:
            delay_ms *= 1.0 + jitter * self._rng.uniform(-1.0, 1.0)
        return delay_ms / 1000.0

    # -- dedup ----------------------------------------------------------------

    def _collapse(self, keys: list[ObjectKey]) -> None:
        """Replace each stored file with a link to its size's canonical file.

        The first file of a given size becomes the canonical content, so the
        shared bytes are a real block. A file that has gone (evicted between
        the store and now) is skipped; any other error leaves the real file in
        place, which costs disk but never correctness.
        """
        assert self._base_path is not None and self._canonical_dir is not None
        collapsed = saved = created = errors = 0
        for key in keys:
            path = self._base_path / encode_fs_filename(key)
            try:
                st = os.lstat(path)
                if not stat.S_ISREG(st.st_mode):
                    continue  # already a symlink
                canonical = self._canonical_dir / f"{st.st_size}.canon"
                try:
                    os.link(path, canonical)
                    created += 1
                    if self._link_mode == "hardlink":
                        continue  # path and canonical are now one inode
                except FileExistsError:
                    if os.path.samestat(st, os.stat(canonical)):
                        continue  # already a hardlink to the canonical file
                with self._lock:
                    self._tmp_counter += 1
                    tmp = self._canonical_dir / f".link-{self._tmp_counter}.tmp"
                if self._link_mode == "hardlink":
                    os.link(canonical, tmp)
                else:
                    # Relative, so the link resolves the same way from inside
                    # a container and from the host.
                    os.symlink(os.path.relpath(canonical, self._base_path), tmp)
                os.replace(tmp, path)  # atomic: a reader sees old or new
                collapsed += 1
                saved += st.st_size
            except FileNotFoundError:
                continue
            except OSError as e:
                errors += 1
                logger.warning(
                    "DedupL2Adapter could not collapse %s (%s); keeping the "
                    "real file",
                    path.name,
                    e,
                )
        with self._lock:
            self._files_collapsed += collapsed
            self._bytes_saved += saved
            self._canonical_files += created
            self._collapse_errors += errors

    # -- pump -----------------------------------------------------------------

    def _collect(self, now: float) -> None:
        """Move the inner adapter's new completions into the held queues."""
        completed = self._inner.pop_completed_store_tasks()
        if completed:
            with self._lock:
                keys_by_task = {
                    task_id: self._store_keys.pop(task_id, None)
                    for task_id in completed
                }
            for task_id, result in completed.items():
                keys = keys_by_task[task_id]
                if self._dedup and keys and result.is_successful():
                    self._collapse(keys)
                with self._lock:
                    delay = self._delay_seconds(
                        self._config.write_delay_ms,
                        self._config.write_delay_ms_per_mb,
                        result.bytes_transferred(),
                    )
                    self._held_stores.append((now + delay, task_id, result))

        with self._lock:
            pending = list(self._load_bytes)
        for task_id in pending:
            bitmap = self._inner.query_load_result(task_id)
            if bitmap is None:
                continue
            with self._lock:
                nbytes = self._load_bytes.pop(task_id, 0)
                delay = self._delay_seconds(
                    self._config.read_delay_ms,
                    self._config.read_delay_ms_per_mb,
                    nbytes,
                )
                self._held_loads.append((now + delay, task_id, bitmap))

    def _release_due(self, now: float) -> Optional[float]:
        """Release completions whose delay has passed; signal the controllers.

        Returns:
            The earliest release time still pending, or ``None`` if nothing
            is held.
        """
        with self._lock:
            due_stores = [h for h in self._held_stores if h[0] <= now]
            due_loads = [h for h in self._held_loads if h[0] <= now]
            self._held_stores = [h for h in self._held_stores if h[0] > now]
            self._held_loads = [h for h in self._held_loads if h[0] > now]
            for _, task_id, result in due_stores:
                self._ready_stores[task_id] = result
            for _, task_id, bitmap in due_loads:
                self._ready_loads[task_id] = bitmap
            remaining = [h[0] for h in self._held_stores]
            remaining += [h[0] for h in self._held_loads]
        if due_stores:
            self._store_efd.notify()
        if due_loads:
            self._load_efd.notify()
        return min(remaining) if remaining else None

    def _pump_loop(self) -> None:
        """Collect inner completions and release them when their delay ends."""
        poller = select.poll()
        watched = {
            self._inner.get_store_event_fd(),
            self._inner.get_load_event_fd(),
            self._wake_efd.fileno(),
        }
        for fd in watched:
            poller.register(fd, select.POLLIN)

        timeout_ms: float = _PUMP_IDLE_TIMEOUT_MS
        while not self._stop.is_set():
            for fd, events in poller.poll(timeout_ms):
                if events & select.POLLIN:
                    try:
                        consume_fd(fd)
                    except (OSError, BlockingIOError):
                        pass
            if self._stop.is_set():
                break
            try:
                now = time.monotonic()
                self._collect(now)
                next_release = self._release_due(time.monotonic())
            except Exception:
                logger.exception("DedupL2Adapter pump iteration failed")
                next_release = None
            if next_release is None:
                timeout_ms = _PUMP_IDLE_TIMEOUT_MS
            else:
                wait_ms = (next_release - time.monotonic()) * 1000.0
                timeout_ms = max(0.0, min(wait_ms, _PUMP_IDLE_TIMEOUT_MS))

    # -- event fds (ours, so delayed completions still wake the server) -------

    def get_store_event_fd(self) -> int:
        """Return this adapter's store event fd, signalled on delayed release."""
        return self._store_efd.fileno()

    def get_lookup_and_lock_event_fd(self) -> int:
        """Return the inner adapter's lookup-and-lock event fd (not delayed)."""
        return self._inner.get_lookup_and_lock_event_fd()

    def get_load_event_fd(self) -> int:
        """Return this adapter's load event fd, signalled on delayed release."""
        return self._load_efd.fileno()

    # -- store (delegate the write; intercept the completion) -----------------

    def submit_store_task(
        self, keys: list[ObjectKey], objects: list[MemoryObj]
    ) -> L2TaskId:
        """Submit the store to the inner adapter, recording ``keys``.

        The lock is held across the inner submit so the pump cannot observe
        the completion before the keys are recorded.
        """
        with self._lock:
            task_id = self._inner.submit_store_task(keys, objects)
            self._store_keys[task_id] = list(keys)
        return task_id

    def pop_completed_store_tasks(self) -> dict[L2TaskId, L2StoreResult]:
        """Pop the store tasks whose files are collapsed and whose delay passed."""
        with self._lock:
            completed = self._ready_stores
            self._ready_stores = {}
        return completed

    # -- lookup and lock (pure delegation) ------------------------------------

    def submit_lookup_and_lock_task(
        self, keys: list[ObjectKey], group_layout_descs: dict[int, MemoryLayoutDesc]
    ) -> L2TaskId:
        """Delegate the lookup-and-lock task to the inner adapter."""
        return self._inner.submit_lookup_and_lock_task(keys, group_layout_descs)

    def query_lookup_and_lock_result(self, task_id: L2TaskId) -> Bitmap | None:
        """Delegate to the inner adapter; lookup results are passed through."""
        return self._inner.query_lookup_and_lock_result(task_id)

    def submit_unlock(self, keys: list[ObjectKey]) -> None:
        """Delegate the unlock to the inner adapter."""
        self._inner.submit_unlock(keys)

    # -- load (delegate the read; intercept the completion) -------------------

    def submit_load_task(
        self, keys: list[ObjectKey], objects: list[MemoryObj]
    ) -> L2TaskId:
        """Submit the load to the inner adapter, recording its size.

        The loaded bytes are the canonical block for each key's size, not the
        KV originally stored under that key.
        """
        nbytes = 0
        for obj in objects:
            try:
                nbytes += len(obj.byte_array)
            except Exception:
                pass
        with self._lock:
            task_id = self._inner.submit_load_task(keys, objects)
            self._load_bytes[task_id] = nbytes
        return task_id

    def query_load_result(self, task_id: L2TaskId) -> Bitmap | None:
        """Return the load result once the inner load is done and its delay passed.

        Thread-safe. Non-idempotent: returns a non-None bitmap only once per
        ``task_id``.
        """
        with self._lock:
            return self._ready_loads.pop(task_id, None)

    # -- listener / eviction / usage (delegate) -------------------------------

    def register_listener(self, listener: L2AdapterListener) -> None:
        """Forward the listener registration to the inner adapter."""
        self._inner.register_listener(listener)

    def set_backend_identity(self, name: str, shared: bool = False) -> None:
        """Forward the event-tagging identity to the inner adapter (which
        owns the listener-notify funnel that tags cache events)."""
        self._inner.set_backend_identity(name, shared)

    def submit_touch(self, keys: list[ObjectKey]) -> None:
        """Delegate the touch to the inner adapter."""
        self._inner.submit_touch(keys)

    def delete(self, keys: list[ObjectKey]) -> None:
        """Delegate the delete; unlinking a key's entry leaves the canonical file."""
        self._inner.delete(keys)

    def list_l2_keys(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Delegate key listing to the inner adapter."""
        return self._inner.list_l2_keys(*args, **kwargs)

    def get_usage(self) -> AdapterUsage:
        """Return the inner adapter's usage: logical bytes, as if not deduped."""
        return self._inner.get_usage()

    @property
    def supports_global_eviction(self) -> bool:
        """Whether the inner adapter supports aggregate usage-based eviction."""
        return self._inner.supports_global_eviction

    def report_status(self) -> dict:
        """Return the inner adapter's status, annotated with the dedup state."""
        status = self._inner.report_status()
        with self._lock:
            status["dedup"] = {
                "enabled": self._dedup,
                "link_mode": self._link_mode,
                "canonical_dir": (
                    str(self._canonical_dir) if self._canonical_dir else None
                ),
                "canonical_files_created": self._canonical_files,
                "files_collapsed": self._files_collapsed,
                "bytes_saved": self._bytes_saved,
                "collapse_errors": self._collapse_errors,
                "write_delay_ms": self._config.write_delay_ms,
                "write_delay_ms_per_mb": self._config.write_delay_ms_per_mb,
                "read_delay_ms": self._config.read_delay_ms,
                "read_delay_ms_per_mb": self._config.read_delay_ms_per_mb,
                "delay_jitter": self._config.delay_jitter,
                "held_stores": len(self._held_stores),
                "held_loads": len(self._held_loads),
            }
        return status

    def get_memory_tags(self) -> MemoryTags:
        """Return the inner adapter's memory tags."""
        return self._inner.get_memory_tags()

    def close(self) -> None:
        """Stop the pump, then close the inner adapter and the event fds."""
        self._stop.set()
        self._wake_efd.notify()
        self._pump.join(timeout=5.0)
        self._inner.close()
        for efd in (self._store_efd, self._load_efd, self._wake_efd):
            efd.close()


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------
#
# Registering here is what an in-tree copy of this file needs; it is harmless
# (and lets ``{"type": "dedup"}`` work once the module is imported) when the
# file is loaded out of tree through the plugin adapter.

register_l2_adapter_type("dedup", DedupL2AdapterConfig)


def _create_dedup_adapter(
    config: L2AdapterConfigBase,
    l1_memory_desc: "Optional[L1MemoryDesc]" = None,
) -> L2AdapterInterface:
    """Create a ``DedupL2Adapter`` from a ``DedupL2AdapterConfig``.

    Args:
        config: The parsed adapter config; must be a ``DedupL2AdapterConfig``.
        l1_memory_desc: Optional L1 memory descriptor, forwarded to the inner
            adapter's factory.

    Returns:
        A ``DedupL2Adapter`` wrapping the freshly built inner adapter.
    """
    assert isinstance(config, DedupL2AdapterConfig)
    return DedupL2Adapter(config, l1_memory_desc)


register_l2_adapter_factory("dedup", _create_dedup_adapter)

#!/usr/bin/env python3
"""Generate docker-compose.yml for the LMCache Fleet Demo:
  - 1 coordinator (fleet-wide key directory)
  - N MP servers  (each: L1 DRAM tier + L2 fs adapter, registers w/ coordinator,
                   reports cache events so the directory tracks L1/L2 placements)
  - N stimulators (one-shot scripts/stim.py containers that STORE synthetic KV
                   chunks into their paired server; shares the server's IPC
                   namespace so the CPU SHM path works)
  - N restorers   (the same script fetching chunks back, from L1 or via L2)

Writes docker/docker-compose.yml and monitoring/prometheus.yml, both generated
and both gitignored. Volume paths are relative to docker/, since that is where
compose resolves them from.

Usage (from the repo root):
  python3 scripts/gen_compose.py [N]    # N = number of MP servers (default 3)
  SEQ=8 NUM_SERVERS=4 python3 scripts/gen_compose.py   # env overrides
Then:
  docker compose up -d                  # coordinator + servers
  docker compose --profile stim up stim-1 ... stim-N
  scripts/report.sh                     # directory summary (L1/L2 per server)
"""
import json
import os
import random
import re
import socket
import subprocess
import sys

# ---------------- `--env-vars` listing ---------------------------------------
# Descriptions live here; DEFAULTS ARE NOT REPEATED. They are read back out of
# the source below, so the listing cannot drift from what the code actually
# does -- and any variable missing from this map still gets listed, under
# "undocumented", rather than quietly going unmentioned.
ENV_DOCS = {
    "NUM_SERVERS": ("Fleet", "number of MP servers"),
    "NUM_COORD": ("Fleet", "coordinators the servers are dealt across; each tracks only its own"),
    "COORD_EVENT_FLUSH_INTERVAL": ("Fleet", "seconds between a server's cache-event posts; unset = LMCache's 1"),
    "KV_EVENT_LOG_SIZE": ("Fleet", "records each server keeps for KV-event subscribers; 0 = channel off"),
    "LMCACHE_IMAGE": ("Fleet", "image for coordinator, servers and stimulators"),
    "CHUNK_SIZE": ("Fleet", "chunk size; must match across servers and coordinator"),
    "L1_GIB": ("Fleet", "per-server L1 DRAM tier size in GiB"),
    "EVICT_WATERMARK": ("Fleet", "L1 fraction that triggers the L2 spill"),
    "SHM_SIZE": ("Fleet", "/dev/shm per server and stimulator"),
    "COORD_PORT": ("Fleet", "host port of the first coordinator; the rest follow it"),
    "HTTP_BASE": ("Fleet", "first host port for the servers' HTTP APIs"),

    "SEQ": ("Workload", "sequences each stimulator stores (its window width)"),
    "NUM_TOKENS": ("Workload", "tokens per store request; multiple of CHUNK_SIZE"),
    "SHAPE_SPEC": ("Workload", "bench KV shape; bigger fills L1 faster"),
    "TOTAL": ("Workload", "total unique blocks the fleet may address"),
    "SEED": ("Workload", "seeds where each stimulator's window lands"),
    "RANGE_LO": ("Workload", "low bound of the block sub-range windows sit in"),
    "RANGE_HI": ("Workload", "high bound of that sub-range"),

    "STIM_OP": ("Workload", "what stim-N does: store, or verify (store then reread)"),
    "STIM_INTERVAL": ("Workload", "seconds between a stimulator's store requests"),
    "RESTORE_ORDER": ("Workload", "how restore-N walks its window: random or sequential"),
    "RESTORE_COUNT": ("Workload", "restore requests each restore-N sends"),
    "RESTORE_INTERVAL": ("Workload", "seconds between a restore-N's requests"),
    "RESTORE_MIN_HIT_RATE": ("Workload", "restore-N fails below this fraction of chunks found"),

    "L2_DEDUP": ("L2 simulation", "1 = one canonical file per block size (fake contents)"),
    "L2_LINK_MODE": ("L2 simulation", "hardlink or symlink for collapsed files"),
    "L2_WRITE_DELAY_MS": ("L2 simulation", "fixed delay added to each L2 store"),
    "L2_READ_DELAY_MS": ("L2 simulation", "fixed delay added to each L2 load"),
    "L2_WRITE_DELAY_MS_PER_MB": ("L2 simulation", "store delay per MiB written"),
    "L2_READ_DELAY_MS_PER_MB": ("L2 simulation", "load delay per MiB read"),
    "L2_DELAY_JITTER": ("L2 simulation", "fractional jitter on L2 delays, 0 to 1"),
    "L2_CAPACITY_GIB": ("L2 simulation", "per-server L2 capacity in logical GiB; 0 = unbounded, no eviction"),
    "L2_EVICT_POLICY": ("L2 simulation", "L2 eviction policy once a capacity is set: LRU or ARC"),
    "L2_EVICT_WATERMARK": ("L2 simulation", "L2 usage fraction that triggers eviction"),
    "L2_EVICT_RATIO": ("L2 simulation", "fraction of L2 evicted each time it triggers"),

    "L2_SHARED": ("L2 sharing", "0 = each server its own L2; 1 = add a tier the servers share; only = shared tier alone"),
    "SHARE_GROUPS": ("L2 sharing", "shared directories the servers are dealt across: 1 = all share one, NUM_SERVERS = none do"),
    "L2_SHARED_CAPACITY_GIB": ("L2 sharing", "shared-tier capacity in logical GiB, counted per server; 0 = unbounded"),
    "L2_SHARED_READ_DELAY_MS": ("L2 sharing", "fixed delay added to each load from the shared tier"),
    "L2_SHARED_WRITE_DELAY_MS": ("L2 sharing", "fixed delay added to each store to the shared tier"),

    "TRACE": ("Trace replay", "token trace under data/traces to replay (see scripts/traces.py)"),
    "TRACE_ROUTE": ("Trace replay", "what stays on one server: session, group or request"),
    "TRACE_LANES": ("Trace replay", "sessions in flight across the fleet"),
    "TRACE_SPEEDUP": ("Trace replay", "divide the trace's own timing by this"),
    "TRACE_IDLE_CAP": ("Trace replay", "longest gap kept between a session's requests, trace seconds"),
    "TRACE_DURATION": ("Trace replay", "stop scheduling after this many replay seconds; 0 = whole trace"),
    "TRACE_SESSIONS": ("Trace replay", "replay only the first N sessions; 0 = all"),
    "TRACE_SHAPE": ("Trace replay", "bench KV shape for trace clients; blocks x 16 tokens bounds a prompt"),
    "TRACE_RUN": ("Trace replay", "name of the directory under data/trace-runs the request logs go to"),
    "TRACE_MIN_HIT_RATE": ("Trace replay", "trace-N fails below this fraction of chunks found"),

    "PROM_IMAGE": ("Monitoring", "Prometheus image"),
    "GRAFANA_IMAGE": ("Monitoring", "Grafana image"),
    "EXPORTER_IMAGE": ("Monitoring", "image the directory exporter runs in"),
    "SCRAPE_INTERVAL": ("Monitoring", "Prometheus scrape interval"),
    "PROM_RETENTION": ("Monitoring", "how long metric history is kept"),
    "GRAFANA_PORTS": ("Monitoring", "host port range Grafana may bind"),
    "GRAFANA_LOG_LEVEL": ("Monitoring", "Grafana log level: debug, info, warn or error"),
    "COORD_CHECKPOINT": ("Fleet", "1 = checkpoint the coordinator's directory so a restart resumes"),
    "COORD_CHECKPOINT_INTERVAL": ("Fleet", "seconds between checkpoint writes; 0 = only on a clean stop"),
    "QUIET_ACCESS_LOG": ("Monitoring", "1 = drop uvicorn access lines for polled paths"),
    "QUIET_ACCESS_PATHS": ("Monitoring", "comma-separated path prefixes treated as polled"),
    "GRAFANA_PORT": ("Monitoring", "pin Grafana to exactly this host port"),
    "DIRECTORY_POLL": ("Monitoring", "seconds between directory exporter polls"),
    "DIRECTORY_PAGE_SIZE": ("Monitoring", "keys per page the exporter reads; up to 10000"),
    "AGGREGATE": ("Monitoring", "1 = run fleet aggregators above the coordinators; on when NUM_COORD > 1"),
    "AGG_FANOUT": ("Monitoring", "children per aggregator; 0 = one aggregator over all, N >= 2 = a tree"),
    "AGG_POLL": ("Monitoring", "seconds between an aggregator's reads of its children"),
    "AGG_PORT": ("Monitoring", "host port the root aggregator is published on"),
    "SUMMARY_BLOOM_BITS": ("Monitoring", "bits in the fleet summary's block filter; same at every level"),

    "RUN_UID": ("Runtime", "uid every container runs as"),
    "RUN_GID": ("Runtime", "gid every container runs as"),
    "CGROUP_PARENT": ("Runtime", "systemd slice for every container, e.g. system.slice; unset = docker's default"),
    "COMPOSE_PROJECT_NAME": ("Runtime", "scopes the network and container names"),

    # Read by the exporter itself, so they are set on that service rather than
    # passed to gen_compose.
    "COORDINATOR_URL": ("Exporter (set on the service)", "coordinator to poll"),
    "POLL_INTERVAL": ("Exporter (set on the service)", "seconds between polls"),
    "PAGE_SIZE": ("Exporter (set on the service)", "keys per directory page"),
    "EXPORTER_PORT": ("Exporter (set on the service)", "port /metrics is served on"),
    "MISSING_POLLS": ("Exporter (set on the service)", "absences before a placement counts as gone"),
    "TOP_REUSED": ("Exporter (set on the service)", "most-reused keys published individually by hash; 0 = off"),
    "STATE_PATH": ("Exporter (set on the service)", "where first-seen times are checkpointed"),
    "STATE_SAVE_INTERVAL": ("Exporter (set on the service)", "seconds between checkpoints"),
    "AGE_BOUNDS": ("Exporter (set on the service)", "age bucket bounds, comma-separated durations"),
}

_SECTION_ORDER = ["Fleet", "Workload", "Trace replay", "L2 simulation", "L2 sharing", "Monitoring", "Runtime",
                  "Exporter (set on the service)", "undocumented"]

# Defaults the source computes rather than spells out.
COMPUTED_DEFAULTS = {
    "RESTORE_COUNT": "SEQ",
    "NUM_SERVERS": "3 (or argv[1])",
    "TOTAL": "NUM_SERVERS x SEQ x blocks",
    "RANGE_HI": "TOTAL",
    "RUN_UID": "your uid",
    "RUN_GID": "your gid",
    "AGE_BOUNDS": "5m,30m,1h,2h,4h,8h",
}


def _scan_env_defaults(path):
    """Yield (name, default) for every os.environ.get in a source file.

    Reads the call text directly instead of importing, so listing the
    variables never runs the generator's side effects. Parentheses are
    balanced by hand because some defaults contain calls of their own.
    """
    with open(path) as f:
        source = f.read()
    needle = "os.environ" + ".get("   # split so this line is not itself a hit
    start = 0
    while (found := source.find(needle, start)) != -1:
        cursor = found + len(needle)
        depth, arg_start = 1, cursor
        while cursor < len(source) and depth:
            if source[cursor] == "(":
                depth += 1
            elif source[cursor] == ")":
                depth -= 1
            cursor += 1
        args = source[arg_start:cursor - 1]
        start = cursor
        name, _, default = args.partition(",")
        name = name.strip().strip("\"'")
        default = " ".join(default.split()).strip() or "(unset)"
        if default.startswith(("'", '"')) and default.endswith(("'", '"')):
            default = default[1:-1]
        if name.isupper():
            yield name, default


def _env_vars_palette():
    """ANSI codes matching the Makefile's `help` target, or blanks when piped.

    Colour only when stdout is a terminal, so redirecting or grepping the
    listing yields plain text. NO_COLOR is honoured as well.
    """
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return {k: "" for k in ("bold", "cyan", "dim", "off")}
    return {"bold": "\033[1m", "cyan": "\033[36m", "dim": "\033[2m", "off": "\033[0m"}


def _print_env_vars():
    """Print every environment variable the demo reads, grouped, with defaults."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sources = [os.path.join(here, "scripts", "gen_compose.py"),
               os.path.join(here, "monitoring", "exporter", "directory_exporter.py")]
    found = {}
    for source in sources:
        if os.path.exists(source):
            for name, default in _scan_env_defaults(source):
                found.setdefault(name, COMPUTED_DEFAULTS.get(name, default))

    sections = {}
    for name, default in found.items():
        section, description = ENV_DOCS.get(name, ("undocumented", ""))
        sections.setdefault(section, []).append((name, default, description))

    c = _env_vars_palette()
    print(f"\n{c['bold']}LMCache Fleet environment variables{c['off']}\n")
    print(f"  {c['dim']}Pass them to make, e.g."
          f"  L2_DEDUP=1 L1_GIB=0.05 make run NUM=2 SEQ=6000{c['off']}")
    print(f"  {c['dim']}NUM and SEQ are make variables; "
          f"everything below is an env var.{c['off']}")
    for section in _SECTION_ORDER:
        if section not in sections:
            continue
        print(f"\n{c['bold']}{section}{c['off']}")
        for name, default, description in sorted(sections[section]):
            # Pad inside the colour, so the escapes never count toward the width.
            print(f"  {c['cyan']}{name:<26}{c['off']} {default:<26} {description}")
    print()


if "--env-vars" in sys.argv:
    _print_env_vars()
    raise SystemExit(0)


# ---------------- tunables (env-overridable) --------------------------------
NUM_SERVERS = int(os.environ.get("NUM_SERVERS", sys.argv[1] if len(sys.argv) > 1 else "3"))

# Coordinators. LMCache v0.5.8 has no coordinator clustering: a key directory
# lives in one process, and a server's --coordinator-url takes a single URL. So
# more than one coordinator here means pods, not a cluster: servers are dealt
# across NUM_COORD coordinators in turn, each of which tracks its own servers
# and knows nothing of the rest. That is what a single coordinator needs once a
# fleet outgrows it. It decodes and applies every cache event on one event
# loop; measured on 100 servers replaying agent traces it was offered about 960
# batches a second, applied 81, and spent the rest of its core decoding posts
# that had already timed out at the server. Past that point it does not slow
# down, it stops being right. Dealing is the same modulo SHARE_GROUPS uses, so
# NUM_COORD == SHARE_GROUPS gives each shared L2 directory its own coordinator.
NUM_COORD = int(os.environ.get("NUM_COORD", "1"))
if not 1 <= NUM_COORD <= NUM_SERVERS:
    sys.exit(f"NUM_COORD={NUM_COORD} must be between 1 and NUM_SERVERS={NUM_SERVERS}")


def coord_name(k):
    """Service name of coordinator k. The first keeps the bare name everything
    outside this file knows it by."""
    return "coordinator" if k == 1 else f"coordinator-{k}"


def coord_of(i):
    """Which coordinator (1-based) server i reports to."""
    return (i - 1) % NUM_COORD + 1


def exporter_name(k):
    return "directory-exporter" if k == 1 else f"directory-exporter-{k}"


# Fleet aggregators: what stands above coordinators that know only their own
# servers. Each directory exporter publishes a summary of its coordinator's
# directory that can be merged with others' (monitoring/exporter/
# fleet_summary.py), and an aggregator merges its children's and publishes the
# result in the same form -- so an aggregator's children can be aggregators.
#
#   AGG_FANOUT  children per aggregator. 0 puts one aggregator over every
#               coordinator. N >= 2 builds a tree: aggregators over groups of N
#               exporters, then over groups of N of those, up to a single root.
#               The numbers at the root are the same either way; the tree is
#               there to show, and to test, that nothing changes with depth.
#
# The root is always the service `aggregator`; the rest are named by level.
AGGREGATE = os.environ.get("AGGREGATE", "1" if NUM_COORD > 1 else "0") \
    not in ("0", "", "false", "no")
AGG_FANOUT = int(os.environ.get("AGG_FANOUT", "0"))
AGG_POLL = os.environ.get("AGG_POLL", "30")
# The root answers /summary and /locate?hash=..., so it is published on the
# host's loopback like the coordinators are.
AGG_PORT = int(os.environ.get("AGG_PORT", "9106"))
if AGG_FANOUT < 0 or AGG_FANOUT == 1:
    sys.exit(f"AGG_FANOUT={AGG_FANOUT} must be 0 (one aggregator over everything) or at "
             "least 2: a fan-out of 1 never narrows to a root")
AGGREGATORS = []          # (name, level, [child URL, ...], is the root)
if AGGREGATE:
    _below = [f"http://{exporter_name(k)}:9105" for k in range(1, NUM_COORD + 1)]
    _level = 1
    while True:
        _fan = AGG_FANOUT or len(_below)
        _groups = [_below[i:i + _fan] for i in range(0, len(_below), _fan)]
        _root = len(_groups) == 1
        _names = ["aggregator" if _root else f"aggregator-{_level}-{n}"
                  for n in range(1, len(_groups) + 1)]
        AGGREGATORS += [(name, _level, group, _root)
                        for name, group in zip(_names, _groups, strict=True)]
        if _root:
            break
        _below = [f"http://{name}:9106" for name in _names]
        _level += 1
AGG_LEVELS = max((level for _n, level, _c, _r in AGGREGATORS), default=0)

# Sizes are GiB, and named so. LMCache's own flags say "gb" (--l1-size-gb,
# max_capacity_gb) but multiply by 1024**3, so the old *_GB names here promised
# a unit nothing honoured. An old name still set is an error rather than
# ignored: silently falling back to the 1 GiB default L1 turns a run meant to
# spill into one that never does.
for _old, _new in (("L1_GB", "L1_GIB"), ("L2_CAPACITY_GB", "L2_CAPACITY_GIB")):
    if _old in os.environ:
        sys.exit(f"{_old} was renamed {_new} (the value was always GiB); "
                 f"set {_new}={os.environ[_old]} instead")

IMAGE        = os.environ.get("LMCACHE_IMAGE", "lmcache-fleet-demo/lmcache:0.5.8-cpu")
CHUNK_SIZE   = int(os.environ.get("CHUNK_SIZE", "256"))
L1_GIB        = float(os.environ.get("L1_GIB", "1"))
SEQS         = int(os.environ.get("SEQ", "6"))          # distinct synthetic sequences per stimulator
NUM_TOKENS   = int(os.environ.get("NUM_TOKENS", "512")) # tokens per synthetic request
COORD_PORT   = int(os.environ.get("COORD_PORT", "9300")) # host 127.0.0.1 port for coordinator
# The port the coordinator listens on INSIDE the fleet network. Fixed, and
# deliberately separate from COORD_PORT: everything on the network addresses
# the coordinator by service name and this port, while COORD_PORT only decides
# which host port that is published to. Conflating them meant overriding
# COORD_PORT pointed every server at a port the coordinator was not listening
# on, and the key directory silently stayed empty.
COORD_NET_PORT = 9300
HTTP_BASE    = int(os.environ.get("HTTP_BASE", "8181")) # host ports: 8181, 8182, ...
EVICT_WATERMARK = os.environ.get("EVICT_WATERMARK", "0.8")
SHM_SIZE = os.environ.get("SHM_SIZE", "512m")

# Two things a server keeps in memory for the sake of whoever tracks its blocks,
# both invisible at four servers and both decisive at a hundred.
#
# The KV-event channel: a log of store and eviction records plus a cache of the
# tokens behind them, for engines that subscribe to it. Nothing in this fleet
# does, and it still fills: up to 32768 records and 65536 token bindings, each
# holding a chunk's token ids as Python ints, about 10 KB apiece. Measured at
# roughly a quarter of a GiB an hour per server until full. Off by default
# here; set a size to turn it back on.
KV_EVENT_LOG_SIZE = int(os.environ.get("KV_EVENT_LOG_SIZE", "0"))
# How often a server posts its cache events to its coordinator. Unset leaves
# LMCache's own default of one second.
COORD_EVENT_FLUSH_INTERVAL = os.environ.get("COORD_EVENT_FLUSH_INTERVAL", "")
SERVER_EVENT_FLAGS = f"      --kv-event-log-size {KV_EVENT_LOG_SIZE}\n" + (
    f"      --coordinator-event-flush-interval {float(COORD_EVENT_FLUSH_INTERVAL):g}\n"
    if COORD_EVENT_FLUSH_INTERVAL else "")

# Run every container as the invoking user so files written to the L2 bind
# mounts are owned by us, not by whatever uid the image happens to default to.
RUN_UID = os.environ.get("RUN_UID", str(os.getuid()))
RUN_GID = os.environ.get("RUN_GID", str(os.getgid()))
# Some nodes reject a container that names no systemd slice ("cgroup-parent for
# systemd cgroup should be a valid slice"). Unset, nothing is written and docker
# picks its own parent, as before.
CGROUP_PARENT = os.environ.get("CGROUP_PARENT", "")
RUN_AS = (f"    cgroup_parent: {CGROUP_PARENT}\n" if CGROUP_PARENT else "") \
    + f'    user: "{RUN_UID}:{RUN_GID}"\n    environment:\n      HOME: /tmp\n'

# docker/Dockerfile deliberately sets no ENTRYPOINT, so the command compose
# passes is what runs. This clears any ENTRYPOINT anyway, which costs nothing
# and keeps an overridden LMCACHE_IMAGE from running something before lmcache.
NO_ENTRYPOINT = '    entrypoint: [""]\n'

# Compose project name: scopes the network, volumes and container names so a
# second LMCache stack on the same node cannot collide with this one.
PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "lmcache-fleet-demo")

# ---------------- key-space assignment --------------------------------------
# `lmcache bench server` generates its KV deterministically from the sequence
# number, so two stimulators given the same --start/--end store byte-identical
# keys. Its ONLY key selector is a contiguous half-open [--start, --end) range:
# there is no --seed and no way to hand it a scattered set. So the PRNG lives
# here, choosing where each stimulator's contiguous window lands inside a shared
# key space, and the bench just walks the window it is given.
#
#   TOTAL      total unique blocks the fleet may address
#   SEED       seeds the per-stimulator window placement; same seed → same layout
#   RANGE_LO / RANGE_HI   optional block sub-range windows must stay inside
#
# A sequence covers NUM_TOKENS/CHUNK_SIZE blocks, so block counts are converted
# to sequence counts before being handed to the bench.
BLOCKS_PER_SEQ = NUM_TOKENS // CHUNK_SIZE
if BLOCKS_PER_SEQ < 1:
    sys.exit(f"NUM_TOKENS={NUM_TOKENS} is smaller than CHUNK_SIZE={CHUNK_SIZE}: "
             "a request must cover at least one block")
if NUM_TOKENS % CHUNK_SIZE:
    sys.exit(f"NUM_TOKENS={NUM_TOKENS} must be a multiple of CHUNK_SIZE={CHUNK_SIZE}")

# Default key space: wide enough for every stimulator's window to fit without
# overlapping, which keeps the old scale while making the windows distinct.
_default_total = NUM_SERVERS * SEQS * BLOCKS_PER_SEQ
TOTAL = int(os.environ.get("TOTAL", _default_total))
SEED = os.environ.get("SEED", "0")
RANGE_LO = int(os.environ.get("RANGE_LO", "0"))
RANGE_HI = int(os.environ.get("RANGE_HI", TOTAL))

if TOTAL % BLOCKS_PER_SEQ:
    sys.exit(f"TOTAL={TOTAL} must be a multiple of NUM_TOKENS/CHUNK_SIZE "
             f"({BLOCKS_PER_SEQ} blocks per sequence)")
if not 0 <= RANGE_LO < RANGE_HI <= TOTAL:
    sys.exit(f"need 0 <= RANGE_LO < RANGE_HI <= TOTAL, got "
             f"RANGE_LO={RANGE_LO} RANGE_HI={RANGE_HI} TOTAL={TOTAL}")
if RANGE_LO % BLOCKS_PER_SEQ or RANGE_HI % BLOCKS_PER_SEQ:
    sys.exit(f"RANGE_LO={RANGE_LO} and RANGE_HI={RANGE_HI} must be multiples of "
             f"{BLOCKS_PER_SEQ} (blocks per sequence)")

_lo_seq, _hi_seq = RANGE_LO // BLOCKS_PER_SEQ, RANGE_HI // BLOCKS_PER_SEQ
if _hi_seq - _lo_seq < SEQS:
    sys.exit(f"range [{RANGE_LO},{RANGE_HI}) holds {_hi_seq - _lo_seq} sequences "
             f"but each stimulator stores SEQ={SEQS}; widen the range, lower SEQ, "
             "or raise TOTAL")

# One window per stimulator. Seeding per-stimulator (rather than drawing from
# one stream) keeps each server's window stable when NUM_SERVERS changes.
windows = []
for _i in range(1, NUM_SERVERS + 1):
    _rng = random.Random(f"{SEED}:{_i}")
    _start = _rng.randrange(_lo_seq, _hi_seq - SEQS + 1)
    windows.append((_start, _start + SEQS))

# ---------------- L2 backend -------------------------------------------------
# Stock: the fs adapter, real bytes on disk. With L2_DEDUP=1 (or any L2 delay)
# the fs adapter is wrapped by plugins/lmcache_l2_sim, loaded through LMCache's
# built-in `plugin` adapter type: every key keeps a unique filename, but all
# files of one size share a single canonical file, so large block counts cost
# almost no disk. L2 reads then return that canonical block rather than the
# key's own KV, so the bench's checksum verification is switched off with it.
L2_DEDUP = os.environ.get("L2_DEDUP", "0") not in ("0", "", "false", "no")
L2_LINK_MODE = os.environ.get("L2_LINK_MODE", "hardlink")
L2_DELAYS = {
    "write_delay_ms": float(os.environ.get("L2_WRITE_DELAY_MS", "0")),
    "write_delay_ms_per_mb": float(os.environ.get("L2_WRITE_DELAY_MS_PER_MB", "0")),
    "read_delay_ms": float(os.environ.get("L2_READ_DELAY_MS", "0")),
    "read_delay_ms_per_mb": float(os.environ.get("L2_READ_DELAY_MS_PER_MB", "0")),
    "delay_jitter": float(os.environ.get("L2_DELAY_JITTER", "0")),
}

# L2 eviction. Off by default: L2 grows without bound, as it always has here.
# Setting L2_CAPACITY_GIB turns it on; _l2_spec below has what that takes.
L2_CAPACITY_GIB = float(os.environ.get("L2_CAPACITY_GIB", "0"))
L2_EVICT_POLICY = os.environ.get("L2_EVICT_POLICY", "LRU")
L2_EVICT_WATERMARK = float(os.environ.get("L2_EVICT_WATERMARK", "0.8"))
L2_EVICT_RATIO = float(os.environ.get("L2_EVICT_RATIO", "0.2"))
if L2_CAPACITY_GIB < 0:
    sys.exit(f"L2_CAPACITY_GIB={L2_CAPACITY_GIB:g} must be >= 0 (0 disables L2 eviction)")
# IsolatedLRU evicts per cache_salt against coordinator quotas, not against
# this capacity, and noop never evicts: neither does what the knob says.
if L2_EVICT_POLICY not in ("LRU", "ARC"):
    sys.exit(f"L2_EVICT_POLICY={L2_EVICT_POLICY!r} must be LRU or ARC")
if not 0 < L2_EVICT_WATERMARK <= 1:
    sys.exit(f"L2_EVICT_WATERMARK={L2_EVICT_WATERMARK:g} must be in (0, 1]")
if not 0 < L2_EVICT_RATIO <= 1:
    sys.exit(f"L2_EVICT_RATIO={L2_EVICT_RATIO:g} must be in (0, 1]")

# A second L2 tier, shared between servers. LMCache takes --l2-adapter more than
# once: every key is stored to every adapter, and a lookup is served by the
# first adapter, in the order given, that holds the key. So the local tier goes
# first (a server's own NVMe) and the shared one second (a filesystem or object
# store every server mounts): a block comes back from local disk when this
# server has it and from the shared tier when only another server stored it.
#
#   L2_SHARED     0     each server has its own L2 and nothing else (as before)
#                 1     local tier, then the shared tier
#                 only  the shared tier alone
#   SHARE_GROUPS  how many shared directories there are. Servers are dealt
#                 across them in turn, so 1 means the whole fleet shares one,
#                 and NUM_SERVERS means nobody shares anything -- the sweep
#                 from one to the other is how sharing is varied.
L2_SHARED = os.environ.get("L2_SHARED", "0")
if L2_SHARED in ("", "false", "no"):
    L2_SHARED = "0"
if L2_SHARED in ("true", "yes"):
    L2_SHARED = "1"
if L2_SHARED not in ("0", "1", "only"):
    sys.exit(f"L2_SHARED={L2_SHARED!r} must be 0, 1 or only")
SHARE_GROUPS = int(os.environ.get("SHARE_GROUPS", "1"))
if not 1 <= SHARE_GROUPS <= NUM_SERVERS:
    sys.exit(f"SHARE_GROUPS={SHARE_GROUPS} must be between 1 and NUM_SERVERS={NUM_SERVERS}")
L2_SHARED_CAPACITY_GIB = float(os.environ.get("L2_SHARED_CAPACITY_GIB", "0"))
L2_SHARED_DELAYS = {
    "write_delay_ms": float(os.environ.get("L2_SHARED_WRITE_DELAY_MS", "0")),
    "read_delay_ms": float(os.environ.get("L2_SHARED_READ_DELAY_MS", "0")),
}
L2_PLUGIN = L2_DEDUP or any(L2_DELAYS.values()) or \
    (L2_SHARED != "0" and any(L2_SHARED_DELAYS.values()))


def _l2_spec(base_path, capacity_gib, delays, shared=False, tmp_dir=None):
    """One --l2-adapter spec: fs or fs_native, wrapped by the simulator if asked.

    Setting a capacity turns eviction on, and three things have to change
    together for LMCache to act on it -- get any one wrong and nothing is evicted:
      * the backing adapter becomes fs_native. Plain fs declares no capacity,
        so LMCache logs "does not support global eviction" and skips it.
      * max_capacity_gb goes on that backing spec. It is counted in logical
        bytes, so under dedup it bounds what the server believes it stored,
        not the near-zero disk actually used -- which is the point of it.
      * the "eviction" block goes on the OUTERMOST spec, the one --l2-adapter
        parses. Inside the plugin's "inner" it is ignored without a warning.
    """
    eviction = {}
    if capacity_gib:
        eviction = {"eviction": {"eviction_policy": L2_EVICT_POLICY,
                                 "trigger_watermark": L2_EVICT_WATERMARK,
                                 "eviction_ratio": L2_EVICT_RATIO}}
        # The block is repeated on the backing spec even when the plugin wraps
        # it. It does nothing there, but fs_native otherwise warns that the
        # capacity "will NOT bound disk usage" because it sees none on its own
        # spec -- a false alarm when the outer block is doing the work.
        backing = {"type": "fs_native", "base_path": base_path,
                   "max_capacity_gb": capacity_gib, **eviction}
    else:
        backing = {"type": "fs", "base_path": base_path}
    if tmp_dir:
        # Writers to one shared directory need a temp name each. Both fs
        # adapters default to <key>.tmp, so two servers storing the same block
        # at once rename each other's file away and the loser logs a traceback.
        backing["relative_tmp_dir"] = tmp_dir
    # "shared" tells LMCache the domain is mounted by several servers, so the
    # coordinator records one placement for a block rather than one per server.
    flags = {"shared": True} if shared else {}
    if L2_DEDUP or any(delays.values()):
        return {
            "type": "plugin",
            "module_path": "lmcache_l2_sim.l2_sim_adapter",
            "class_name": "L2SimAdapter",
            "adapter_params": {
                "inner": {**backing, **flags},
                "dedup": L2_DEDUP,
                "link_mode": L2_LINK_MODE,
                "seed": int(SEED) if SEED.lstrip("-").isdigit() else 0,
                **{k: v for k, v in delays.items() if v},
            },
            **flags, **eviction,
        }
    return {**backing, **flags}


def l2_adapters(i):
    """The --l2-adapter specs for server i, in lookup order: local, then shared."""
    specs = []
    if L2_SHARED != "only":
        specs.append(_l2_spec("/data/l2", L2_CAPACITY_GIB, L2_DELAYS))
    if L2_SHARED != "0":
        specs.append(_l2_spec("/data/l2-shared", L2_SHARED_CAPACITY_GIB, L2_SHARED_DELAYS,
                              shared=True, tmp_dir=f".tmp-server-{i}"))
    return specs


def share_group(i):
    return (i - 1) % SHARE_GROUPS + 1


# plugins/ holds one directory per publishable distribution, so the directory
# itself is not importable -- each plugin's own directory is. Build PYTHONPATH
# from every subdirectory carrying a pyproject.toml, so adding a plugin needs no
# change here: drop the directory in and regenerate.
_PLUGIN_MOUNT = "/opt/lmcache-plugins"
_plugins_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "plugins")
PLUGIN_DIRS = sorted(
    name for name in os.listdir(_plugins_dir)
    if os.path.isfile(os.path.join(_plugins_dir, name, "pyproject.toml"))
) if os.path.isdir(_plugins_dir) else []
PLUGIN_PYTHONPATH = ":".join(f"{_PLUGIN_MOUNT}/{name}" for name in PLUGIN_DIRS)

# Prometheus scrapes /metrics, compose probes /healthcheck, the servers
# heartbeat and the exporter pages /directory/keys -- every few seconds, each
# logged by uvicorn at INFO. On a small fleet that is ~150 lines a minute
# saying nothing, and the coordinator is the worst of it. LMCache hardcodes
# access_log=True with no flag to turn it down, so docker/quiet/sitecustomize.py
# goes on PYTHONPATH instead: Python imports a module of that name at startup,
# early enough to attach a filter to the access logger. It drops only polled
# paths that returned under 400, so a failing healthcheck still prints.
# The coordinator's key directory is derived state rebuilt from the servers'
# cache events, so losing it costs hit rate rather than correctness -- but a
# restart otherwise starts cold, and on a fleet that has stored for an hour
# that means the dashboard and `make report` read empty until the servers
# report again. Checkpointing it into the data tree makes a restart resume.
# Operator-set state (L2 pins, per-cache_salt quotas) is separate and written
# only when it changes, hence the second path.
COORD_CHECKPOINT = os.environ.get("COORD_CHECKPOINT", "1") not in ("0", "", "false", "no")
COORD_CHECKPOINT_INTERVAL = os.environ.get("COORD_CHECKPOINT_INTERVAL", "60")

QUIET_ACCESS_LOG = os.environ.get("QUIET_ACCESS_LOG", "1")
_QUIET_MOUNT = "/opt/lmcache-fleet/quiet"
_PYTHONPATH = ([_QUIET_MOUNT] if QUIET_ACCESS_LOG not in ("0", "", "false", "no") else []) \
    + ([PLUGIN_PYTHONPATH] if L2_PLUGIN else [])
# Mounted on every lmcache container; only the HTTP servers have an access log
# to quieten, but the path has to exist wherever PYTHONPATH names it.
QUIET_MOUNT = (f"      - ../docker/quiet:{_QUIET_MOUNT}:ro      # access-log filter\n"
               if QUIET_ACCESS_LOG not in ("0", "", "false", "no") else "")
PY_ENV = f"      PYTHONPATH: {':'.join(_PYTHONPATH)}\n" if _PYTHONPATH else ""
# Both files live in the data tree, so `make clean` takes them with the rest
# of the run's state rather than leaving a stale directory to be resumed from.
def _coord_volumes(k):
    """The volumes block of coordinator k: the access-log filter, and its own
    state directory when it checkpoints."""
    state = (f"      - ../data/{coord_name(k)}:/state            "
             "# checkpointed key directory, pins and quotas\n"
             if COORD_CHECKPOINT else "")
    return f"    volumes:\n{QUIET_MOUNT}{state}" if QUIET_MOUNT + state else ""


COORD_FLAGS = (f"      --checkpoint-path /state/checkpoint.json\n"
               f"      --checkpoint-interval {COORD_CHECKPOINT_INTERVAL}\n"
               f"      --metadata-path /state/metadata.json\n"
               if COORD_CHECKPOINT else "")
# The coordinator loads no plugins, so it gets the quiet path only rather than
# a PYTHONPATH naming a plugins mount it does not have.
COORD_ENV = f"      PYTHONPATH: {_QUIET_MOUNT}\n" if QUIET_MOUNT else ""
L2_ENV = PY_ENV
L2_PLUGIN_MOUNT = (f"      - ../plugins:{_PLUGIN_MOUNT}:ro           # out-of-tree L2 adapters\n"
                   if L2_PLUGIN else "")
if L2_PLUGIN and not PLUGIN_DIRS:
    sys.exit(f"no plugin distributions found under {_plugins_dir} "
             "(each needs its own directory with a pyproject.toml)")
# Checksum verification compares retrieved KV against what was stored, which
# dedup deliberately breaks for anything served from L2.
STIM_URL = '""' if L2_DEDUP else None

# Monitoring sidecars. Grafana is the only service published beyond loopback;
# it takes a host port RANGE so compose picks a free one rather than failing on
# a busy node. `make dash` prints whichever port it landed on.
PROM_IMAGE   = os.environ.get("PROM_IMAGE", "prom/prometheus:latest")
GRAFANA_IMAGE = os.environ.get("GRAFANA_IMAGE", "grafana/grafana:latest")
SCRAPE_INTERVAL = os.environ.get("SCRAPE_INTERVAL", "5s")
PROM_RETENTION = os.environ.get("PROM_RETENTION", "6h")
GRAFANA_PORTS = os.environ.get("GRAFANA_PORTS", "3000-3010")
GRAFANA_LOG_LEVEL = os.environ.get("GRAFANA_LOG_LEVEL", "warn")
# Set GRAFANA_PORT to pin one exactly; otherwise a port is chosen from the
# range below and written into the compose file as a fixed mapping.
GRAFANA_PORT = os.environ.get("GRAFANA_PORT")


def _port_free(port):
    """Whether we can bind 0.0.0.0:port, the way docker publishes it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _running_grafana_port():
    """Host port this project's grafana already publishes, if it is running.

    Preferring it is what makes the port sticky: without this the picker would
    see the port as busy (our own container holds it) and move to the next one,
    recreating grafana somewhere else on every regeneration.
    """
    try:
        out = subprocess.run(
            ["docker", "ps",
             "--filter", f"label=com.docker.compose.project={PROJECT}",
             "--filter", "label=com.docker.compose.service=grafana",
             "--format", "{{.Ports}}"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r":(\d+)->3000/tcp", out)
    return int(match.group(1)) if match else None


def _pick_grafana_port():
    """Return a stable host port, or None to let docker choose from the range.

    Handing compose a range makes docker pick, and its allocator walks forward
    past recently-used ports rather than reusing the lowest free one -- so the
    port drifts on every restart. Choosing here instead keeps it put.
    """
    if GRAFANA_PORT:
        return int(GRAFANA_PORT)
    low, _, high = GRAFANA_PORTS.partition("-")
    low, high = int(low), int(high or low)
    current = _running_grafana_port()
    if current is not None and low <= current <= high:
        return current
    for candidate in range(low, high + 1):
        if _port_free(candidate):
            return candidate
    return None


_grafana_port = _pick_grafana_port()
GRAFANA_PORT_SPEC = (f"{_grafana_port}:3000" if _grafana_port
                     else f"{GRAFANA_PORTS}:3000")
# Directory exporter: polls the coordinator's key directory and publishes
# per-server inventory, placement age, replication and churn.
EXPORTER_IMAGE = os.environ.get("EXPORTER_IMAGE", "python:3-alpine")
DIRECTORY_POLL = os.environ.get("DIRECTORY_POLL", "15")
# Keys the exporter asks the coordinator for at a time. Every page costs the
# coordinator a pass over its whole directory before the keys themselves, and
# it answers on the same event loop that applies cache events, so on a large
# directory fewer, larger pages are much cheaper for both: 82,000 keys took
# 11 s in pages of 1000 and under 5 s in pages of 5000. The coordinator caps a
# page at 10000.
DIRECTORY_PAGE_SIZE = int(os.environ.get("DIRECTORY_PAGE_SIZE", "1000"))
if not 1 <= DIRECTORY_PAGE_SIZE <= 10000:
    sys.exit(f"DIRECTORY_PAGE_SIZE={DIRECTORY_PAGE_SIZE} must be between 1 and 10000, "
             "the most the coordinator will return in one page")
# The exporter serves its last finished poll, so Prometheus can read it more
# often than it polls. It has to once polls are minutes apart: a series not
# sampled for five minutes goes stale and every directory panel shows gaps.
DIRECTORY_SCRAPE = min(float(DIRECTORY_POLL), 60.0)

# KV shape for bench: (n_kv,n_blocks,tokens_per_block,n_heads,head_dim):dtype:n_layers
# Sized small — this host has ~4 GB spare RAM. Each bench client allocates
# n_kv*n_layers tensors of NB*BS*H*D*2bytes; keep the product well under L1.
SHAPE_SPEC = os.environ.get("SHAPE_SPEC", "(2,64,16,4,64):float16:4")

# ---------------- stimulators ------------------------------------------------
# scripts/stim.py rather than `lmcache bench server`. The bench is a sanity
# test with one fixed case: store a sequence, then read the same one straight
# back. Every block is read while still in L1, L2 never serves a read, and the
# write and read rates cannot be told apart. stim.py drives the same client one
# operation at a time, so stores and restores are separate services:
#   stim-N     profile "stim":    stores its window, reads nothing back
#   restore-N  profile "restore": retrieves from the same window, stores nothing
# STIM_OP=verify puts the bench's store-then-reread case back for stim-N.
STIM_OP = os.environ.get("STIM_OP", "store")
if STIM_OP not in ("store", "verify"):
    sys.exit(f"STIM_OP={STIM_OP!r} must be store or verify "
             "(restores are their own service: make restore)")
STIM_INTERVAL = float(os.environ.get("STIM_INTERVAL", "0.2"))
# Restores sample the window at random by default. In order, a restore walks
# the oldest blocks first, which are exactly the ones L2 eviction has taken.
RESTORE_ORDER = os.environ.get("RESTORE_ORDER", "random")
if RESTORE_ORDER not in ("random", "sequential"):
    sys.exit(f"RESTORE_ORDER={RESTORE_ORDER!r} must be random or sequential")
RESTORE_COUNT = int(os.environ.get("RESTORE_COUNT", str(SEQS)))
RESTORE_INTERVAL = float(os.environ.get("RESTORE_INTERVAL", "0.2"))
RESTORE_MIN_HIT_RATE = float(os.environ.get("RESTORE_MIN_HIT_RATE", "0"))
_SCRIPTS_MOUNT = "/opt/lmcache-fleet/scripts"

# Trace replay: trace-N replays its server's share of one token trace, built by
# scripts/traces.py from real agent sessions or synthesised. Every trace-N
# computes the same schedule from the trace, then keeps the requests routed to
# its own server, so the fleet as a whole replays the trace once.
TRACE = os.environ.get("TRACE", "").strip("/")
TRACE_ROUTE = os.environ.get("TRACE_ROUTE", "session")
if TRACE_ROUTE not in ("session", "group", "request"):
    sys.exit(f"TRACE_ROUTE={TRACE_ROUTE!r} must be session, group or request")
TRACE_LANES = int(os.environ.get("TRACE_LANES", "8"))
TRACE_SPEEDUP = float(os.environ.get("TRACE_SPEEDUP", "1"))
TRACE_IDLE_CAP = float(os.environ.get("TRACE_IDLE_CAP", "60"))
TRACE_DURATION = float(os.environ.get("TRACE_DURATION", "0"))
TRACE_SESSIONS = int(os.environ.get("TRACE_SESSIONS", "0"))
TRACE_MIN_HIT_RATE = float(os.environ.get("TRACE_MIN_HIT_RATE", "0"))
TRACE_RUN = os.environ.get("TRACE_RUN", "latest")
# A real prompt is 100k tokens, not the 512 the synthetic stimulators send, and
# at the default shape that would be 400 MiB of KV per request. The trace shape
# keeps a chunk at 8 KiB so a whole session fits in memory; 8192 blocks of 16
# tokens bound a prompt at 131072 tokens, and longer ones are truncated.
TRACE_SHAPE = os.environ.get("TRACE_SHAPE", "(2,8192,16,1,8):float16:1")
_TRACE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "traces", TRACE)
if TRACE and not os.path.isfile(os.path.join(_TRACE_DIR, "index.jsonl")):
    sys.exit(f"TRACE={TRACE}: no token trace at {_TRACE_DIR} "
             "(make trace-synth or make trace-exgentic builds one)")

# ---------------- what this run is --------------------------------------------
# The dashboard's top row describes the run it is showing: tier sizes, the L2
# simulation, the workload. None of that is in any metric -- the servers export
# L2 usage but not capacity, and the simulator's settings appear nowhere -- so
# it is published here as the labels of one constant series, by a recording
# rule. A rule rather than the directory exporter because the exporter is the
# first thing to fall over on a fleet too big for it, and a description of the
# run is most wanted exactly then.
def _tier(capacity_gib, delays):
    """One L2 tier in words: backing adapter, capacity, eviction, latency."""
    if capacity_gib:
        text = (f"{capacity_gib:g} GiB per server, {L2_EVICT_POLICY} at "
                f"{L2_EVICT_WATERMARK:.0%} evicting {L2_EVICT_RATIO:.0%}")
    else:
        text = "unbounded, never evicts"
    lag = ", ".join(f"{name} +{delays.get(f'{name}_delay_ms', 0):g} ms"
                    for name in ("read", "write") if delays.get(f"{name}_delay_ms"))
    return f"{text}; {lag}" if lag else text


if TRACE:
    _workload = (f"trace {TRACE}: route by {TRACE_ROUTE}, {TRACE_LANES} lanes, "
                 f"speedup {TRACE_SPEEDUP:g}"
                 + (f", stops at {TRACE_DURATION:g} s" if TRACE_DURATION else ""))
else:
    _workload = (f"synthetic windows: {SEQS} sequences of {NUM_TOKENS} tokens per server, "
                 f"{STIM_OP} every {STIM_INTERVAL:g} s")
RUN_INFO = {
    "fleet": (f"{NUM_SERVERS} server{'s' if NUM_SERVERS != 1 else ''}, "
              + ("1 coordinator" if NUM_COORD == 1 else
                 f"{NUM_COORD} coordinators each tracking its own "
                 f"{'server' if NUM_COORD == NUM_SERVERS else 'servers'}")
              + f", {CHUNK_SIZE}-token chunks"),
    "l1": f"{L1_GIB:g} GiB DRAM per server, spills to L2 at {float(EVICT_WATERMARK):.0%}",
    "l2_local": "none" if L2_SHARED == "only" else _tier(L2_CAPACITY_GIB, L2_DELAYS),
    "l2_shared": ("none" if L2_SHARED == "0" else
                  f"{SHARE_GROUPS} director{'y' if SHARE_GROUPS == 1 else 'ies'} for "
                  f"{NUM_SERVERS} servers; "
                  + _tier(L2_SHARED_CAPACITY_GIB, L2_SHARED_DELAYS)),
    "l2_sim": ((f"dedup on ({L2_LINK_MODE}s): L2 contents are fake" if L2_DEDUP
                else "latency only: L2 contents are real") if L2_PLUGIN
               else "off: stock adapter, real bytes"),
    "aggregation": (
        "none: nothing above the coordinator" + ("s" if NUM_COORD > 1 else "")
        if not AGGREGATORS else
        f"{len(AGGREGATORS)} aggregator{'s' if len(AGGREGATORS) != 1 else ''} in "
        f"{AGG_LEVELS} level{'s' if AGG_LEVELS != 1 else ''} above the coordinators"
        + (f", {AGG_FANOUT} children each" if AGG_FANOUT else "")),
    "workload": _workload,
    "image": IMAGE,
}

servers, stimulators = [], []

for i in range(1, NUM_SERVERS + 1):
    name = f"lmcache-server-{i}"
    l2_flags = "\n".join(
        f"      --l2-adapter '{json.dumps(spec, separators=(',', ':'))}'"
        for spec in l2_adapters(i))
    l2_volumes = ""
    if L2_SHARED != "only":
        l2_volumes += (f"      - ../data/l2-server-{i}:/data/l2"
                       "              # this server's own L2\n")
    if L2_SHARED != "0":
        l2_volumes += (f"      - ../data/l2-shared-{share_group(i)}:/data/l2-shared"
                       "       # the L2 its share group has in common\n")
    servers.append(f"""  {name}:
    image: {IMAGE}
    hostname: {name}
{NO_ENTRYPOINT}{RUN_AS}{L2_ENV}    ipc: shareable
    shm_size: {SHM_SIZE}
    networks: [fleet]
    ports:
      - "127.0.0.1:{HTTP_BASE + i - 1}:8080"   # HTTP API on host for /status, /cache/objects
    volumes:
{l2_volumes}{L2_PLUGIN_MOUNT}{QUIET_MOUNT}    healthcheck:
      test: ["CMD", "/opt/venv/bin/python", "-c",
             "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthcheck',timeout=2)"]
      interval: 5s
      timeout: 3s
      retries: 30
      start_period: 15s
    command: >
      /opt/venv/bin/lmcache server
      --host 0.0.0.0 --port 5555
      --http-host 0.0.0.0 --http-port 8080
      --chunk-size {CHUNK_SIZE}
      --l1-size-gb {L1_GIB}
      --eviction-policy LRU
      --eviction-trigger-watermark {EVICT_WATERMARK}
      --supported-transfer-mode auto
      --shm-name fleet{i} --no-l1-use-lazy
      --coordinator-url http://{coord_name(coord_of(i))}:{COORD_NET_PORT}
      --coordinator-advertise-ip {name}
      --coordinator-event-reporting
{SERVER_EVENT_FLAGS}{l2_flags}
""")

    # Everything a client of server i needs, whichever operation it runs.
    # The PID namespace is shared for the same reason the IPC one is, and the
    # two go together. LMCache names each client's POSIX SHM segment
    # /lmcache_kv_<pid>_<counter>, and a container's main process is always
    # pid 1 -- so a store and a restore sharing the server's IPC namespace both
    # ask for /lmcache_kv_1_0 and the second one dies with EEXIST. Sharing the
    # server's PID namespace gives every client a distinct pid, and therefore a
    # distinct segment name, however many of them there are.
    _client = f"""{NO_ENTRYPOINT}{RUN_AS}    ipc: service:{name}          # share /dev/shm so the client's CPU SHM path reaches the server
    pid: service:{name}          # distinct pids, so the clients' SHM names cannot collide
    shm_size: {SHM_SIZE}
    networks: [fleet]
    volumes:
      - ../scripts:{_SCRIPTS_MOUNT}:ro
{QUIET_MOUNT}
    depends_on:
      {name}:
        condition: service_healthy
    command: >
      /opt/venv/bin/python -u {_SCRIPTS_MOUNT}/stim.py
      --rpc-url tcp://{name}:5555
      --url {STIM_URL or f"http://{name}:8080"}
      --mode cpu --transfer-mode lmcache_driven
"""
    _window = f"""      --kvcache-shape-spec "{SHAPE_SPEC}"
      --num-tokens {NUM_TOKENS}
      --start {windows[i - 1][0]} --end {windows[i - 1][1]}
"""
    stimulators.append(f"""  stim-{i}:
    image: {IMAGE}
    profiles: [stim]
{_client}{_window}      --op {STIM_OP} --interval {STIM_INTERVAL:g}
""")
    # Its own client index, so a restore running alongside the store does not
    # register its workers under the ids the store client already holds.
    stimulators.append(f"""  restore-{i}:
    image: {IMAGE}
    profiles: [restore]
{_client}{_window}      --op restore --client-index 1
      --order {RESTORE_ORDER} --count {RESTORE_COUNT} --seed {i}
      --interval {RESTORE_INTERVAL:g} --min-hit-rate {RESTORE_MIN_HIT_RATE:g}
""")
    if TRACE:
        # The trace and the run's logs are extra mounts, so the shared client
        # block gets them spliced in after the scripts mount.
        _trace_client = _client.replace(
            f"      - ../scripts:{_SCRIPTS_MOUNT}:ro\n",
            f"      - ../scripts:{_SCRIPTS_MOUNT}:ro\n"
            f"      - ../data/traces/{TRACE}:/trace:ro\n"
            f"      - ../data/trace-runs/{TRACE_RUN}:/run\n")
        stimulators.append(f"""  trace-{i}:
    image: {IMAGE}
    profiles: [trace]
{_trace_client}      --kvcache-shape-spec "{TRACE_SHAPE}"
      --op trace --client-index 2 --trace /trace --trace-name {TRACE}
      --shard {i - 1} --shards {NUM_SERVERS} --route {TRACE_ROUTE}
      --lanes {TRACE_LANES} --speedup {TRACE_SPEEDUP:g} --idle-cap {TRACE_IDLE_CAP:g}
      {f"--duration {TRACE_DURATION:g} " if TRACE_DURATION else ""}{f"--max-sessions {TRACE_SESSIONS} " if TRACE_SESSIONS else ""}--min-hit-rate {TRACE_MIN_HIT_RATE:g}
      --log /run/trace-{i}.jsonl
""")

coordinators = "\n".join(f"""  {coord_name(k)}:
    image: {IMAGE}
    hostname: {coord_name(k)}
{NO_ENTRYPOINT}{RUN_AS}{COORD_ENV}    networks: [fleet]
{_coord_volumes(k)}    ports:
      - "127.0.0.1:{COORD_PORT + k - 1}:{COORD_NET_PORT}"
    healthcheck:
      test: ["CMD", "/opt/venv/bin/python", "-c",
             "import urllib.request;urllib.request.urlopen('http://localhost:{COORD_NET_PORT}/healthz',timeout=2)"]
      interval: 5s
      timeout: 3s
      retries: 30
    command: >
      /opt/venv/bin/lmcache coordinator
      --host 0.0.0.0 --port {COORD_NET_PORT}
      --chunk-size {CHUNK_SIZE}
      --instance-timeout 30
{COORD_FLAGS}""" for k in range(1, NUM_COORD + 1))


# The filter in a fleet summary is one size at every level, because filters
# merge by OR. The default (1 MiB of bits) is about 1% wrong at 870,000 blocks
# beneath the root; a bigger fleet wants a bigger one.
SUMMARY_BLOOM_BITS = os.environ.get("SUMMARY_BLOOM_BITS", "")
SUMMARY_ENV = f'      SUMMARY_BLOOM_BITS: "{int(SUMMARY_BLOOM_BITS)}"\n' if SUMMARY_BLOOM_BITS else ""

# One exporter per coordinator: it polls a single directory, and its ages and
# churn counters are kept per placement, so each needs its own state too.
exporters = "\n".join(f"""  {exporter_name(k)}:
    image: {EXPORTER_IMAGE}
    hostname: {exporter_name(k)}
{RUN_AS}      COORDINATOR_URL: http://{coord_name(k)}:{COORD_NET_PORT}
      POLL_INTERVAL: "{DIRECTORY_POLL}"
      PAGE_SIZE: "{DIRECTORY_PAGE_SIZE}"
      SUMMARY_NAME: "{coord_name(k)}"
{SUMMARY_ENV}    networks: [fleet]
    volumes:
      - ../monitoring/exporter:/exporter:ro
      - ../data/{"exporter" if k == 1 else f"exporter-{k}"}:/state            # first-seen times, so ages survive a restart
    command: ["python", "-u", "/exporter/directory_exporter.py"]
""" for k in range(1, NUM_COORD + 1))

_AGG_ROOT_PORTS = f'    ports:\n      - "127.0.0.1:{AGG_PORT}:9106"\n'
aggregators = "\n".join(f"""  {name}:
    image: {EXPORTER_IMAGE}
    hostname: {name}
{RUN_AS}      AGGREGATOR_NAME: "{name}"
      AGGREGATOR_LEVEL: "{level}"
      CHILDREN: "{','.join(children)}"
      POLL_INTERVAL: "{AGG_POLL}"
    networks: [fleet]
{_AGG_ROOT_PORTS if root else ""}    volumes:
      - ../monitoring/exporter:/exporter:ro
    command: ["python", "-u", "/exporter/fleet_aggregator.py"]
""" for name, level, children, root in AGGREGATORS)

compose = f"""# Generated by gen_compose.py — LMCache Fleet Demo (v0.5.8)
# {NUM_SERVERS} MP servers (L1 DRAM + L2 fs) + {NUM_COORD} coordinator(s) + {NUM_SERVERS} stimulators.
# Containers run as uid:gid {RUN_UID}:{RUN_GID}; project "{PROJECT}" scopes the network.
# Regenerate with: NUM_SERVERS=<n> SEQ=<seqs> python3 gen_compose.py

name: {PROJECT}

networks:
  fleet:
    driver: bridge

services:
{coordinators}

  prometheus:
    image: {PROM_IMAGE}
    hostname: prometheus
{RUN_AS}    networks: [fleet]
    volumes:
      - ../monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ../monitoring/rules.yml:/etc/prometheus/rules.yml:ro
      - ../data/prometheus:/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time={PROM_RETENTION}

{exporters}{chr(10) + aggregators if aggregators else ""}
  grafana:
    image: {GRAFANA_IMAGE}
    hostname: grafana
{RUN_AS}      GF_AUTH_ANONYMOUS_ENABLED: "true"        # no login: straight to the dashboard
      GF_AUTH_ANONYMOUS_ORG_ROLE: "Admin"
      GF_AUTH_DISABLE_LOGIN_FORM: "true"
      GF_AUTH_BASIC_ENABLED: "false"
      GF_USERS_DEFAULT_THEME: "dark"
      GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH: /etc/grafana/dashboards/lmcache-fleet.json
      GF_ANALYTICS_REPORTING_ENABLED: "false"
      GF_ANALYTICS_CHECK_FOR_UPDATES: "false"
      # Grafana's own chatter. warn hides the plugin-update and flag-evaluation
      # INFO lines; the manifest watcher logs AT warn every poll about finding
      # zero manifests, which is just what a provisioned-only install looks
      # like, so that one logger is filtered to error.
      GF_LOG_LEVEL: "{GRAFANA_LOG_LEVEL}"
      GF_LOG_FILTERS: "search-manifest-watcher:error"
    networks: [fleet]
    depends_on: [prometheus]
    ports:
      - "{GRAFANA_PORT_SPEC}"   # picked at generation time and kept; `make dash` prints it
    volumes:
      - ../monitoring/grafana/provisioning:/etc/grafana/provisioning:ro
      - ../monitoring/grafana/dashboards:/etc/grafana/dashboards:ro
      - ../data/grafana:/var/lib/grafana

""" + "\n".join(servers + stimulators)

# ---------------- prometheus scrape config ----------------------------------
# Both the coordinator and the MP servers serve Prometheus on their normal HTTP
# port (NOT the server's --prometheus-port 9090, which only binds when metrics
# are not already on the HTTP app). Targets are addressed by compose service
# name on the fleet network, so nothing needs publishing to the host.
scrape_targets = "\n".join(
    f"""      - targets: ["lmcache-server-{i}:8080"]
        labels:
          server: "lmcache-server-{i}"
          component: "mp-server\""""
    for i in range(1, NUM_SERVERS + 1)
)

# The `coordinator` label is what tells one pod's series from another's: the
# coordinators and the exporters publish identical metric names.
coordinator_targets = "\n".join(
    f"""      - targets: ["{coord_name(k)}:{COORD_NET_PORT}"]
        labels:
          component: "coordinator"
          coordinator: "{coord_name(k)}\""""
    for k in range(1, NUM_COORD + 1))
exporter_targets = "\n".join(
    f"""      - targets: ["{exporter_name(k)}:9105"]
        labels:
          component: "directory-exporter"
          coordinator: "{coord_name(k)}\""""
    for k in range(1, NUM_COORD + 1))

# `root` marks the one aggregator whose numbers are the whole fleet's, which is
# what the dashboard reads; the rest are scraped so each level can be checked
# against the one above.
aggregator_job = ("""
  - job_name: "lmcache-fleet"
    static_configs:
""" + "\n".join(
    f"""      - targets: ["{name}:9106"]
        labels:
          component: "fleet-aggregator"
          aggregator: "{name}"
          level: "{level}"
          root: "{'true' if root else 'false'}\""""
    for name, level, _children, root in AGGREGATORS) + "\n") if AGGREGATORS else ""

prometheus_yml = f"""# Generated by gen_compose.py — do not edit; regenerate with `make gen`.
global:
  scrape_interval: {SCRAPE_INTERVAL}
  evaluation_interval: {SCRAPE_INTERVAL}

rule_files:
  - /etc/prometheus/rules.yml

scrape_configs:
  - job_name: "lmcache-coordinator"
    metrics_path: /metrics
    static_configs:
{coordinator_targets}

  - job_name: "lmcache-directory"
    scrape_interval: {DIRECTORY_SCRAPE:g}s
    static_configs:
{exporter_targets}
{aggregator_job}
  - job_name: "lmcache-servers"
    metrics_path: /metrics
    static_configs:
{scrape_targets}
"""

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Bind mounts must be writable by the container uid. Containers run as the
# invoking user by default, so owner-only write is enough; only fall back to
# world-writable when RUN_UID has been overridden to someone else.
BIND_MODE = 0o755 if str(os.getuid()) == RUN_UID else 0o777
for sub in (([f"l2-server-{i}" for i in range(1, NUM_SERVERS + 1)]
             if L2_SHARED != "only" else [])
            + ([f"l2-shared-{g}" for g in range(1, SHARE_GROUPS + 1)]
               if L2_SHARED != "0" else [])
            + ([os.path.join("trace-runs", TRACE_RUN)] if TRACE else [])
            + ["prometheus", "grafana"]
            + ["exporter" if k == 1 else f"exporter-{k}" for k in range(1, NUM_COORD + 1)]
            + ([coord_name(k) for k in range(1, NUM_COORD + 1)] if COORD_CHECKPOINT else [])):
    d = os.path.join(here, "data", sub)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, BIND_MODE)

mon = os.path.join(here, "monitoring")
os.makedirs(mon, exist_ok=True)
with open(os.path.join(mon, "prometheus.yml"), "w") as f:
    f.write(prometheus_yml)
# json.dumps gives a double-quoted string that is valid YAML whatever the value
# holds. Rewritten in place, so the running Prometheus sees it through its bind
# mount; it rereads rules on SIGHUP, which the make targets send.
with open(os.path.join(mon, "rules.yml"), "w") as f:
    f.write("# Generated by gen_compose.py -- do not edit; regenerate with `make gen`.\n"
            "groups:\n  - name: lmcache-fleet-run\n    rules:\n"
            "      - record: lmcache_fleet_run_info\n        expr: vector(1)\n"
            "        labels:\n"
            + "".join(f"          {k}: {json.dumps(v)}\n" for k, v in RUN_INFO.items()))

docker_dir = os.path.join(here, "docker")
os.makedirs(docker_dir, exist_ok=True)
out = os.path.join(docker_dir, "docker-compose.yml")
with open(out, "w") as f:
    f.write(compose)
print(f"wrote {out} ({NUM_SERVERS} servers, "
      f"{'coordinator' if NUM_COORD == 1 else f'{NUM_COORD} coordinators'}, stimulators, "
      f"prometheus + grafana; SEQ={SEQS})")
print("  compose no longer sits at the repo root: use make, or "
      "export COMPOSE_FILE=docker/docker-compose.yml")
print(f"wrote {os.path.join(mon, 'prometheus.yml')} "
      f"({NUM_SERVERS + 2 * NUM_COORD + len(AGGREGATORS)} scrape targets)")
if AGGREGATORS:
    print(f"aggregators: {len(AGGREGATORS)} in {AGG_LEVELS} level(s): "
          + "; ".join(f"{name} (L{level}) <- "
                      + ",".join(c.split("//")[1].split(":")[0] for c in children)
                      for name, level, children, _root in AGGREGATORS))
if NUM_COORD > 1:
    _pods = {}
    for _i in range(1, NUM_SERVERS + 1):
        _pods.setdefault(coord_of(_i), []).append(_i)
    print(f"coordinators: {NUM_COORD}, on host ports {COORD_PORT}-{COORD_PORT + NUM_COORD - 1}: "
          + "; ".join(f"{coord_name(k)} <- servers {','.join(map(str, m))}"
                      for k, m in sorted(_pods.items())))
    print("  each tracks only its own servers: `make report` and `make check` read "
          "the first one")

# Key-space summary: what each stimulator drew, and how much the fleet covers.
covered = set()
for _s, _e in windows:
    covered.update(range(_s, _e))
overlap = sum(e - s for s, e in windows) - len(covered)
print(f"\nkey space: TOTAL={TOTAL} blocks "
      f"({TOTAL // BLOCKS_PER_SEQ} sequences x {BLOCKS_PER_SEQ} blocks), "
      f"seed={SEED}, range=[{RANGE_LO},{RANGE_HI})")
for _i, (_s, _e) in enumerate(windows, 1):
    print(f"  stim-{_i}: sequences [{_s},{_e})  "
          f"blocks [{_s * BLOCKS_PER_SEQ},{_e * BLOCKS_PER_SEQ})")
print(f"  fleet covers {len(covered) * BLOCKS_PER_SEQ} unique blocks"
      f"{f'; {overlap * BLOCKS_PER_SEQ} blocks replicated' if overlap else ''}")
for _spec in l2_adapters(1):
    _inner = _spec.get("adapter_params", {}).get("inner", _spec)
    _shared = "shared" if _spec.get("shared") else "local"
    _how = "real bytes on disk"
    if _spec["type"] == "plugin":
        _params = _spec["adapter_params"]
        _delays = ", ".join(f"{k}={v:g}" for k, v in _params.items()
                            if k.endswith("_ms") or k.endswith("_mb")) or "no delay"
        _how = (f"wrapped by lmcache_l2_sim (dedup="
                f"{'on, ' + L2_LINK_MODE if L2_DEDUP else 'off'}; {_delays})"
                f"{' — contents are fake, checksum verification off' if L2_DEDUP else ''}")
    _cap = _inner.get("max_capacity_gb")
    _evict = (f"{L2_EVICT_POLICY} eviction at {L2_EVICT_WATERMARK:.0%} of "
              f"{_cap:g} GiB logical per server, {L2_EVICT_RATIO:.0%} each time"
              if _cap else "no capacity, grows without bound")
    print(f"L2 {_shared}: {_inner['type']}, {_how}; {_evict}")
if L2_SHARED != "0":
    _groups = {}
    for _i in range(1, NUM_SERVERS + 1):
        _groups.setdefault(share_group(_i), []).append(_i)
    print(f"L2 sharing: {SHARE_GROUPS} shared director{'y' if SHARE_GROUPS == 1 else 'ies'}: "
          + "; ".join(f"l2-shared-{g} <- servers {','.join(map(str, m))}"
                      for g, m in sorted(_groups.items())))
if TRACE:
    print(f"trace: {TRACE}, route={TRACE_ROUTE}, {TRACE_LANES} lanes, "
          f"speedup {TRACE_SPEEDUP:g}, logs in data/trace-runs/{TRACE_RUN}/")

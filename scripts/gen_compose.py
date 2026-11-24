#!/usr/bin/env python3
"""Generate docker-compose.yml for the LMCache Fleet Demo:
  - 1 coordinator (fleet-wide key directory)
  - N MP servers  (each: L1 DRAM tier + L2 fs adapter, registers w/ coordinator,
                   reports cache events so the directory tracks L1/L2 placements)
  - N stimulators (one-shot `lmcache bench server --mode cpu` containers that
                   STORE synthetic KV chunks into their paired server; shares
                   the server's IPC namespace so the CPU SHM path works)

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

# ---------------- tunables (env-overridable) --------------------------------
NUM_SERVERS = int(os.environ.get("NUM_SERVERS", sys.argv[1] if len(sys.argv) > 1 else "3"))

# Fleet size is programmable; coordinator count is not. LMCache v0.5.8 has no
# coordinator clustering: the key directory lives in one process, the server's
# --coordinator-url takes a single URL, and the only persistence is a
# single-writer --checkpoint-path. Named here so the topology is explicit and
# so this becomes a real knob if upstream grows HA coordinators.
NUM_COORD = int(os.environ.get("NUM_COORD", "1"))
if NUM_COORD != 1:
    sys.exit(f"NUM_COORD={NUM_COORD}: LMCache {os.environ.get('LMCACHE_VERSION', 'v0.5.8')} "
             "supports exactly one coordinator per fleet")

IMAGE        = os.environ.get("LMCACHE_IMAGE", "lmcache/standalone:v0.5.8")
CHUNK_SIZE   = int(os.environ.get("CHUNK_SIZE", "256"))
L1_GB        = float(os.environ.get("L1_GB", "1"))
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

# Run every container as the invoking user so files written to the L2 bind
# mounts are owned by us, not by whatever uid the image happens to default to.
RUN_UID = os.environ.get("RUN_UID", str(os.getuid()))
RUN_GID = os.environ.get("RUN_GID", str(os.getgid()))
RUN_AS = f'    user: "{RUN_UID}:{RUN_GID}"\n    environment:\n      HOME: /tmp\n'

# The lmcache image is built on NVIDIA's NGC PyTorch base, whose ENTRYPOINT is
# /opt/nvidia/nvidia_entrypoint.sh: CUDA banners, GPU-driver probes and a CPU
# capability check that misfires on this host and sleeps 2s per container
# start. This fleet is CPU-only -- no GPU, no vLLM -- so none of it applies.
# Skip it and exec lmcache directly.
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
# the fs adapter is wrapped by plugins/lmcache_dedup, loaded through LMCache's
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
L2_PLUGIN = L2_DEDUP or any(L2_DELAYS.values())
_fs_spec = {"type": "fs", "base_path": "/data/l2"}
if L2_PLUGIN:
    L2_ADAPTER = {
        "type": "plugin",
        "module_path": "lmcache_dedup.dedup_l2_adapter",
        "class_name": "DedupL2Adapter",
        "adapter_params": {
            "inner": _fs_spec,
            "dedup": L2_DEDUP,
            "link_mode": L2_LINK_MODE,
            "seed": int(SEED) if SEED.lstrip("-").isdigit() else 0,
            **{k: v for k, v in L2_DELAYS.items() if v},
        },
    }
else:
    L2_ADAPTER = _fs_spec
L2_ADAPTER_JSON = json.dumps(L2_ADAPTER, separators=(",", ":"))
L2_ENV = "      PYTHONPATH: /opt/lmcache-plugins\n" if L2_PLUGIN else ""
L2_PLUGIN_MOUNT = ("      - ../plugins:/opt/lmcache-plugins:ro           # out-of-tree L2 adapter\n"
                   if L2_PLUGIN else "")
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

# KV shape for bench: (n_kv,n_blocks,tokens_per_block,n_heads,head_dim):dtype:n_layers
# Sized small — this host has ~4 GB spare RAM. Each bench client allocates
# n_kv*n_layers tensors of NB*BS*H*D*2bytes; keep the product well under L1.
SHAPE_SPEC = os.environ.get("SHAPE_SPEC", "(2,64,16,4,64):float16:4")

servers, stimulators = [], []

for i in range(1, NUM_SERVERS + 1):
    name = f"lmcache-server-{i}"
    servers.append(f"""  {name}:
    image: {IMAGE}
    hostname: {name}
{NO_ENTRYPOINT}{RUN_AS}{L2_ENV}    ipc: shareable
    shm_size: {SHM_SIZE}
    networks: [fleet]
    ports:
      - "127.0.0.1:{HTTP_BASE + i - 1}:8080"   # HTTP API on host for /status, /cache/objects
    volumes:
      - ../data/l2-server-{i}:/data/l2              # per-server L2 backend (fs adapter)
{L2_PLUGIN_MOUNT}    healthcheck:
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
      --l1-size-gb {L1_GB}
      --eviction-policy LRU
      --eviction-trigger-watermark {EVICT_WATERMARK}
      --supported-transfer-mode auto
      --shm-name fleet{i} --no-l1-use-lazy
      --coordinator-url http://coordinator:{COORD_NET_PORT}
      --coordinator-advertise-ip {name}
      --coordinator-event-reporting
      --l2-adapter '{L2_ADAPTER_JSON}'
""")

    stimulators.append(f"""  stim-{i}:
    image: {IMAGE}
    profiles: [stim]
{NO_ENTRYPOINT}{RUN_AS}    ipc: service:{name}          # share /dev/shm so bench's CPU SHM path reaches the server
    shm_size: {SHM_SIZE}
    networks: [fleet]
    depends_on:
      {name}:
        condition: service_healthy
    command: >
      /opt/venv/bin/lmcache bench server
      --rpc-url tcp://{name}:5555
      --url {STIM_URL or f"http://{name}:8080"}
      --mode cpu --transfer-mode lmcache_driven
      --kvcache-shape-spec "{SHAPE_SPEC}"
      --num-tokens {NUM_TOKENS}
      --start {windows[i - 1][0]} --end {windows[i - 1][1]}
      --interval 0.2
""")

compose = f"""# Generated by gen_compose.py — LMCache Fleet Demo (v0.5.8)
# {NUM_SERVERS} MP servers (L1 DRAM + L2 fs) + {NUM_COORD} coordinator + {NUM_SERVERS} stimulators.
# Containers run as uid:gid {RUN_UID}:{RUN_GID}; project "{PROJECT}" scopes the network.
# Regenerate with: NUM_SERVERS=<n> SEQ=<seqs> python3 gen_compose.py

name: {PROJECT}

networks:
  fleet:
    driver: bridge

services:
  coordinator:
    image: {IMAGE}
    hostname: coordinator
{NO_ENTRYPOINT}{RUN_AS}    networks: [fleet]
    ports:
      - "127.0.0.1:{COORD_PORT}:{COORD_NET_PORT}"
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

  prometheus:
    image: {PROM_IMAGE}
    hostname: prometheus
{RUN_AS}    networks: [fleet]
    volumes:
      - ../monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ../data/prometheus:/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time={PROM_RETENTION}

  directory-exporter:
    image: {EXPORTER_IMAGE}
    hostname: directory-exporter
{RUN_AS}      COORDINATOR_URL: http://coordinator:{COORD_NET_PORT}
      POLL_INTERVAL: "{DIRECTORY_POLL}"
    networks: [fleet]
    volumes:
      - ../monitoring/exporter:/exporter:ro
      - ../data/exporter:/state            # first-seen times, so ages survive a restart
    command: ["python", "-u", "/exporter/directory_exporter.py"]

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

prometheus_yml = f"""# Generated by gen_compose.py — do not edit; regenerate with `make gen`.
global:
  scrape_interval: {SCRAPE_INTERVAL}
  evaluation_interval: {SCRAPE_INTERVAL}

scrape_configs:
  - job_name: "lmcache-coordinator"
    metrics_path: /metrics
    static_configs:
      - targets: ["coordinator:{COORD_NET_PORT}"]
        labels:
          component: "coordinator"

  - job_name: "lmcache-directory"
    scrape_interval: {DIRECTORY_POLL}s
    static_configs:
      - targets: ["directory-exporter:9105"]
        labels:
          component: "directory-exporter"

  - job_name: "lmcache-servers"
    metrics_path: /metrics
    static_configs:
{scrape_targets}
"""

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Bind mounts must be writable by the container uid. Containers run as the
# invoking user by default, so owner-only write is enough; only fall back to
# world-writable when RUN_UID has been overridden to someone else.
BIND_MODE = 0o755 if RUN_UID == str(os.getuid()) else 0o777
for sub in ([f"l2-server-{i}" for i in range(1, NUM_SERVERS + 1)]
            + ["prometheus", "grafana", "exporter"]):
    d = os.path.join(here, "data", sub)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, BIND_MODE)

mon = os.path.join(here, "monitoring")
os.makedirs(mon, exist_ok=True)
with open(os.path.join(mon, "prometheus.yml"), "w") as f:
    f.write(prometheus_yml)

docker_dir = os.path.join(here, "docker")
os.makedirs(docker_dir, exist_ok=True)
out = os.path.join(docker_dir, "docker-compose.yml")
with open(out, "w") as f:
    f.write(compose)
print(f"wrote {out} ({NUM_SERVERS} servers, coordinator, stimulators, "
      f"prometheus + grafana; SEQ={SEQS})")
print("  compose no longer sits at the repo root: use make, or "
      "export COMPOSE_FILE=docker/docker-compose.yml")
print(f"wrote {os.path.join(mon, 'prometheus.yml')} "
      f"({NUM_SERVERS + 2} scrape targets)")

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
if L2_PLUGIN:
    _delays = ", ".join(f"{k}={v:g}" for k, v in L2_DELAYS.items() if v) or "no delay"
    print(f"L2: fs wrapped by lmcache_dedup "
          f"(dedup={'on, ' + L2_LINK_MODE if L2_DEDUP else 'off'}; {_delays})"
          f"{' — L2 contents are fake, bench checksum verification off' if L2_DEDUP else ''}")
else:
    print("L2: fs (real bytes on disk)")

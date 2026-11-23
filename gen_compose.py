#!/usr/bin/env python3
"""Generate docker-compose.yml for the LMCache Fleet Demo:
  - 1 coordinator (fleet-wide key directory)
  - N MP servers  (each: L1 DRAM tier + L2 fs adapter, registers w/ coordinator,
                   reports cache events so the directory tracks L1/L2 placements)
  - N stimulators (one-shot `lmcache bench server --mode cpu` containers that
                   STORE synthetic KV chunks into their paired server; shares
                   the server's IPC namespace so the CPU SHM path works)

Usage:
  python3 gen_compose.py [N]            # N = number of MP servers (default 3)
  SEQ=8 NUM_SERVERS=4 python3 gen_compose.py   # env overrides
Then:
  docker compose up -d                  # coordinator + servers
  docker compose --profile stim up stim-1 ... stim-N
  ./report.sh                           # directory summary (L1/L2 per server)
"""
import os
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
HTTP_BASE    = int(os.environ.get("HTTP_BASE", "8181")) # host ports: 8181, 8182, ...
EVICT_WATERMARK = os.environ.get("EVICT_WATERMARK", "0.8")
SHM_SIZE = os.environ.get("SHM_SIZE", "512m")

# Run every container as the invoking user so files written to the L2 bind
# mounts are owned by us, not by whatever uid the image happens to default to.
RUN_UID = os.environ.get("RUN_UID", str(os.getuid()))
RUN_GID = os.environ.get("RUN_GID", str(os.getgid()))
RUN_AS = f'    user: "{RUN_UID}:{RUN_GID}"\n    environment:\n      HOME: /tmp\n'

# Compose project name: scopes the network, volumes and container names so a
# second LMCache stack on the same node cannot collide with this one.
PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "lmcache-fleet-demo")

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
{RUN_AS}    ipc: shareable
    shm_size: {SHM_SIZE}
    networks: [fleet]
    ports:
      - "127.0.0.1:{HTTP_BASE + i - 1}:8080"   # HTTP API on host for /status, /cache/objects
    volumes:
      - ./data/l2-server-{i}:/data/l2              # per-server L2 backend (fs adapter)
    healthcheck:
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
      --coordinator-url http://coordinator:{COORD_PORT}
      --coordinator-advertise-ip {name}
      --coordinator-event-reporting
      --l2-adapter '{{"type":"fs","base_path":"/data/l2"}}'
""")

    stimulators.append(f"""  stim-{i}:
    image: {IMAGE}
    profiles: [stim]
{RUN_AS}    ipc: service:{name}          # share /dev/shm so bench's CPU SHM path reaches the server
    shm_size: {SHM_SIZE}
    networks: [fleet]
    depends_on:
      {name}:
        condition: service_healthy
    command: >
      /opt/venv/bin/lmcache bench server
      --rpc-url tcp://{name}:5555
      --url http://{name}:8080
      --mode cpu --transfer-mode lmcache_driven
      --kvcache-shape-spec "{SHAPE_SPEC}"
      --num-tokens {NUM_TOKENS}
      --start 0 --end {SEQS}
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
{RUN_AS}    networks: [fleet]
    ports:
      - "127.0.0.1:{COORD_PORT}:9300"
    healthcheck:
      test: ["CMD", "/opt/venv/bin/python", "-c",
             "import urllib.request;urllib.request.urlopen('http://localhost:9300/healthz',timeout=2)"]
      interval: 5s
      timeout: 3s
      retries: 30
    command: >
      /opt/venv/bin/lmcache coordinator
      --host 0.0.0.0 --port 9300
      --chunk-size {CHUNK_SIZE}
      --instance-timeout 30

""" + "\n".join(servers + stimulators)

here = os.path.dirname(os.path.abspath(__file__))
for i in range(1, NUM_SERVERS + 1):          # L2 bind mounts must be writable by container uid
    d = os.path.join(here, "data", f"l2-server-{i}")
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o777)

out = os.path.join(here, "docker-compose.yml")
with open(out, "w") as f:
    f.write(compose)
print(f"wrote {out} ({NUM_SERVERS} servers, coordinator + stimulators; SEQ={SEQS})")

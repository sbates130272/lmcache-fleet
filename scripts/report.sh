#!/usr/bin/env bash
# Fleet report: coordinator directory (L1/L2 placements per instance), live L1
# usage, and L2 file counts with logical size vs space actually used on disk.
set -euo pipefail
COORD="${COORD:-http://127.0.0.1:9300}"
N="${1:-3}"

echo "== Coordinator @ $COORD =="
python3 - "$COORD" <<'PY'
import json, sys, urllib.request, collections
coord = sys.argv[1]
def get(path):
    with urllib.request.urlopen(coord + path, timeout=5) as r:
        return json.load(r)

insts = get("/instances").get("instances", [])
print(f"fleet: {len(insts)} registered instance(s)")
for i in sorted(insts, key=lambda x: x["instance_id"]):
    print(f"  {i['instance_id']:40s} {i['ip']}:{i['http_port']}")

print("\n== Key directory (placements) ==")
agg = collections.Counter()          # (instance_id, tier, backend) -> key count
tiers = collections.Counter()        # tier -> placement count
total_keys = 0
offset = 0
while True:
    page = get(f"/directory/keys?limit=1000&offset={offset}")
    total_keys = page["total"]
    for k in page["keys"]:
        for p in k.get("placements", []):
            agg[(p.get("instance_id", "?"), p.get("tier", "?"), p.get("backend", "?"))] += 1
            tiers[p.get("tier", "?")] += 1
    offset += len(page["keys"])
    if offset >= total_keys or not page["keys"]:
        break
print(f"unique keys tracked: {total_keys}")
for (inst, tier, backend), n in sorted(agg.items()):
    print(f"  {inst:40s} {tier:3s} via {backend:6s} : {n}")
for t, n in sorted(tiers.items()):
    print(f"  tier {t:3s} total placements: {n}")
PY

echo
echo "== Per-server L1 usage (HTTP /status) =="
for i in $(seq 1 "$N"); do
  port=$((8181 + i - 1))
  curl -sf "http://127.0.0.1:${port}/status" >/tmp/lmstatus.json 2>/dev/null || { echo "  server-$i: no response on $port"; continue; }
  python3 - "$i" <<'PY'
import json, sys, re
i = sys.argv[1]
d = json.load(open("/tmp/lmstatus.json"))
blob = json.dumps(d)
# surface any l1 usage/capacity-ish fields we can find
found = re.findall(r'"((?:l1|used|capacity|resident)[^"]*)"\s*:\s*([0-9.]+)', blob, re.I)
print(f"  server-{i}: " + (", ".join(f"{k}={v}" for k, v in found[:8]) if found else "ok (see /status)"))
PY
done

echo
echo "== L2 on disk (./data/l2-server-N) =="
here="$(cd "$(dirname "$0")/.." && pwd)"   # repo root, one level up from scripts/
for i in $(seq 1 "$N"); do
  d="$here/data/l2-server-$i"
  [ -d "$d" ] || { echo "  server-$i: no $d"; continue; }
  # Logical = what the server believes it stored: every .data name at its full
  # size, following links. On disk = blocks actually allocated, shared content
  # counted once.
  find -L "$d" -maxdepth 1 -name '*.data' -printf '%s\n' 2>/dev/null \
    | awk -v s="$i" -v disk="$(du -sh "$d" | cut -f1)" \
          -v canon="$(find "$d/.dedup" -name '*.canon' 2>/dev/null | wc -l)" '
        { n++; b += $1 }
        END {
          split("B KiB MiB GiB TiB", u, " "); k = 1
          while (b >= 1024 && k < 5) { b /= 1024; k++ }
          printf "  server-%s: %d files, %.1f %s logical, %s on disk%s\n", s, n, b, u[k], disk,
                 (canon > 0 ? sprintf(" (dedup: %d canonical)", canon) : "")
        }'
done

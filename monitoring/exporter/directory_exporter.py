#!/usr/bin/env python3
"""Prometheus exporter for the LMCache coordinator's key directory.

The coordinator's own /metrics give fleet-wide totals by tier, but nothing per
server for L2 and nothing about time. This polls GET /directory/keys, which
lists every key with its placements, and publishes what can be derived from
watching that list: per-server placements and bytes, how long each placement
has existed, how many servers hold each key, access counts, and churn.

Ages are measured by this process: a placement's age is the time since the
exporter first saw it. Placements already present when the exporter starts are
aged from that moment, and a restart resets every age.

Stdlib only, so it runs in any Python image.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COORD = os.environ.get("COORDINATOR_URL", "http://coordinator:9300").rstrip("/")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "15"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "1000"))
PORT = int(os.environ.get("EXPORTER_PORT", "9105"))
# A placement must be absent from this many consecutive polls before it counts
# as removed: offset paging over a changing directory can skip an entry.
MISSING_POLLS = int(os.environ.get("MISSING_POLLS", "2"))

# Ordered, non-cumulative buckets: (label, upper bound in seconds).
AGE_BUCKETS = [("0-1m", 60), ("1-5m", 300), ("5-15m", 900), ("15-30m", 1800),
               ("30-60m", 3600), ("1-2h", 7200), ("2h+", float("inf"))]
ACCESS_BUCKETS = [("0", 0), ("1-2", 2), ("3-5", 5), ("6-10", 10), ("11+", float("inf"))]


def bucket(value, buckets):
    for label, upper in buckets:
        if value <= upper:
            return label
    return buckets[-1][0]


def get(path):
    with urllib.request.urlopen(COORD + path, timeout=10) as r:
        return json.load(r)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.first_seen = {}   # placement id -> time first seen
        self.missing = {}      # placement id -> consecutive polls absent
        self.labels = {}       # placement id -> (server, tier, backend)
        self.added = {}        # (server, tier) -> count
        self.removed = {}      # (server, tier) -> count
        self.primed = False    # first poll done; later arrivals count as added
        self.polls = 0
        self.errors = 0
        self.text = "# no successful poll yet\n"


def poll(state):
    t0 = time.time()
    servers = {i["instance_id"]: i.get("ip") or i["instance_id"]
               for i in get("/instances").get("instances", [])}

    seen = {}
    sizes = {}
    keys, tokens = 0, 0
    replicas, tier_sets, access = {}, {}, {}
    offset = 0
    while True:
        page = get(f"/directory/keys?limit={PAGE_SIZE}&offset={offset}")
        for k in page["keys"]:
            ident = k["key"]
            kid = (ident["chunk_hash_hex"], ident["model_name"], ident["kv_rank"],
                   ident["object_group_id"], ident["cache_salt"])
            placements = k.get("placements", [])
            if not placements:
                continue
            keys += 1
            tokens += k.get("num_tokens", 0)
            holders = {p["instance_id"] for p in placements}
            n = len(holders)
            r = str(n) if n < 3 else "3+"
            replicas[r] = replicas.get(r, 0) + 1
            ts = "+".join(sorted({p["tier"] for p in placements}))
            tier_sets[ts] = tier_sets.get(ts, 0) + 1
            a = bucket(k.get("access_count", 0), ACCESS_BUCKETS)
            access[a] = access.get(a, 0) + 1
            for p in placements:
                pid = kid + (p["instance_id"], p["tier"], p.get("backend", ""))
                seen[pid] = (servers.get(p["instance_id"], p["instance_id"]),
                             p["tier"], p.get("backend", ""))
                sizes[pid] = p.get("size_bytes", 0)
        offset += len(page["keys"])
        if not page["keys"] or offset >= page["total"]:
            break

    now = time.time()
    with state.lock:
        for pid, lbl in seen.items():
            if pid not in state.first_seen:
                state.first_seen[pid] = now
                state.labels[pid] = lbl
                if state.primed:
                    key = lbl[:2]
                    state.added[key] = state.added.get(key, 0) + 1
            state.missing.pop(pid, None)
        for pid in [p for p in state.first_seen if p not in seen]:
            state.missing[pid] = state.missing.get(pid, 0) + 1
            if state.missing[pid] >= MISSING_POLLS:
                key = state.labels[pid][:2]
                state.removed[key] = state.removed.get(key, 0) + 1
                del state.first_seen[pid], state.labels[pid], state.missing[pid]
        state.primed = True
        state.polls += 1

        count, nbytes, ages, oldest = {}, {}, {}, {}
        for pid, lbl in seen.items():
            count[lbl] = count.get(lbl, 0) + 1
            nbytes[lbl] = nbytes.get(lbl, 0) + sizes[pid]
            age = now - state.first_seen[pid]
            st = lbl[:2]
            ak = st + (bucket(age, AGE_BUCKETS),)
            ages[ak] = ages.get(ak, 0) + 1
            oldest[st] = max(oldest.get(st, 0.0), age)

        out = []

        def metric(name, kind, helptext, samples):
            out.append(f"# HELP {name} {helptext}")
            out.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                lbl = ",".join(f'{k}="{v}"' for k, v in labels.items())
                out.append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")

        P = "lmcache_directory_"
        metric(P + "keys", "gauge", "Keys in the coordinator directory with at least one placement.",
               [({}, keys)])
        metric(P + "tokens", "gauge", "Tokens covered by the keys in the directory.",
               [({}, tokens)])
        metric(P + "placements", "gauge", "Placements by server, tier and backend.",
               [({"server": s, "tier": t, "backend": b}, v) for (s, t, b), v in sorted(count.items())])
        metric(P + "placement_bytes", "gauge", "Logical bytes of placements by server, tier and backend.",
               [({"server": s, "tier": t, "backend": b}, v) for (s, t, b), v in sorted(nbytes.items())])
        age_samples = []
        for s, t in sorted({k[:2] for k in ages}):
            for label, _ in AGE_BUCKETS:   # emit zeros so stacked series do not vanish
                age_samples.append(({"server": s, "tier": t, "age": label}, ages.get((s, t, label), 0)))
        metric(P + "placements_by_age", "gauge",
               "Placements by how long this exporter has seen them (non-cumulative buckets).", age_samples)
        metric(P + "oldest_placement_age_seconds", "gauge", "Age of the longest-lived placement.",
               [({"server": s, "tier": t}, round(v, 1)) for (s, t), v in sorted(oldest.items())])
        metric(P + "keys_by_replicas", "gauge", "Keys by the number of servers holding them.",
               [({"replicas": r}, replicas.get(r, 0)) for r in ("1", "2", "3+")])
        metric(P + "keys_by_tiers", "gauge", "Keys by the set of tiers they are placed in.",
               [({"tiers": t}, v) for t, v in sorted(tier_sets.items())])
        metric(P + "keys_by_access_count", "gauge", "Keys by coordinator-reported access count.",
               [({"accesses": label}, access.get(label, 0)) for label, _ in ACCESS_BUCKETS])
        metric(P + "placements_added_total", "counter",
               "Placements that appeared after the exporter's first poll.",
               [({"server": s, "tier": t}, v) for (s, t), v in sorted(state.added.items())])
        metric(P + "placements_removed_total", "counter", "Placements that disappeared.",
               [({"server": s, "tier": t}, v) for (s, t), v in sorted(state.removed.items())])
        metric(P + "poll_duration_seconds", "gauge", "Time the last directory poll took.",
               [({}, round(time.time() - t0, 3))])
        metric(P + "last_poll_timestamp_seconds", "gauge", "When the last successful poll finished.",
               [({}, round(now, 1))])
        metric(P + "polls_total", "counter", "Successful directory polls.", [({}, state.polls)])
        metric(P + "poll_errors_total", "counter", "Failed directory polls.", [({}, state.errors)])
        state.text = "\n".join(out) + "\n"


def poll_loop(state):
    while True:
        start = time.time()
        try:
            poll(state)
        except Exception as e:   # coordinator down or mid-restart: keep serving the last view
            with state.lock:
                state.errors += 1
            print(f"poll failed: {e}", flush=True)
        time.sleep(max(1.0, POLL_INTERVAL - (time.time() - start)))


def main():
    state = State()
    threading.Thread(target=poll_loop, args=(state,), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urllib.parse.urlparse(self.path).path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            with state.lock:
                body = state.text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print(f"directory exporter: polling {COORD} every {POLL_INTERVAL:g}s, serving :{PORT}/metrics",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

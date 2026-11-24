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
# Where first-seen times are checkpointed. Without this every restart would
# reset each block to age zero, which is ruinous mid-experiment. Set empty to
# disable. Times are wall-clock, so they survive across processes.
STATE_PATH = os.environ.get("STATE_PATH", "/state/first_seen.json")
STATE_SAVE_INTERVAL = float(os.environ.get("STATE_SAVE_INTERVAL", "60"))
_SEP = "\x1f"

# Age bucket upper bounds in seconds, ascending, with an implicit +Inf band on
# the end. Override with AGE_BOUNDS as a comma-separated list of durations, e.g.
# AGE_BOUNDS=1m,5m,15m,1h for a short run or 1h,6h,1d,7d for a soak.
#
# These bounds are also published as a real Prometheus histogram
# (lmcache_directory_placement_age_seconds_bucket), which is what makes the
# *view* dynamic: quantiles and any banding can be computed at query time.
# Prefer that for new panels. The named buckets below are the readable form,
# and their labels are derived from the same bounds so the two cannot drift.
#
# Bounds are deliberately NOT auto-scaled to the observed age range: label
# values that change mid-run would churn the Prometheus series, and a stacked
# graph would silently mix datapoints that used different bucket definitions.
_DEFAULT_AGE_BOUNDS = "5m,30m,1h,2h,4h,8h"
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text):
    """Return seconds for a duration like '90s', '5m', '2h', '7d' or '300'."""
    text = text.strip().lower()
    if not text:
        raise ValueError("empty duration")
    if text[-1] in _UNITS:
        return float(text[:-1]) * _UNITS[text[-1]]
    return float(text)


def format_duration(seconds):
    """Render seconds compactly: 300 -> '5m', 3600 -> '1h', 86400 -> '1d'."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"


def make_age_buckets(bounds):
    """Return [(label, upper), ...] for ascending bounds, plus a +Inf band.

    Labels read as ranges, dropping a repeated unit on the low side:
    (300, 1800) -> '5-30m', (1800, 3600) -> '30m-1h'.
    """
    buckets = []
    low = 0.0
    for upper in bounds:
        lo_text = "0" if low == 0 else format_duration(low)
        hi_text = format_duration(upper)
        if lo_text != "0" and lo_text[-1] == hi_text[-1]:
            lo_text = lo_text[:-1]
        buckets.append((f"{lo_text}-{hi_text}", upper))
        low = upper
    buckets.append((f"{format_duration(low)}+", float("inf")))
    return buckets


AGE_BOUNDS = sorted({parse_duration(t)
                     for t in os.environ.get("AGE_BOUNDS", _DEFAULT_AGE_BOUNDS).split(",")
                     if t.strip()})
if not AGE_BOUNDS:
    raise SystemExit("AGE_BOUNDS must list at least one duration")
AGE_BUCKETS = make_age_buckets(AGE_BOUNDS)
ACCESS_BUCKETS = [("0", 0), ("1-2", 2), ("3-5", 5), ("6-10", 10), ("11+", float("inf"))]


def format_le(bound):
    """Render a bucket bound the way Prometheus expects an `le` label."""
    return f"{bound:g}"


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
        self.loaded = 0
        self.saved_at = 0.0
        self.text = "# no successful poll yet\n"

    def load(self):
        """Restore first-seen times from the checkpoint, if there is one."""
        if not STATE_PATH:
            return
        try:
            with open(STATE_PATH) as f:
                saved = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            print(f"ignoring unreadable state file {STATE_PATH}: {e}", flush=True)
            return
        for packed, (first, labels) in saved.get("placements", {}).items():
            pid = tuple(packed.split(_SEP))
            self.first_seen[pid] = first
            self.labels[pid] = tuple(labels)
        self.added = {tuple(k.split(_SEP)): v for k, v in saved.get("added", {}).items()}
        self.removed = {tuple(k.split(_SEP)): v for k, v in saved.get("removed", {}).items()}
        self.loaded = len(self.first_seen)
        # Anything already known is not a new arrival on the first poll back.
        self.primed = bool(self.first_seen)
        print(f"restored {self.loaded} placement ages from {STATE_PATH}", flush=True)

    def save(self):
        """Checkpoint first-seen times atomically. Caller must hold the lock."""
        if not STATE_PATH:
            return
        payload = {
            "placements": {_SEP.join(pid): [self.first_seen[pid], list(self.labels.get(pid, ()))]
                           for pid in self.first_seen},
            "added": {_SEP.join(k): v for k, v in self.added.items()},
            "removed": {_SEP.join(k): v for k, v in self.removed.items()},
        }
        tmp = STATE_PATH + ".tmp"
        try:
            os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, STATE_PATH)
            self.saved_at = time.time()
        except OSError as e:
            print(f"could not write state file {STATE_PATH}: {e}", flush=True)


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
            kid = (ident["chunk_hash_hex"], ident["model_name"], str(ident["kv_rank"]),
                   str(ident["object_group_id"]), ident["cache_salt"])
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
        if STATE_PATH and now - state.saved_at >= STATE_SAVE_INTERVAL:
            state.save()

        count, nbytes, ages, oldest = {}, {}, {}, {}
        hist, age_sum, age_count = {}, {}, {}
        for pid, lbl in seen.items():
            count[lbl] = count.get(lbl, 0) + 1
            nbytes[lbl] = nbytes.get(lbl, 0) + sizes[pid]
            age = now - state.first_seen[pid]
            st = lbl[:2]
            ak = st + (bucket(age, AGE_BUCKETS),)
            ages[ak] = ages.get(ak, 0) + 1
            oldest[st] = max(oldest.get(st, 0.0), age)
            # Per-bound tallies for the histogram; made cumulative when emitted.
            slot = next((i for i, b in enumerate(AGE_BOUNDS) if age <= b), len(AGE_BOUNDS))
            counts = hist.setdefault(st, [0] * (len(AGE_BOUNDS) + 1))
            counts[slot] += 1
            age_sum[st] = age_sum.get(st, 0.0) + age
            age_count[st] = age_count.get(st, 0) + 1

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
        # A real cumulative histogram: lets Grafana compute quantiles and any
        # banding at query time, instead of being fixed to the named buckets.
        bucket_samples = []
        for (srv, tier), counts in sorted(hist.items()):
            running = 0
            for i, bound in enumerate(AGE_BOUNDS):
                running += counts[i]
                bucket_samples.append(
                    ({"server": srv, "tier": tier, "le": format_le(bound)}, running))
            bucket_samples.append(
                ({"server": srv, "tier": tier, "le": "+Inf"}, running + counts[-1]))
        metric(P + "placement_age_seconds_bucket", "counter",
               "Cumulative count of placements by age, for histogram_quantile "
               "and query-time banding. Not monotonic: it is a live snapshot of "
               "what exists now, not a running total.", bucket_samples)
        metric(P + "placement_age_seconds_sum", "counter",
               "Summed age of current placements.",
               [({"server": s_, "tier": t_}, round(v, 1)) for (s_, t_), v in sorted(age_sum.items())])
        metric(P + "placement_age_seconds_count", "counter",
               "Number of placements behind the age histogram.",
               [({"server": s_, "tier": t_}, v) for (s_, t_), v in sorted(age_count.items())])
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
        metric(P + "ages_restored", "gauge",
               "Placement ages restored from the state file at startup.",
               [({}, state.loaded)])
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
    state.load()
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

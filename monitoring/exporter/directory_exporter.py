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

Departed instances. An MP server mints a new instance id every time it starts.
When one leaves, the coordinator fences its L1 placements but deliberately
keeps its L2 ones: L2 is storage that outlives the process, and those leave
only through DELETE events. So the directory carries L2 placements owned by ids
that no longer exist, and nothing will ever delete them. The per-server metrics
here cover registered instances only; what departed ones left behind is
published separately as lmcache_directory_departed_*, under the server name the
instance had while it was registered, so a restart does not add a series.

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
# How many of the most-reused keys to publish individually, with their hash,
# where they sit and how old they are. Everything else here is an aggregate,
# because a per-key series for a directory of this size would be a cardinality
# disaster; this is bounded at N, but the hash and location labels still churn
# as the ranking moves, so keep it small. 0 turns it off.
TOP_REUSED = int(os.environ.get("TOP_REUSED", "10"))
# Where first-seen times are checkpointed. Without this every restart would
# reset each block to age zero, which is ruinous mid-experiment. Set empty to
# disable. Times are wall-clock, so they survive across processes.
STATE_PATH = os.environ.get("STATE_PATH", "/state/first_seen.json")
STATE_SAVE_INTERVAL = float(os.environ.get("STATE_SAVE_INTERVAL", "60"))
_SEP = "\x1f"
# Server label for a departed instance this exporter never saw registered, so
# it has no name for it. One fixed value rather than the raw instance id: ids
# are unbounded across restarts, and a label that grows with every restart is
# the problem the departed metrics exist to avoid.
UNKNOWN_SERVER = "unknown"

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
        self.names = {}        # instance id -> server name it registered under
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
        # Absent from checkpoints written before departed instances were
        # tracked; those instances read as UNKNOWN_SERVER until they age out.
        self.names = dict(saved.get("names", {}))
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
            "names": self.names,
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
    live = {i["instance_id"]: i.get("ip") or i["instance_id"]
            for i in get("/instances").get("instances", [])}
    # Every name ever seen registered, so a departed instance keeps the one it
    # had. Only this thread writes state.names, so reading it unlocked is safe.
    names = {**state.names, **live}

    seen = {}
    sizes = {}
    keys, tokens, departed_only = 0, 0, 0
    replicas, tier_sets, access = {}, {}, {}
    departed_pids, departed_ids = set(), set()
    departed_count, departed_bytes = {}, {}
    # Only the current top few are kept, but the ranking is not known until the
    # last page has been read, so every key's hash, holders and pids are held
    # until then. Dropped as soon as the top N is picked.
    reuse = []
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
            a = bucket(k.get("access_count", 0), ACCESS_BUCKETS)
            access[a] = access.get(a, 0) + 1
            # Replication and tier sets describe the registered fleet. A copy
            # left by a departed instance is not a server holding the key, and
            # counting it made every restarted server look like a replica of
            # its own previous run.
            held = [p for p in placements if p["instance_id"] in live]
            if held:
                n = len({p["instance_id"] for p in held})
                r = str(n) if n < 3 else "3+"
                replicas[r] = replicas.get(r, 0) + 1
                ts = "+".join(sorted({p["tier"] for p in held}))
                tier_sets[ts] = tier_sets.get(ts, 0) + 1
            else:
                departed_only += 1
            if TOP_REUSED and held:
                reuse.append((k.get("access_count", 0), ident["chunk_hash_hex"],
                              sorted({(live[p["instance_id"]], p["tier"]) for p in held}),
                              [kid + (p["instance_id"], p["tier"], p.get("backend", ""))
                               for p in held]))
            for p in placements:
                inst, tier, backend = p["instance_id"], p["tier"], p.get("backend", "")
                pid = kid + (inst, tier, backend)
                if inst in live:
                    seen[pid] = (live[inst], tier, backend)
                    sizes[pid] = p.get("size_bytes", 0)
                else:
                    lbl = (names.get(inst, UNKNOWN_SERVER), tier, backend)
                    departed_pids.add(pid)
                    departed_ids.add(inst)
                    departed_count[lbl] = departed_count.get(lbl, 0) + 1
                    departed_bytes[lbl] = departed_bytes.get(lbl, 0) + p.get("size_bytes", 0)
        offset += len(page["keys"])
        if not page["keys"] or offset >= page["total"]:
            break

    now = time.time()
    with state.lock:
        # Forget a name once its instance is neither registered nor holding
        # anything, or the map would grow by one entry per restart for good.
        state.names = {i: n for i, n in names.items() if i in live or i in departed_ids}
        for pid, lbl in seen.items():
            if pid not in state.first_seen:
                state.first_seen[pid] = now
                state.labels[pid] = lbl
                if state.primed:
                    key = lbl[:2]
                    state.added[key] = state.added.get(key, 0) + 1
            state.missing.pop(pid, None)
        for pid in [p for p in state.first_seen if p not in seen]:
            if pid in departed_pids:
                # The instance left; the placement is still in the directory.
                # It moves to the departed tallies without counting as
                # removed, so the removal counters keep meaning one thing: a
                # block the directory no longer lists, which for L2 is an
                # eviction or a delete.
                del state.first_seen[pid], state.labels[pid]
                state.missing.pop(pid, None)
                continue
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
        metric(P + "keys_by_replicas", "gauge",
               "Keys by the number of registered servers holding them.",
               [({"replicas": r}, replicas.get(r, 0)) for r in ("1", "2", "3+")])
        metric(P + "keys_by_tiers", "gauge",
               "Keys by the set of tiers registered servers hold them in.",
               [({"tiers": t}, v) for t, v in sorted(tier_sets.items())])
        metric(P + "departed_instances", "gauge",
               "Instances no longer registered that still own placements in the directory.",
               [({}, len(departed_ids))])
        metric(P + "departed_placements", "gauge",
               "Placements owned by instances that are no longer registered, under the "
               "server name the instance had. Nothing will delete these.",
               [({"server": s, "tier": t, "backend": b}, v)
                for (s, t, b), v in sorted(departed_count.items())])
        metric(P + "departed_placement_bytes", "gauge",
               "Logical bytes of the placements owned by departed instances.",
               [({"server": s, "tier": t, "backend": b}, v)
                for (s, t, b), v in sorted(departed_bytes.items())])
        metric(P + "keys_departed_only", "gauge",
               "Keys whose every placement is owned by a departed instance.",
               [({}, departed_only)])
        # The most-reused keys, named individually: hash, where the copies sit,
        # how old the oldest copy is. Ties are broken by hash so the ranking is
        # stable between polls rather than shuffling among equal counts.
        top = sorted(reuse, key=lambda r: (-r[0], r[1]))[:TOP_REUSED]
        rank_samples, rank_ages = [], []
        for position, (accesses, chunk_hash, places, pids) in enumerate(top, 1):
            labels = {
                "rank": f"{position:02d}",
                "hash": chunk_hash[:16],
                "location": ", ".join(f"{s} {t}" for s, t in places),
            }
            rank_samples.append((labels, accesses))
            ages_seen = [now - state.first_seen[p] for p in pids if p in state.first_seen]
            rank_ages.append((labels, round(max(ages_seen), 1) if ages_seen else 0))
        metric(P + "top_reused_accesses", "gauge",
               "Access count of the most-reused keys, by hash and where they sit. "
               "Rank is zero-padded so it sorts as a string.", rank_samples)
        metric(P + "top_reused_age_seconds", "gauge",
               "Age of the oldest copy of each of those keys, labelled "
               "identically so the two can be shown as one table.", rank_ages)
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

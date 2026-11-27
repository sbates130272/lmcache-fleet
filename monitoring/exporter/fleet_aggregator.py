#!/usr/bin/env python3
"""Fleet aggregator: one summary for everything beneath it.

LMCache v0.5.8 has no coordinator of coordinators. A fleet too big for one
coordinator is dealt across several, each of which tracks its own servers, and
nothing above them can say how many distinct blocks the fleet holds, which are
reused most, or which coordinator to ask about one.

This collects the summary each child publishes at /summary, merges them (see
fleet_summary.py for why that needs no keys), and publishes the result three
ways:

  /summary   the merged summary, in the format it was read in. A child can be a
             directory exporter or another aggregator, and this one can be a
             child in turn, so aggregators stack to any depth; each level holds
             a bounded summary per child however many servers lie beneath it.
  /metrics   the same for Prometheus, as lmcache_fleet_*.
  /locate?hash=<chunk hash>
             which coordinators may hold a block. Each child's filter is kept
             apart, so the question goes only to the children that could have
             it and is passed down until it reaches coordinators: one request
             per level rather than one per coordinator. An answer is a hint to
             check with that coordinator, as every directory answer is.

Children are polled, not pushed to: a coordinator has no way to report upward,
and a pull keeps a slow or absent child from costing anything but its own
slice of the picture. A child that stops answering keeps its last summary and
is counted as stale.

Standard library only, so it runs in the exporter's bare Python image.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fleet_summary

NAME = os.environ.get("AGGREGATOR_NAME", "aggregator")
LEVEL = int(os.environ.get("AGGREGATOR_LEVEL", "1"))
CHILDREN = [u.strip().rstrip("/") for u in os.environ.get("CHILDREN", "").split(",") if u.strip()]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
PORT = int(os.environ.get("AGGREGATOR_PORT", "9106"))
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "30"))
# A child is stale once it has gone this long without a successful read. Its
# last summary is still merged: an old slice is a better answer than a hole.
STALE_AFTER = float(os.environ.get("STALE_AFTER", str(max(4 * POLL_INTERVAL, 120.0))))
# Entries kept when merging the children's most-reused lists, and how many of
# the result are published as individual Prometheus series. The first is long
# on purpose: a block is only found if it made some child's list.
TOP_KEPT = int(os.environ.get("SUMMARY_TOP", str(fleet_summary.TOP_N)))
TOP_PUBLISHED = int(os.environ.get("TOP_REUSED", "10"))


def fetch(url, timeout=FETCH_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.children = {}     # url -> (summary, time last read successfully)
        self.failing = {}      # url -> consecutive failed reads
        self.merged = None
        self.wire = b""
        self.text = "# no successful poll yet\n"
        self.polls = 0


def poll(state):
    started = time.time()
    for url in CHILDREN:
        try:
            summary = fleet_summary.decode(fetch(url + "/summary"))
        except Exception as e:      # down, starting, or not polled yet: keep the last one
            state.failing[url] = state.failing.get(url, 0) + 1
            if state.failing[url] in (1, 10) or state.failing[url] % 100 == 0:
                print(f"{url}: {type(e).__name__}: {e} "
                      f"({state.failing[url]} failed read(s))", flush=True)
            continue
        state.failing.pop(url, None)
        state.children[url] = (summary, time.time())

    now = time.time()
    known, stale_children = [], 0
    for url in CHILDREN:
        if url not in state.children:
            stale_children += 1
            continue
        summary, read_at = state.children[url]
        if now - read_at > STALE_AFTER:
            stale_children += 1
            # Everything beneath a stale child is stale, whatever it reported.
            summary = {**summary, "coordinators_stale": summary["coordinators"]}
        known.append(summary)
    merged = fleet_summary.merge(known, NAME, LEVEL, TOP_KEPT, now)
    bloom = merged["bloom"]

    out = []

    def metric(name, kind, helptext, samples):
        out.append(f"# HELP {name} {helptext}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lbl = ",".join(f'{k}="{v}"' for k, v in labels.items())
            out.append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")

    P = "lmcache_fleet_"
    metric(P + "coordinators", "gauge", "Coordinators summarized beneath this aggregator.",
           [({}, merged["coordinators"])])
    metric(P + "coordinators_stale", "gauge",
           "Coordinators whose summary has stopped being refreshed somewhere beneath.",
           [({}, merged["coordinators_stale"])])
    metric(P + "servers", "gauge", "Servers registered with those coordinators.",
           [({}, merged["servers"])])
    metric(P + "blocks_distinct", "gauge",
           "Distinct blocks beneath this aggregator, by where they are held: a block "
           "under several coordinators counts once. Estimated from merged sketches.",
           [({"place": place}, sketch.count()) for place, sketch in merged["sketches"].items()])
    metric(P + "blocks_sum", "gauge",
           "Blocks as the coordinators count them, added up: a block under several "
           "counts once for each.", [({}, merged["blocks_sum"])])
    metric(P + "blocks_duplicated", "gauge",
           "Copies of blocks beyond the first coordinator to hold them: blocks_sum "
           "less the distinct count, so it carries the sketch's error.",
           [({}, max(0, merged["blocks_sum"] - merged["blocks"]))])
    for field, name, helptext in (
            ("placements", "placements", "Placements beneath this aggregator."),
            ("bytes", "placement_bytes", "Logical bytes of those placements.")):
        metric(P + name, "gauge", helptext,
               [({"tier": where.split("|")[0], "backend": where.split("|")[1]}, value)
                for where, value in sorted(merged[field].items())])
    top = merged["top"][:TOP_PUBLISHED]
    metric(P + "top_reused_accesses", "gauge",
           "Access count of the most reused blocks beneath this aggregator, summed over "
           "the coordinators that hold them. Rank is zero-padded so it sorts as a string.",
           [({"rank": f"{rank:02d}", "hash": entry["hash"][:16], "pods": entry["pods"]},
             entry["accesses"]) for rank, entry in enumerate(top, 1)])
    metric(P + "summary_age_seconds", "gauge",
           "Age of the oldest coordinator summary merged in: how stale the picture can be.",
           [({}, round(now - merged["ts_oldest"], 1))])
    metric(P + "children", "gauge", "Children this aggregator collects from.",
           [({}, len(CHILDREN))])
    metric(P + "children_stale", "gauge",
           "Children with no summary yet, or none read within STALE_AFTER.",
           [({}, stale_children)])
    metric(P + "locate_filter_fill", "gauge",
           "Fraction of the merged filter's bits that are set. Raised to its k it is "
           "the chance /locate names a place that does not hold the block.",
           [({}, round(bloom.fill(), 4))])
    wire = fleet_summary.encode(merged)
    metric(P + "summary_bytes", "gauge", "Size of the merged summary as served.",
           [({}, len(wire))])
    metric(P + "poll_duration_seconds", "gauge", "Time the last collect-and-merge took.",
           [({}, round(time.time() - started, 3))])
    metric(P + "polls_total", "counter", "Collect-and-merge passes.", [({}, state.polls + 1)])

    with state.lock:
        state.merged, state.wire = merged, wire
        state.text = "\n".join(out) + "\n"
        state.polls += 1


def locate(state, wanted):
    """Coordinators that may hold a block, asked only of children that could."""
    h = fleet_summary.block_hash(wanted)
    with state.lock:
        children = dict(state.children)
    candidates, asked, unreachable = [], 0, []
    for url, (summary, _read_at) in children.items():
        if h not in summary["bloom"]:
            continue
        asked += 1
        if summary["level"] == 0 and summary.get("coordinator"):
            candidates.append({"source": summary["source"],
                               "coordinator": summary["coordinator"]})
            continue
        try:
            below = json.loads(fetch(f"{url}/locate?hash={urllib.parse.quote(wanted)}"))
        except Exception:
            unreachable.append(url)
            continue
        candidates.extend(below.get("candidates", []))
        unreachable.extend(below.get("unreachable", []))
    return {"hash": wanted, "candidates": candidates, "children": len(children),
            "children_asked": asked, "unreachable": unreachable}


def poll_loop(state):
    while True:
        started = time.time()
        try:
            poll(state)
        except Exception as e:
            print(f"poll failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(max(1.0, POLL_INTERVAL - (time.time() - started)))


def main():
    if not CHILDREN:
        raise SystemExit("CHILDREN is empty: give this aggregator the URLs it collects from")
    state = State()
    threading.Thread(target=poll_loop, args=(state,), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            content_type = "application/json"
            with state.lock:
                ready, wire, text = state.merged is not None, state.wire, state.text
            if url.path == "/metrics":
                body, content_type = text.encode(), "text/plain; version=0.0.4; charset=utf-8"
            elif url.path == "/summary" and ready:
                body = wire
            elif url.path == "/locate" and ready:
                wanted = urllib.parse.parse_qs(url.query).get("hash", [""])[0]
                body = json.dumps(locate(state, wanted) if wanted else
                                  {"hash": "", "candidates": []}).encode()
            else:
                self.send_response(503 if url.path in ("/summary", "/locate") else 404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print(f"fleet aggregator {NAME} (level {LEVEL}): collecting from {len(CHILDREN)} "
          f"children every {POLL_INTERVAL:g}s, serving :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

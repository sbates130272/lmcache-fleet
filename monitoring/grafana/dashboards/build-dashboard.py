#!/usr/bin/env python3
"""Emit the LMCache fleet Grafana dashboard JSON (run once; output is committed)."""
import json

DS = {"type": "prometheus", "uid": "lmcache-prom"}

# dataviz categorical palette, dark steps, fixed order (never cycled).
SERVER_HUES = ["#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#0ca30c", "#9085e9", "#e66767"]
L1, L2 = "#3987e5", "#d95926"

# The coordinator labels servers by UUID; the servers' own target_info carries
# that same UUID next to the scrape's friendly `server` label. Join through it
# so coordinator-sourced panels read as lmcache-server-N instead of a UUID.
#
# The sum is what keeps a restart from splitting the line. A server mints a new
# UUID each time it starts, so the join yields one series per run, all sharing
# a `server` value: the legend showed the same name once per restart. Summing
# over the UUID folds the runs into one series per server.
COORD_PER_SERVER = (
    "sum by (server) ("
    "lmcache_coordinator_key_directory_server_l1_keys"
    " * on(instance_id) group_left(server) "
    'label_replace(target_info{job="lmcache-servers",server=~"$server"},'
    '"instance_id","$1","service_instance_id","(.*)")'
    ")"
)

def color(name, hexv):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hexv}}]}

SERVER_OVERRIDES = [color(f"lmcache-server-{i+1}", h) for i, h in enumerate(SERVER_HUES)]
T1 = "L1 (CPU DRAM)"
T2 = "L2 (Filesystem)"
TIER_OVERRIDES = [color(T1, L1), color(T2, L2)]

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

# Ordered buckets get one hue. On the dark surface the low end sits closest to
# the background and the high end is lightest, so the oldest / most accessed
# bucket stands out. Only ramp steps that keep contrast against the surface.
SEQ_BLUES = ["#256abf", "#2a78d6", "#3987e5", "#6da7ec", "#86b6ef", "#9ec5f4", "#cde2fb"]
# Keep in step with AGE_BUCKETS in monitoring/exporter/directory_exporter.py.
AGE_LABELS = ["0-5m", "5-30m", "30m-1h", "1-2h", "2-4h", "4-8h", "8h+"]
# strict: the comment above asks these to stay in step with the exporter's
# bands, and a silent zip would just drop the band nobody gave a colour.
AGE_OVERRIDES = [color(a, c) for a, c in zip(AGE_LABELS, SEQ_BLUES, strict=True)]


# One target per band rather than a single `sum by (age)`. Grafana sorts the
# series of one query by label, and these labels do not sort into their own
# order: "1-2h" lands before "30m-1h" and "5-30m", so the legend read hours
# before minutes. Series follow target order, so listing the bands explicitly
# is what puts them in age order. The same applies to any ordered bucket label
# -- "11+" sorts before "3-5" in the access counts.
def bucket_targets(metric, label, values, selector="", per_server=True):
    # per_server=False for the fleet-wide metrics the exporter publishes with
    # no server label at all: filtering them by $server would match nothing.
    srv = ',server=~"$server"' if per_server else ""
    return [(f'sum (lmcache_directory_{metric}{{{selector}{label}="{v}"{srv}}})', v)
            for v in values]


def age_targets(tier):
    return bucket_targets("placements_by_age", "age", AGE_LABELS,
                          selector=f'tier="{tier}",')


ACCESS_LABELS = ["0", "1-2", "3-5", "6-10", "11+"]
ACCESS_OVERRIDES = [color(a, SEQ_BLUES[i])
                    for a, i in zip(ACCESS_LABELS, (0, 2, 3, 5, 6), strict=True)]
REPLICA_OVERRIDES = [color("1 Server", "#3987e5"), color("2 Servers", "#d95926"),
                     color("3+ Servers", "#199e70")]
CHURN_OVERRIDES = [color(f"{T1} Added", "#3987e5"), color(f"{T2} Added", "#d95926"),
                   color(f"{T1} Removed", "#199e70"), color(f"{T2} Removed", "#c98500")]


def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}


def stat(title, expr, unit, x, y, w=6, h=4, desc=""):
    return {
        "id": nid(), "type": "stat", "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": "text"},
                                     "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]}},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "orientation": "auto", "textMode": "auto", "colorMode": "none",
                    "graphMode": "area", "justifyMode": "auto"},
        "targets": [{"refId": "A", "datasource": DS, "expr": expr, "instant": False}],
    }

def table(title, targets, x, y, w=24, h=11, desc="", sort_by=None,
          rename=None, units=None, widths=None, order=None):
    """A table of current values, one row per series.

    Instant queries in table format, so each series' labels become columns and
    its value the last column. Several queries are merged into one table by
    their shared label columns, which is how two metrics with identical labels
    -- a count and an age, say -- end up as two columns of one row.
    """
    rename = rename or {}
    overrides = [{"matcher": {"id": "byName", "options": name},
                  "properties": [{"id": "unit", "value": unit}]}
                 for name, unit in (units or {}).items()]
    overrides += [{"matcher": {"id": "byName", "options": name},
                   "properties": [{"id": "custom.width", "value": width}]}
                  for name, width in (widths or {}).items()]
    return {
        "id": nid(), "type": "table", "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                    "inspect": False},
                         "color": {"mode": "thresholds"},
                         "thresholds": {"mode": "absolute",
                                        "steps": [{"color": "text", "value": None}]}},
            "overrides": overrides,
        },
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                               "fields": ""},
                    "sortBy": ([{"displayName": sort_by, "desc": False}]
                               if sort_by else [])},
        "transformations": [
            # Merge first: it is what folds the per-metric frames into single
            # rows on their common label columns.
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "renameByName": rename,
                # Keyed by the ORIGINAL field name, before renameByName.
                "indexByName": {n: i for i, n in enumerate(order or [])}}},
        ],
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e,
                     "format": "table", "instant": True, "legendFormat": lf}
                    for i, (e, lf) in enumerate(targets)],
    }


def heatmap(title, expr, x, y, w=12, h=11, desc=""):
    """Age distribution over time, bucketed at query time from the histogram.

    Reads the exporter's cumulative histogram, so the bands shown follow the
    exporter's AGE_BOUNDS without the dashboard hardcoding them.
    """
    return {
        "id": nid(), "type": "heatmap", "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {
            "calculate": False,
            "cellGap": 2,
            "cellValues": {"unit": "short"},
            "color": {"mode": "scheme", "scheme": "Blues", "steps": 64,
                      "reverse": False, "exponent": 0.5, "fill": "#3987e5"},
            "filterValues": {"le": 1e-9},
            "legend": {"show": True},
            "rowsFrame": {"layout": "auto", "value": "blocks"},
            "tooltip": {"mode": "single", "yHistogram": True, "showColorScale": True},
            "yAxis": {"axisPlacement": "left", "unit": "s", "reverse": False},
        },
        "targets": [{"refId": "A", "datasource": DS, "expr": expr,
                     "format": "heatmap", "legendFormat": "{{le}}"}],
    }


# Status roles, fixed and reserved: never a series colour. Each tile also says
# its state in words or a number, so the fill never carries the meaning alone.
GOOD, WARNING, CRITICAL = "#0aac23", "#fbb315", "#dc3c3d"


def steps(*pairs):
    """Threshold steps from (colour, from-value) pairs; the first starts at -inf."""
    return [{"color": c, "value": v} for c, v in pairs]


def health(title, expr, unit, x, y, thresholds, mappings=None, w=3, h=4, desc=""):
    """One current value on a status fill: a health check read at a glance.

    Instant, so a tile is what is true now rather than the last sample of a
    range, and a series that has stopped being reported reads as no data
    instead of holding its last value.
    """
    return {
        "id": nid(), "type": "stat", "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {"unit": unit, "noValue": "no data",
                         "color": {"mode": "thresholds"},
                         "thresholds": {"mode": "absolute", "steps": thresholds},
                         "mappings": [{"type": "value", "options": {
                             str(value): {"text": text, "index": i}
                             for i, (value, text) in enumerate((mappings or {}).items())}}]
                         if mappings else []},
            "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "orientation": "auto", "textMode": "value", "colorMode": "background",
                    "graphMode": "none", "justifyMode": "center"},
        "targets": [{"refId": "A", "datasource": DS, "expr": expr, "instant": True}],
    }


def counts(title, parts, x, y, w=3, h=4, desc=""):
    """Two or more named counts in one tile, each on its own status fill.

    For a pair that only means something together -- how many are up beside how
    many are down -- where a tile each would spend the row's width on one fact.
    `parts` is (expr, name, threshold steps); the name is shown with the value.
    """
    tile = health(title, parts[0][0], "short", x, y, parts[0][2], w=w, h=h, desc=desc)
    tile["targets"] = [{"refId": chr(65 + i), "datasource": DS, "expr": expr,
                        "instant": True, "legendFormat": name}
                       for i, (expr, name, _) in enumerate(parts)]
    tile["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": name},
         "properties": [{"id": "thresholds", "value": {"mode": "absolute", "steps": thresholds}}]}
        for _, name, thresholds in parts]
    tile["options"].update(textMode="value_and_name", orientation="vertical")
    return tile


# gen_compose.py publishes the run's settings as the labels of one constant
# series (see RUN_INFO there). Named here so a label added there without a row
# here simply does not show, rather than appearing under its raw name.
RUN_ROWS = [("fleet", "Fleet"), ("l1", "L1"), ("l2_local", "L2, Local to a Server"),
            ("l2_shared", "L2, Shared between Servers"), ("l2_sim", "L2 Simulation"),
            ("workload", "Workload"), ("image", "Image")]


def run_table(x, y, w=24, h=9):
    """The run's settings as two columns: what, and how it is set.

    One instant sample in table form is a single row whose columns are the
    labels; transposing it gives a row per setting, which is the readable way
    round for seven sentences. Transpose spends the first column on naming the
    new ones, so __name__ is kept and put first to be the one it spends.
    """
    return {
        "id": nid(), "type": "table", "title": "This Run",
        "description": "What the fleet on this dashboard was started with, as "
                       "gen_compose.py generated it: tier sizes and eviction, what "
                       "the L2 simulator is doing, and the workload. Read from the "
                       "labels of lmcache_fleet_run_info, a constant Prometheus "
                       "records from a generated rule, so it is here whether or not "
                       "the coordinator and the directory exporter are keeping up.",
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                    "inspect": False},
                         "color": {"mode": "thresholds"},
                         "thresholds": {"mode": "absolute",
                                        "steps": [{"color": "text", "value": None}]}},
            "overrides": [{"matcher": {"id": "byName", "options": "Setting"},
                           "properties": [{"id": "custom.width", "value": 240}]}]},
        "options": {"showHeader": False, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                               "fields": ""}},
        "transformations": [
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Value": True},
                "renameByName": dict(RUN_ROWS),
                "indexByName": {"__name__": 0,
                                **{name: i for i, (name, _) in enumerate(RUN_ROWS, 1)}}}},
            {"id": "transpose", "options": {"firstFieldName": "Setting",
                                            "restFieldsName": "Value"}},
        ],
        "targets": [{"refId": "A", "datasource": DS, "expr": "lmcache_fleet_run_info",
                     "format": "table", "instant": True}],
    }


# Grafana unit ids: 'bytes' is bytes(IEC) and renders B/KiB/MiB/GiB. The
# lowercase-b suffixes come from the 'bits'/'decbits' units, which this
# dashboard never uses.
def ts(title, targets, unit, x, y, w=12, h=8, overrides=(), stacked=False,
       fill=0, desc="", maxv=None, thresholds=None):
    custom = {
        "drawStyle": "line", "lineWidth": 2, "fillOpacity": fill,
        "showPoints": "never", "spanNulls": True, "gradientMode": "none",
        "lineInterpolation": "linear", "pointSize": 8,
        "axisBorderShow": False, "axisLabel": "",
        "scaleDistribution": {"type": "linear"},
        "hideFrom": {"legend": False, "tooltip": False, "viz": False},
    }
    if stacked:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
                "min": 0}
    if maxv is not None:
        defaults["max"] = maxv
    if thresholds:
        defaults["thresholds"] = thresholds
        custom["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "id": nid(), "type": "timeseries", "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": defaults, "overrides": list(overrides)},
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom",
                       "showLegend": True,
                       "calcs": ["lastNotNull", "mean", "max"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [{"refId": chr(65 + i), "datasource": DS,
                     "expr": e, "legendFormat": lf} for i, (e, lf) in enumerate(targets)],
    }

panels = [
    # Declared y only orders the bands (see relayout), so the top section sits
    # at negative values rather than renumbering everything under it.
    row("This Run", -40),
    run_table(0, -30),
    health("Servers Up", 'count(up{job="lmcache-servers"} == 1) or vector(0)', "short", 0, -20,
           steps((CRITICAL, None), (GOOD, 1)),
           desc="MP servers Prometheus scraped successfully on its last pass. "
                "Compare with the fleet size in the table above."),
    health("Servers Down", 'count(up{job="lmcache-servers"} == 0) or vector(0)', "short", 3, -20,
           steps((GOOD, None), (CRITICAL, 1)), mappings={0: "None"},
           desc="MP servers whose last scrape failed: stopped, crashed, or too "
                "busy to answer within the scrape timeout."),
    counts("Coordinators",
           [('count(up{job="lmcache-coordinator"} == 1) or vector(0)', "Up",
             steps((CRITICAL, None), (GOOD, 1))),
            ('count(up{job="lmcache-coordinator"} == 0) or vector(0)', "Down",
             steps((GOOD, None), (CRITICAL, 1)))],
           6, -20,
           desc="How many coordinators Prometheus could scrape, and how many it "
                "could not. Each tracks only the servers dealt to it, so one "
                "down means that many servers' blocks are untracked, not that "
                "the fleet is. A coordinator that is up can still be too slow to "
                "be useful: see the three tiles to the right, which show the "
                "worst of them."),
    health("Coordinator Busy",
           "max(rate(lmcache_coordinator_event_loop_blocked_seconds_total[2m]))",
           "percentunit", 9, -20,
           steps((GOOD, None), (WARNING, 0.2), (CRITICAL, 0.5)),
           desc="Share of the last two minutes the busiest coordinator's event "
                "loop was blocked. A coordinator is one process, and it decodes and applies cache "
                "events on that loop, so this is how close it is to its limit: "
                "while blocked it answers no heartbeat, event or directory "
                "request. Past half, servers time out and drop events."),
    health("Event Delay",
           "max(lmcache_coordinator_ingest_server_event_delivery_delay_seconds)", "s", 12, -20,
           steps((GOOD, None), (WARNING, 3), (CRITICAL, 6)),
           desc="Longest time, across servers, between a cache event being "
                "recorded and the coordinator applying it. Servers flush about "
                "once a second and give a post two seconds, so a delay well past "
                "that means posts are queueing behind a busy coordinator."),
    health("Events Dropped/s",
           "sum(rate(lmcache_coordinator_ingest_events_dropped_by_servers_total[2m]))", "short",
           15, -20, steps((GOOD, None), (CRITICAL, 0.001)), mappings={0: "None"},
           desc="Cache events servers gave up delivering, as they report it. A "
                "failed post is not retried, so each one is a store, access or "
                "eviction the key directory never hears about."),
    health("Servers with Gaps",
           "sum(lmcache_coordinator_ingest_server_view_incomplete) or vector(0)", "short", 18, -20,
           steps((GOOD, None), (CRITICAL, 1)), mappings={0: "None"},
           desc="Servers whose event stream reached the coordinator with batches "
                "missing. The directory's picture of such a server is incomplete "
                "until it restarts, so every panel built on the directory "
                "under-reports it."),
    health("Directory Poll Age",
           "time() - min(lmcache_directory_last_poll_timestamp_seconds)", "s", 21, -20,
           steps((GOOD, None), (WARNING, 600), (CRITICAL, 1800)),
           desc="Time since the stalest directory exporter last read its whole key "
                "directory. The inventory panels further down are as stale as "
                "this. Amber from ten minutes, so a DIRECTORY_POLL of five is "
                "still green between polls. It pages the directory a thousand keys at a time, so on a "
                "large one a poll can outlast its timeout and never finish."),
    row("Fleet State (Coordinator and Servers)", -10),
    stat("Keys Tracked", "sum(lmcache_coordinator_key_directory_keys_tracked)", "short", 0, 0,
         desc="Unique keys the coordinator's directory holds at least one placement for. "
              "Summed over coordinators when there are several, and each tracks only "
              "its own servers, so a key held under two of them counts twice."),
    stat("Servers Registered", "sum(lmcache_coordinator_registry_servers_registered)", "short", 6, 0,
         desc="MP servers currently heartbeating to the coordinator."),
    stat("L1 Placements", 'sum(lmcache_coordinator_key_directory_placements{tier="l1"})', "short", 12, 0,
         desc="Key copies resident in some server's L1 DRAM tier."),
    stat("L2 Placements", 'sum(lmcache_coordinator_key_directory_placements{tier="l2"})', "short", 18, 0,
         desc="Key copies written through to an L2 backend. Counts only what was "
              "written while this coordinator has been up: nothing scans L2 at "
              "startup, so .data files left by an earlier run read as 0 here."),

    ts("Fleet Keys by Tier",
       [('sum(lmcache_coordinator_key_directory_placements{tier="l1"})', T1),
        ('sum(lmcache_coordinator_key_directory_placements{tier="l2"})', T2)],
       "short", 0, 4, stacked=True, fill=60, overrides=TIER_OVERRIDES,
       desc="Coordinator's own view. A placement is one place a key is stored: "
            "L1 on one server, or one L2 backend. Stacked, so total height is "
            "every copy the fleet holds."),
    ts("Fleet Bytes by Tier",
       [('sum(lmcache_coordinator_usage_bytes_stored{tier="l1"})', T1),
        ('sum(lmcache_coordinator_usage_bytes_stored{tier="l2"})', T2)],
       "bytes", 12, 4, stacked=True, fill=60, overrides=TIER_OVERRIDES,
       desc="Bytes the fleet reports holding, as aggregated by the coordinator."),

    ts("L1 Keys per Server — Coordinator's View",
       [(COORD_PER_SERVER, "{{server}}")],
       "short", 0, 12, overrides=SERVER_OVERRIDES,
       desc="lmcache_coordinator_key_directory_server_l1_keys: what each server "
            "reported to the coordinator's key directory. The coordinator "
            "identifies servers by UUID, so this joins through the servers' "
            "target_info to recover the friendly name. A sudden drop to 0 is a "
            "fence, not an eviction. Cross-check against the panel on the right: "
            "they come from independent sources and should agree."),
    # A server exports the eviction counter only once it has evicted, and a
    # subtraction with one side missing is empty: the panel read "No data" until
    # every L1 had filled, which on a large L1 is most of a run. `or ... * 0`
    # stands in a zero for a server that has not evicted yet.
    ts("L1 Keys per Server — Servers' Own View",
       [('sum by (server) (lmcache_mp_l1_write_chunks_total{server=~"$server"})'
         ' - (sum by (server) (lmcache_mp_l1_evicted_chunks_total{server=~"$server"})'
         ' or sum by (server) (lmcache_mp_l1_write_chunks_total{server=~"$server"}) * 0)',
         "{{server}}")],
       "short", 12, 12, overrides=SERVER_OVERRIDES,
       desc="Chunks written to L1 minus chunks evicted from it, straight from "
            "each server. Independent of the coordinator's event stream, so a "
            "disagreement with the panel on the left means a directory consumer "
            "has drifted from what the servers actually hold."),
    ts("L1 Bytes per Server",
       [('lmcache_mp_l1_memory_usage_bytes{server=~"$server"}', "{{server}}")],
       "bytes", 0, 20, overrides=SERVER_OVERRIDES,
       desc="Bytes resident in each server's L1 DRAM tier."),

    ts("L1 Usage Ratio per Server",
       [('lmcache_mp_l1_usage_ratio{server=~"$server"}', "{{server}}")],
       "percentunit", 12, 20, overrides=SERVER_OVERRIDES, maxv=1,
       thresholds={"mode": "absolute",
                   "steps": [{"color": "transparent", "value": None},
                             {"color": "#c98500", "value": 0.8}]},
       desc="Used/total per server. The dashed line is the default "
            "--eviction-trigger-watermark (0.8); crossing it spills L1 to L2."),
    ts("L2 Bytes per Server and Adapter",
       [('lmcache_mp_l2_usage_bytes{server=~"$server"}', "{{server}} · {{l2_name}}")],
       "bytes", 0, 28, overrides=SERVER_OVERRIDES,
       desc="Bytes in each attached L2 adapter. Like the L2 placement count, this "
            "reflects only the current process lifetime — blobs left on disk by "
            "an earlier run are not discovered at startup and read as 0."),

    ts("L1 Chunk Writes/Second",
       [('rate(lmcache_mp_l1_write_chunks_total{server=~"$server"}[1m])', "{{server}}")],
       "cps", 0, 36, w=12, overrides=SERVER_OVERRIDES,
       desc="Rate of chunks admitted into L1."),
    ts("L1 Evictions/Second",
       [('rate(lmcache_mp_l1_evicted_chunks_total{server=~"$server"}[1m])', "{{server}}")],
       "cps", 12, 28, w=12, overrides=SERVER_OVERRIDES,
       desc="Rate of chunks evicted from L1 — the spill to L2."),
    ts("Lookup Hit Tokens/Second by Tier",
       [('sum(rate(lmcache_mp_lookup_hit_l1_tokens_total[1m]))', T1),
        ('sum(rate(lmcache_mp_lookup_hit_l2_tokens_total[1m]))', T2)],
       "cps", 12, 36, w=12, overrides=TIER_OVERRIDES,
       desc="Which tier served the hits, fleet-wide. Tokens, not bytes and not "
            "requests, so the numbers are larger than the request rate by the "
            "tokens per request: NUM_TOKENS=512 at 3.8 lookups/s reads as "
            "~1950/s. The axis unit is Grafana's generic counts-per-second, so "
            "it renders as c/s. Bytes would be larger still — a chunk here is "
            "about 1 MiB, so the same rate in bytes would be ~7.6 MiB/s."),
    ts("Chunks Retrieved by Clients/Second",
       [('sum by (server) (rate(lmcache_mp_num_chunks_loaded_total{server=~"$server"}[1m]))',
         "{{server}}")],
       "cps", 0, 40, overrides=SERVER_OVERRIDES,
       desc="Chunks handed back to a client by a retrieve, whichever tier "
            "they came from: the read side of the fleet. The stimulators only "
            "store, so this stays at zero until `make restore` runs."),
    ts("L2 to L1 Loads/Second",
       [('rate(lmcache_mp_l2_prefetch_load_completed_chunks_total{server=~"$server"}[1m])',
         "{{server}}")],
       "cps", 12, 40, overrides=SERVER_OVERRIDES,
       desc="Chunks loaded from L2 back into L1 because a lookup asked for "
            "something L1 no longer held. The share of the panel on the left "
            "that had to come from L2. Each load takes L1 space, so it also "
            "shows up as L1 evictions."),
    row("Key Directory Inventory (Directory Exporter)", 44),
    ts("L2 Placements per Server",
       [('sum by (server) (lmcache_directory_placements{tier="l2",server=~"$server"})', "{{server}}")],
       "short", 0, 45, overrides=SERVER_OVERRIDES,
       desc="KV blocks each server holds in L2, counted from the coordinator's "
            "key directory. The coordinator's own metrics only give this "
            "fleet-wide; the exporter derives the per-server split by listing "
            "the directory."),
    ts("L2 Logical Bytes per Server",
       [('sum by (server) (lmcache_directory_placement_bytes{tier="l2",server=~"$server"})', "{{server}}")],
       "bytes", 12, 45, overrides=SERVER_OVERRIDES,
       desc="Bytes the directory attributes to each server's L2. Logical: under "
            "the dedup adapter the disk holds far less."),
    ts("L2 Blocks by Age",
       age_targets("l2"),
       "short", 0, 53, stacked=True, fill=70, overrides=AGE_OVERRIDES,
       desc="How long each L2 block has existed, in the exporter's named age "
            "bands. Age is time since the exporter first saw the block; it "
            "checkpoints these so a restart no longer resets them. Bands are "
            "the default seven, as both the colours and the ordering need to "
            "know them ahead of time — if you override AGE_BOUNDS, read the "
            "heatmap below instead, which follows any bounds."),
    ts("Block Age Quantiles",
       [('histogram_quantile(0.5, sum by (le) (lmcache_directory_placement_age_seconds_bucket{tier="l2",server=~"$server"}))', f"{T2} Median"),
        ('histogram_quantile(0.9, sum by (le) (lmcache_directory_placement_age_seconds_bucket{tier="l2",server=~"$server"}))', f"{T2} P90"),
        ('histogram_quantile(0.5, sum by (le) (lmcache_directory_placement_age_seconds_bucket{tier="l1",server=~"$server"}))', f"{T1} Median")],
       "s", 12, 53, overrides=[color(f"{T2} Median", "#d95926"), color(f"{T2} P90", "#c98500"),
                               color(f"{T1} Median", "#3987e5")],
       desc="Computed at query time from the age histogram, so it needs no "
            "fixed bands. Quantiles are interpolated within a bucket: with "
            "coarse bounds the value is approximate, and it can never exceed "
            "the largest bound while blocks sit in the +Inf band."),
    heatmap("L2 Block Age Distribution over Time",
            'sum by (le) (lmcache_directory_placement_age_seconds_bucket{tier="l2",server=~"$server"})',
            0, 57,
            desc="Each column is one moment; colour is how many L2 blocks fall "
                 "in that age band. A diagonal means a cohort ageing together; "
                 "a bright bottom row means fresh blocks arriving."),
    ts("L1 Blocks by Age",
       age_targets("l1"),
       "short", 12, 57, stacked=True, fill=70, overrides=AGE_OVERRIDES,
       desc="How long blocks currently in L1 have been there. With a small L1 "
            "under constant eviction this stays young; old L1 blocks mean the "
            "tier has stopped turning over."),
    ts("Oldest L2 Block per Server",
       [('lmcache_directory_oldest_placement_age_seconds{tier="l2",server=~"$server"}', "{{server}}")],
       "s", 0, 61, overrides=SERVER_OVERRIDES,
       desc="Age of the longest-lived block in each server's L2. A steadily "
            "climbing line means nothing is being evicted from L2."),
    ts("Keys by Number of Servers Holding Them",
       [('sum(lmcache_directory_keys_by_replicas{replicas="1"})', "1 Server"),
        ('sum(lmcache_directory_keys_by_replicas{replicas="2"})', "2 Servers"),
        ('sum(lmcache_directory_keys_by_replicas{replicas="3+"})', "3+ Servers")],
       "short", 12, 61, stacked=True, fill=60, overrides=REPLICA_OVERRIDES,
       desc="Replication across the registered fleet. Keys held by one server "
            "are unique to it; keys held by several come from overlapping "
            "stimulator windows. Copies left by a departed instance are not "
            "counted, so a restarted server is not a replica of its own "
            "previous run."),
    ts("Block Churn per Minute",
       [('sum(rate(lmcache_directory_placements_added_total{tier="l1",server=~"$server"}[2m])) * 60',
         f"{T1} Added"),
        ('sum(rate(lmcache_directory_placements_added_total{tier="l2",server=~"$server"}[2m])) * 60',
         f"{T2} Added"),
        ('sum(rate(lmcache_directory_placements_removed_total{tier="l1",server=~"$server"}[2m])) * 60',
         f"{T1} Removed"),
        ('sum(rate(lmcache_directory_placements_removed_total{tier="l2",server=~"$server"}[2m])) * 60',
         f"{T2} Removed")],
       "short", 0, 69, overrides=CHURN_OVERRIDES,
       desc="Blocks entering and leaving each tier, as seen between directory "
            "polls. A block that appears and disappears between two polls is "
            "not counted. A server restart shows as L1 removed, because the "
            "coordinator fences that server's L1; it does not show as L2 "
            "removed, because the L2 blocks are still listed, so L2 removed "
            "means evicted or deleted."),
    ts("Keys by Access Count",
       bucket_targets("keys_by_access_count", "accesses", ACCESS_LABELS, per_server=False),
       "short", 12, 69, stacked=True, fill=70, overrides=ACCESS_OVERRIDES,
       desc="How often the coordinator reports each key as accessed."),
    ts("L2 Placements Left by Departed Instances",
       [('sum by (server) (lmcache_directory_departed_placements{tier="l2",server=~"$server"})',
         "{{server}}")],
       "short", 0, 77, overrides=SERVER_OVERRIDES,
       desc="L2 blocks the directory still lists for instances that are no "
            "longer registered. A server takes a new instance id every time "
            "it starts, and the coordinator keeps a departed instance's L2 "
            "placements on purpose, so each restart strands that run's blocks "
            "here. Shown under the name the instance had; 'unknown' is one "
            "the exporter never saw registered. Empty on a fleet that has "
            "not been restarted."),
    table("Most Reused Blocks",
          # summed by the three labels that matter, which is also what strips
          # __name__, job, instance and component. Those differ between the two
          # metrics -- __name__ always does -- and any differing column stops
          # the merge from folding the pair into one row.
          [('sum by (rank, hash, location) (lmcache_directory_top_reused_accesses)', ""),
           ('sum by (rank, hash, location) (lmcache_directory_top_reused_age_seconds)', "")],
          0, 85, h=12,
          sort_by="Rank",
          order=["rank", "hash", "location", "Value #A", "Value #B"],
          rename={"rank": "Rank", "hash": "Hash", "location": "Location",
                  "Value #A": "Accesses", "Value #B": "Age"},
          units={"Age": "s"},
          widths={"Rank": 70, "Hash": 170, "Accesses": 110, "Age": 110},
          desc="The blocks the coordinator has served most often, named "
               "individually rather than bucketed: the first 16 hex of the "
               "chunk hash, every server and tier holding a copy, how many "
               "times it has been served, and the age of its oldest copy. "
               "Everything else in this row is an aggregate, because a series "
               "per key would be a cardinality disaster; this is bounded by "
               "TOP_REUSED (default 10). Ties break by hash so the order does "
               "not shuffle between polls. The two metrics carry identical "
               "labels, which is what lets them merge into one row."),
    ts("L2 Logical Bytes Left by Departed Instances",
       [('sum by (server) (lmcache_directory_departed_placement_bytes{tier="l2",server=~"$server"})',
         "{{server}}")],
       "bytes", 12, 77, overrides=SERVER_OVERRIDES,
       desc="Bytes behind the panel on the left. No running server counts "
            "these towards its L2 usage, so nothing evicts them: they stay "
            "until the L2 directory is cleared."),
]

# A table legend needs more vertical room than the list one did, so re-flow the
# grid: keep each panel's declared band and x, but give timeseries panels a
# taller box and stack the bands without gaps. Declared y values only have to
# order the bands, not be exact.
def relayout(panels, ts_height=11):
    bands = {}
    for p in panels:
        bands.setdefault(p["gridPos"]["y"], []).append(p)
    y = 0
    for band_y in sorted(bands):
        band = bands[band_y]
        for p in band:
            if p["type"] == "timeseries":
                p["gridPos"]["h"] = ts_height
            p["gridPos"]["y"] = y
        y += max(p["gridPos"]["h"] for p in band)
    return panels


panels = relayout(panels)

dash = {
    "uid": "lmcache-fleet",
    "title": "LMCache Fleet",
    "description": "Fleet state as the coordinator sees it, plus the per-server "
                   "L1/L2 breakdown the coordinator does not label by instance.",
    "tags": ["lmcache"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "schemaVersion": 39,
    "version": 1,
    "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"},
    "timepicker": {},
    # Listed from a metric the servers serve themselves, whose `server` label
    # is the scrape config's and so is only ever a name. The directory
    # exporter's label used to carry the UUID of every departed instance, and
    # those series stay in Prometheus for its whole retention.
    "templating": {"list": [{
        "name": "server",
        "label": "Server",
        "type": "query",
        "datasource": DS,
        "definition": "label_values(lmcache_mp_l1_usage_ratio, server)",
        "query": {"query": "label_values(lmcache_mp_l1_usage_ratio, server)",
                  "refId": "server"},
        "refresh": 2,
        "includeAll": True,
        "multi": True,
        "allValue": ".*",
        "sort": 1,
        "current": {"text": ["All"], "value": ["$__all"], "selected": True},
        "options": [],
    }]},
    "annotations": {"list": []},
    "panels": panels,
}

print(json.dumps(dash, indent=2))

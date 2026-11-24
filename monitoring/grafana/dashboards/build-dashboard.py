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
COORD_PER_SERVER = (
    "lmcache_coordinator_key_directory_server_l1_keys"
    " * on(instance_id) group_left(server) "
    'label_replace(target_info{job="lmcache-servers",server=~"$server"},'
    '"instance_id","$1","service_instance_id","(.*)")'
)

def color(name, hexv):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hexv}}]}

SERVER_OVERRIDES = [color(f"lmcache-server-{i+1}", h) for i, h in enumerate(SERVER_HUES)]
TIER_OVERRIDES = [color("l1", L1), color("l2", L2)]

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
AGE_OVERRIDES = [color(a, c) for a, c in zip(AGE_LABELS, SEQ_BLUES)]
ACCESS_LABELS = ["0", "1-2", "3-5", "6-10", "11+"]
ACCESS_OVERRIDES = [color(a, SEQ_BLUES[i]) for a, i in zip(ACCESS_LABELS, (0, 2, 3, 5, 6))]
REPLICA_OVERRIDES = [color("1 server", "#3987e5"), color("2 servers", "#d95926"),
                     color("3+ servers", "#199e70")]
CHURN_OVERRIDES = [color("l1 added", "#3987e5"), color("l2 added", "#d95926"),
                   color("l1 removed", "#199e70"), color("l2 removed", "#c98500")]


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
    stat("Keys tracked", "lmcache_coordinator_key_directory_keys_tracked", "short", 0, 0,
         desc="Unique keys the coordinator's directory holds at least one placement for."),
    stat("Servers registered", "lmcache_coordinator_registry_servers_registered", "short", 6, 0,
         desc="MP servers currently heartbeating to the coordinator."),
    stat("L1 placements", 'lmcache_coordinator_key_directory_placements{tier="l1"}', "short", 12, 0,
         desc="Key copies resident in some server's L1 DRAM tier."),
    stat("L2 placements", 'lmcache_coordinator_key_directory_placements{tier="l2"}', "short", 18, 0,
         desc="Key copies written through to an L2 backend. Counts only what was "
              "written while this coordinator has been up: nothing scans L2 at "
              "startup, so .data files left by an earlier run read as 0 here."),

    ts("Where the fleet's KV lives — placements by tier",
       [("lmcache_coordinator_key_directory_placements", "{{tier}}")],
       "short", 0, 4, stacked=True, fill=60, overrides=TIER_OVERRIDES,
       desc="Coordinator's own view. A placement is one place a key is stored: "
            "L1 on one server, or one L2 backend. Stacked, so total height is "
            "every copy the fleet holds."),
    ts("Fleet bytes by tier",
       [("lmcache_coordinator_usage_bytes_stored", "{{tier}}")],
       "bytes", 12, 4, stacked=True, fill=60, overrides=TIER_OVERRIDES,
       desc="Bytes the fleet reports holding, as aggregated by the coordinator."),

    ts("L1 keys per server — coordinator's view",
       [(COORD_PER_SERVER, "{{server}}")],
       "short", 0, 12, overrides=SERVER_OVERRIDES,
       desc="lmcache_coordinator_key_directory_server_l1_keys: what each server "
            "reported to the coordinator's key directory. The coordinator "
            "identifies servers by UUID, so this joins through the servers' "
            "target_info to recover the friendly name. A sudden drop to 0 is a "
            "fence, not an eviction. Cross-check against the panel on the right: "
            "they come from independent sources and should agree."),
    ts("Resident L1 chunks per server — servers' own view",
       [('lmcache_mp_l1_write_chunks_total{server=~"$server"} - lmcache_mp_l1_evicted_chunks_total{server=~"$server"}',
         "{{server}}")],
       "short", 12, 12, overrides=SERVER_OVERRIDES,
       desc="Chunks written to L1 minus chunks evicted from it, straight from "
            "each server. Independent of the coordinator's event stream, so a "
            "disagreement with the panel on the left means a directory consumer "
            "has drifted from what the servers actually hold."),
    ts("L1 bytes per server",
       [('lmcache_mp_l1_memory_usage_bytes{server=~"$server"}', "{{server}}")],
       "bytes", 0, 20, overrides=SERVER_OVERRIDES,
       desc="Bytes resident in each server's L1 DRAM tier."),

    ts("L1 usage ratio per server",
       [('lmcache_mp_l1_usage_ratio{server=~"$server"}', "{{server}}")],
       "percentunit", 12, 20, overrides=SERVER_OVERRIDES, maxv=1,
       thresholds={"mode": "absolute",
                   "steps": [{"color": "transparent", "value": None},
                             {"color": "#c98500", "value": 0.8}]},
       desc="Used/total per server. The dashed line is the default "
            "--eviction-trigger-watermark (0.8); crossing it spills L1 to L2."),
    ts("L2 bytes per server and adapter",
       [('lmcache_mp_l2_usage_bytes{server=~"$server"}', "{{server}} · {{l2_name}}")],
       "bytes", 0, 28, overrides=SERVER_OVERRIDES,
       desc="Bytes in each attached L2 adapter. Like the L2 placement count, this "
            "reflects only the current process lifetime — blobs left on disk by "
            "an earlier run are not discovered at startup and read as 0."),

    ts("L1 chunk writes/sec",
       [('rate(lmcache_mp_l1_write_chunks_total{server=~"$server"}[1m])', "{{server}}")],
       "cps", 0, 36, w=12, overrides=SERVER_OVERRIDES,
       desc="Rate of chunks admitted into L1."),
    ts("L1 evictions/sec",
       [('rate(lmcache_mp_l1_evicted_chunks_total{server=~"$server"}[1m])', "{{server}}")],
       "cps", 12, 28, w=12, overrides=SERVER_OVERRIDES,
       desc="Rate of chunks evicted from L1 — the spill to L2."),
    ts("Lookup hit tokens/sec by tier",
       [('sum(rate(lmcache_mp_lookup_hit_l1_tokens_total[1m]))', "l1"),
        ('sum(rate(lmcache_mp_lookup_hit_l2_tokens_total[1m]))', "l2")],
       "cps", 12, 36, w=12, overrides=TIER_OVERRIDES,
       desc="Which tier served the hits, fleet-wide."),
    row("Key directory inventory (directory exporter)", 44),
    ts("L2 placements per server",
       [('sum by (server) (lmcache_directory_placements{tier="l2",server=~"$server"})', "{{server}}")],
       "short", 0, 45, overrides=SERVER_OVERRIDES,
       desc="KV blocks each server holds in L2, counted from the coordinator's "
            "key directory. The coordinator's own metrics only give this "
            "fleet-wide; the exporter derives the per-server split by listing "
            "the directory."),
    ts("L2 logical bytes per server",
       [('sum by (server) (lmcache_directory_placement_bytes{tier="l2",server=~"$server"})', "{{server}}")],
       "bytes", 12, 45, overrides=SERVER_OVERRIDES,
       desc="Bytes the directory attributes to each server's L2. Logical: under "
            "the dedup adapter the disk holds far less."),
    ts("L2 blocks by age",
       [('sum by (age) (lmcache_directory_placements_by_age{tier="l2",server=~"$server"})', "{{age}}")],
       "short", 0, 53, stacked=True, fill=70, overrides=AGE_OVERRIDES,
       desc="How long each L2 block has existed, in the exporter's named age "
            "bands. Age is time since the exporter first saw the block; it "
            "checkpoints these so a restart no longer resets them. Colours "
            "assume the default seven bands — if you override AGE_BOUNDS, read "
            "the heatmap below instead, which follows any bounds."),
    ts("Block age quantiles",
       [('histogram_quantile(0.5, sum by (le) (lmcache_directory_placement_age_seconds_bucket{tier="l2",server=~"$server"}))', "L2 median"),
        ('histogram_quantile(0.9, sum by (le) (lmcache_directory_placement_age_seconds_bucket{tier="l2",server=~"$server"}))', "L2 p90"),
        ('histogram_quantile(0.5, sum by (le) (lmcache_directory_placement_age_seconds_bucket{tier="l1",server=~"$server"}))', "L1 median")],
       "s", 12, 53, overrides=[color("L2 median", "#d95926"), color("L2 p90", "#c98500"),
                               color("L1 median", "#3987e5")],
       desc="Computed at query time from the age histogram, so it needs no "
            "fixed bands. Quantiles are interpolated within a bucket: with "
            "coarse bounds the value is approximate, and it can never exceed "
            "the largest bound while blocks sit in the +Inf band."),
    heatmap("L2 block age distribution over time",
            'sum by (le) (lmcache_directory_placement_age_seconds_bucket{tier="l2",server=~"$server"})',
            0, 57,
            desc="Each column is one moment; colour is how many L2 blocks fall "
                 "in that age band. A diagonal means a cohort ageing together; "
                 "a bright bottom row means fresh blocks arriving."),
    ts("L1 blocks by age",
       [('sum by (age) (lmcache_directory_placements_by_age{tier="l1",server=~"$server"})', "{{age}}")],
       "short", 12, 57, stacked=True, fill=70, overrides=AGE_OVERRIDES,
       desc="How long blocks currently in L1 have been there. With a small L1 "
            "under constant eviction this stays young; old L1 blocks mean the "
            "tier has stopped turning over."),
    ts("Oldest L2 block per server",
       [('lmcache_directory_oldest_placement_age_seconds{tier="l2",server=~"$server"}', "{{server}}")],
       "s", 0, 61, overrides=SERVER_OVERRIDES,
       desc="Age of the longest-lived block in each server's L2. A steadily "
            "climbing line means nothing is being evicted from L2."),
    ts("Keys by number of servers holding them",
       [('lmcache_directory_keys_by_replicas{replicas="1"}', "1 server"),
        ('lmcache_directory_keys_by_replicas{replicas="2"}', "2 servers"),
        ('lmcache_directory_keys_by_replicas{replicas="3+"}', "3+ servers")],
       "short", 12, 61, stacked=True, fill=60, overrides=REPLICA_OVERRIDES,
       desc="Replication across the fleet. Keys held by one server are unique "
            "to it; keys held by several come from overlapping stimulator "
            "windows."),
    ts("Block churn per minute",
       [('sum by (tier) (rate(lmcache_directory_placements_added_total{server=~"$server"}[2m])) * 60',
         "{{tier}} added"),
        ('sum by (tier) (rate(lmcache_directory_placements_removed_total{server=~"$server"}[2m])) * 60',
         "{{tier}} removed")],
       "short", 0, 69, overrides=CHURN_OVERRIDES,
       desc="Blocks entering and leaving each tier, as seen between directory "
            "polls. A block that appears and disappears between two polls is "
            "not counted."),
    ts("Keys by access count",
       [("sum by (accesses) (lmcache_directory_keys_by_access_count)", "{{accesses}}")],
       "short", 12, 69, stacked=True, fill=70, overrides=ACCESS_OVERRIDES,
       desc="How often the coordinator reports each key as accessed."),
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
    "templating": {"list": [{
        "name": "server",
        "label": "Server",
        "type": "query",
        "datasource": DS,
        "definition": "label_values(lmcache_directory_placements, server)",
        "query": {"query": "label_values(lmcache_directory_placements, server)",
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

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
    'label_replace(target_info{job="lmcache-servers"},'
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
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": True, "calcs": []},
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
         desc="Key copies written through to an L2 backend. CAVEAT: with the fs "
              "adapter this reads 0 even when data/l2-server-*/ holds real .data "
              "files and servers are serving L2 hits — the fs adapter does not "
              "report its contents to the coordinator directory."),

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
       [("lmcache_mp_l1_write_chunks_total - lmcache_mp_l1_evicted_chunks_total",
         "{{server}}")],
       "short", 12, 12, overrides=SERVER_OVERRIDES,
       desc="Chunks written to L1 minus chunks evicted from it, straight from "
            "each server. Independent of the coordinator's event stream, so a "
            "disagreement with the panel on the left means a directory consumer "
            "has drifted from what the servers actually hold."),
    ts("L1 bytes per server",
       [("lmcache_mp_l1_memory_usage_bytes", "{{server}}")],
       "bytes", 0, 20, overrides=SERVER_OVERRIDES,
       desc="Bytes resident in each server's L1 DRAM tier."),

    ts("L1 usage ratio per server",
       [("lmcache_mp_l1_usage_ratio", "{{server}}")],
       "percentunit", 12, 20, overrides=SERVER_OVERRIDES, maxv=1,
       thresholds={"mode": "absolute",
                   "steps": [{"color": "transparent", "value": None},
                             {"color": "#c98500", "value": 0.8}]},
       desc="Used/total per server. The dashed line is the default "
            "--eviction-trigger-watermark (0.8); crossing it spills L1 to L2."),
    ts("L2 bytes per server and adapter",
       [("lmcache_mp_l2_usage_bytes", "{{server}} · {{l2_name}}")],
       "bytes", 0, 28, overrides=SERVER_OVERRIDES,
       desc="Bytes in each attached L2 adapter. NOTE: the fs adapter reports 0 "
            "here even with real .data files on disk. To confirm L2 is actually "
            "working, use the eviction rate and the L2 lookup-hit panels below, "
            "or count files in data/l2-server-*/."),

    ts("L1 chunk writes/sec",
       [("rate(lmcache_mp_l1_write_chunks_total[1m])", "{{server}}")],
       "cps", 0, 36, w=12, overrides=SERVER_OVERRIDES,
       desc="Rate of chunks admitted into L1."),
    ts("L1 evictions/sec",
       [("rate(lmcache_mp_l1_evicted_chunks_total[1m])", "{{server}}")],
       "cps", 12, 28, w=12, overrides=SERVER_OVERRIDES,
       desc="Rate of chunks evicted from L1 — the spill to L2."),
    ts("Lookup hit tokens/sec by tier",
       [('sum(rate(lmcache_mp_lookup_hit_l1_tokens_total[1m]))', "l1"),
        ('sum(rate(lmcache_mp_lookup_hit_l2_tokens_total[1m]))', "l2")],
       "cps", 12, 36, w=12, overrides=TIER_OVERRIDES,
       desc="Which tier served the hits, fleet-wide."),
]

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
    "templating": {"list": []},
    "annotations": {"list": []},
    "panels": panels,
}

print(json.dumps(dash, indent=2))

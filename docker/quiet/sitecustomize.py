"""Drop the access-log lines this fleet generates on a timer.

Prometheus scrapes /metrics, compose probes /healthcheck, the servers
heartbeat, and the directory exporter pages through /directory/keys -- all on
a few seconds' interval, all logged by uvicorn at INFO. On a two-server fleet
that is around 150 lines a minute saying nothing, and the coordinator is the
worst of it. Anything interesting is buried.

LMCache starts uvicorn with access_log=True and log_level="info" hardcoded
(lmcache/v1/multiprocess/http_server.py), so there is no flag to turn down.
Python imports a module named sitecustomize at interpreter startup if one is
on sys.path, which is early enough to attach a filter to the access logger.

The filter has to be on the *logger*, not a handler: uvicorn applies its own
dictConfig after this runs, and that removes a logger's handlers but leaves
its filters in place.

Only polled paths at a 2xx/3xx status are dropped. A failing healthcheck or a
404 on /metrics still prints, which is when you would actually want it.
"""
import logging
import os


def _enabled(name, default="1"):
    return os.environ.get(name, default).lower() not in ("0", "false", "no", "")


# Matched as a prefix, so /directory/keys?limit=1000 goes with /directory/keys.
# An entry starting with * is matched as a suffix instead, which is how the
# heartbeats go without taking POST /instances with them -- a server joining
# the fleet is worth a line, a server saying it is still alive every 5s is not.
NOISY_PATHS = tuple(
    p.strip() for p in os.environ.get(
        "QUIET_ACCESS_PATHS",
        "/healthcheck,/metrics,/directory/keys,*/heartbeat",
    ).split(",") if p.strip()
)
PREFIXES = tuple(p for p in NOISY_PATHS if not p.startswith("*"))
SUFFIXES = tuple(p[1:] for p in NOISY_PATHS if p.startswith("*"))


def is_polled(path):
    """Whether this path is one something hits on a timer."""
    path = path.split("?", 1)[0]
    return path.startswith(PREFIXES) or (SUFFIXES and path.endswith(SUFFIXES))


class DropPolledPaths(logging.Filter):
    """Drop successful access-log records for paths polled on a timer."""

    def filter(self, record):
        # uvicorn logs access as
        #   '%s - "%s %s HTTP/%s" %d %s' %
        #   (client_addr, method, full_path, http_version, status_code, phrase)
        args = record.args or ()
        if not isinstance(args, tuple) or len(args) < 5:
            return True          # not the shape we know: leave it alone
        path, status = str(args[2]), args[4]
        if not isinstance(status, int) or status >= 400:
            return True          # keep anything that went wrong
        return not is_polled(path)


if NOISY_PATHS and _enabled("QUIET_ACCESS_LOG"):
    logging.getLogger("uvicorn.access").addFilter(DropPolledPaths())

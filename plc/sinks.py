"""Pluggable outputs for signal edges.

A sink is just a callable taking one plc.signals.Edge. watch_signals.py calls
every configured sink for every edge it sees; nothing about the polling loop
or the display needs to change to add another one -- write a function here
with the same shape (open/connect once, return a closure that does the work)
and wire it up in watch_signals.py's build_sinks().
"""
import csv
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request


def csv_sink(path):
    """Append one row per edge to `path` (created with a header if new)."""
    new = not os.path.exists(path)
    f = open(path, "a", newline="", buffering=1)
    w = csv.writer(f)
    if new:
        w.writerow(["epoch", "iso", "signal", "event", "width_s", "count"])

    def sink(edge):
        w.writerow([
            f"{edge.t:.3f}",
            time.strftime("%H:%M:%S", time.localtime(edge.t)),
            edge.name,
            edge.event,
            f"{edge.width:.3f}" if edge.width else "",
            edge.count,
        ])

    return sink


def rest_sink(config):
    """POST every edge to `{config.base_url}/api/transactions` as
    {"machineId": edge.name, "edge": "rise"|"fall"}, with header
    `x-api-key: {config.api_key}` when one is configured. The signal's own
    name from plc/signals.py's SIGNALS list is used as the machineId, so each
    signal you add there gets posted under its own id automatically.

    Runs the actual HTTP calls on a background thread via a queue, so a slow
    or unreachable API can never stall the PLC polling loop -- sink() just
    enqueues and returns immediately. Failed posts are logged to stderr and
    dropped (no retry queue -- edges are a live signal, not a durable log;
    add persistence here first if you need delivery guarantees).
    """
    url = config.transactions_url
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["x-api-key"] = config.api_key
    else:
        print("[rest_sink] warning: REST_API_KEY is not set, posting without x-api-key",
              file=sys.stderr)
    q = queue.Queue()

    def worker():
        while True:
            edge = q.get()
            body = json.dumps({"machineId": edge.name, "edge": edge.event}).encode()
            req = urllib.request.Request(url, data=body, method="POST", headers=headers)
            try:
                urllib.request.urlopen(req, timeout=config.timeout).read()
            except Exception as e:
                print(f"[rest_sink] POST to {url} failed: {e}", file=sys.stderr)
            q.task_done()

    threading.Thread(target=worker, daemon=True, name="rest_sink").start()

    def sink(edge):
        q.put(edge)

    return sink

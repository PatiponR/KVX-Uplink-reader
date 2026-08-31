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
import sqlite3
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


# --- occurredAt formatting --------------------------------------------------
#
# The API wants UTC ISO-8601 with a literal "Z" and milliseconds:
# "2026-08-31T06:00:00.000Z". Its validator rejects a "+07:00" offset, bare
# local time, and epoch numbers -- so this is built by hand rather than with
# datetime.isoformat(), which produces "+00:00" and microseconds. Bangkok time
# is what Python hands you by default here, and it is exactly what gets
# rejected, so nothing in this path may use local time.

# A Pi has no battery-backed clock. After a power cut it boots believing it is
# whenever it last knew -- or 1970 -- until NTP catches up, and with the uplink
# down that can be a long while. An edge stamped with a wildly wrong time is
# worse than one the server stamps itself, so below this floor we omit the
# field entirely, which the API accepts. Any real reading is after this date.
_CLOCK_SANE_AFTER = 1767225600.0   # 2026-01-01T00:00:00Z

_warned_clock = []


def _occurred_at(t):
    """UTC ISO-8601 with milliseconds and a Z, or None if the Pi's clock is
    obviously unset (in which case we let the server stamp it)."""
    if t < _CLOCK_SANE_AFTER:
        if not _warned_clock:
            _warned_clock.append(True)
            print(f"[rest_sink] system clock reads {time.ctime(t)} -- looks unsynced, "
                  f"omitting occurredAt so the server stamps these instead", file=sys.stderr)
        return None
    # Split into whole seconds + milliseconds in one step. Doing it as
    # int((t - int(t)) * 1000) instead looks equivalent but silently truncates
    # a millisecond off some values -- .007 becomes .006 -- because the binary
    # float sits just under. divmod on the rounded total can't drift, and
    # carries cleanly into the next second when ms would land on 1000.
    whole, ms = divmod(int(round(t * 1000)), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{ms:03d}Z"


# --- durable spool for rest_sink -------------------------------------------
#
# `pending` is the retry queue: every edge the API hasn't accepted yet, oldest
# first by id. `dead` is where edges go that the API will never accept (a 4xx
# that isn't 408/429 -- a malformed body, an unknown machineId). Without that
# split one poison edge would sit at the head of `pending` forever and block
# every edge behind it, so a permanent rejection is set aside instead, still on
# disk for you to inspect, and the queue keeps moving.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    t          REAL NOT NULL,
    machine_id TEXT NOT NULL,
    event      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dead (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    t          REAL NOT NULL,
    machine_id TEXT NOT NULL,
    event      TEXT NOT NULL,
    failed_at  REAL NOT NULL,
    reason     TEXT NOT NULL
);
"""


def _open_spool(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    # WAL so a reader/inspector can't block the writer; synchronous=FULL because
    # the whole point here is surviving power loss on a Pi that nobody is
    # watching. Edges arrive seconds apart, so the extra fsync costs nothing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _spool(conn, t, machine_id, event):
    conn.execute("INSERT INTO pending (t, machine_id, event) VALUES (?, ?, ?)",
                 (t, machine_id, event))
    conn.commit()


def _bury(conn, t, machine_id, event, reason, row_id=None):
    conn.execute(
        "INSERT INTO dead (t, machine_id, event, failed_at, reason) VALUES (?, ?, ?, ?, ?)",
        (t, machine_id, event, time.time(), reason))
    if row_id is not None:
        conn.execute("DELETE FROM pending WHERE id = ?", (row_id,))
    conn.commit()


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]


def rest_sink(config):
    """POST every edge to `{config.base_url}/api/transactions` as
    {"machineId": edge.name, "edge": "rise"|"fall", "occurredAt": <UTC ISO Z>},
    with header
    `x-api-key: {config.api_key}` when one is configured. The signal's own
    name from plc/signals.py's SIGNALS list is used as the machineId, so each
    signal you add there gets posted under its own id automatically.

    Nothing is dropped when the API is unreachable. An edge whose POST fails
    is written to a SQLite spool (config.spool_path) and retried with
    exponential backoff, from config.retry_base up to config.retry_max seconds
    between attempts, until it's accepted. The spool is on disk, so a reboot or
    a crash mid-outage replays the backlog instead of losing it.

    While any backlog exists, new edges go straight to the spool rather than
    being posted directly -- otherwise a live edge would overtake the older
    ones still waiting and the API would see them out of order.

    Runs on one background thread that owns the SQLite connection (so there's
    no cross-thread sqlite3 sharing to get wrong); sink() only enqueues and
    returns, so neither a slow API nor a long outage can stall the PLC polling
    loop.

    Every POST carries `occurredAt`: the moment the PLC actually fired, in the
    UTC-with-Z format the API's validator requires (see _occurred_at above).
    The spool stores that time alongside the edge, so a replay after a two-hour
    outage still reports when the edge really happened rather than when it was
    finally delivered.
    """
    url = config.transactions_url
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["x-api-key"] = config.api_key
    else:
        print("[rest_sink] warning: REST_API_KEY is not set, posting without x-api-key",
              file=sys.stderr)
    q = queue.Queue()

    def post(t, machine_id, event):
        """Try one POST. Returns (delivered, permanent_reason). A non-None
        reason means retrying can never succeed -- don't keep it queued."""
        payload = {"machineId": machine_id, "edge": event}
        occurred_at = _occurred_at(t)
        if occurred_at:
            payload["occurredAt"] = occurred_at
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=config.timeout) as r:
                r.read()
            return True, None
        except urllib.error.HTTPError as e:
            # 408/429 are "try again"; every other 4xx means this exact body
            # will be rejected forever, however long we wait.
            if 400 <= e.code < 500 and e.code not in (408, 429):
                return False, f"HTTP {e.code}"
            print(f"[rest_sink] POST to {url} failed: HTTP {e.code}", file=sys.stderr)
            return False, None
        except Exception as e:
            print(f"[rest_sink] POST to {url} failed: {e}", file=sys.stderr)
            return False, None

    def drain(conn):
        """Replay spooled edges oldest-first until one fails or none are left.
        Returns how many were delivered."""
        sent = 0
        while True:
            row = conn.execute(
                "SELECT id, t, machine_id, event FROM pending ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return sent
            row_id, t, machine_id, event = row
            delivered, permanent = post(t, machine_id, event)
            if delivered:
                conn.execute("DELETE FROM pending WHERE id = ?", (row_id,))
                conn.commit()
                sent += 1
            elif permanent:
                print(f"[rest_sink] {machine_id}/{event} rejected permanently "
                      f"({permanent}), moved to the dead table", file=sys.stderr)
                _bury(conn, t, machine_id, event, permanent, row_id)
            else:
                return sent

    def worker():
        conn = _open_spool(config.spool_path)
        backlog = _count(conn)
        if backlog:
            print(f"[rest_sink] {backlog} edge(s) left over from a previous run, replaying",
                  file=sys.stderr)
        delay = config.retry_base
        next_try = time.monotonic()

        while True:
            # Block for a new edge, but no longer than the next retry deadline
            # when there's a backlog waiting to go out.
            timeout = max(0.0, next_try - time.monotonic()) if backlog else None
            try:
                edge = q.get(timeout=timeout)
            except queue.Empty:
                edge = None

            if edge is not None:
                if backlog:
                    _spool(conn, edge.t, edge.name, edge.event)
                    backlog += 1
                else:
                    delivered, permanent = post(edge.t, edge.name, edge.event)
                    if permanent:
                        print(f"[rest_sink] {edge.name}/{edge.event} rejected permanently "
                              f"({permanent}), moved to the dead table", file=sys.stderr)
                        _bury(conn, edge.t, edge.name, edge.event, permanent)
                    elif not delivered:
                        _spool(conn, edge.t, edge.name, edge.event)
                        backlog = _count(conn)
                        delay = config.retry_base
                        next_try = time.monotonic() + delay
                        print(f"[rest_sink] spooled {edge.name}/{edge.event}, "
                              f"retrying in {delay:g}s", file=sys.stderr)
                q.task_done()
                continue

            # Retry deadline reached with a backlog: try to flush it.
            sent = drain(conn)
            backlog = _count(conn)
            if backlog:
                delay = min(delay * 2, config.retry_max)
                next_try = time.monotonic() + delay
                if sent:
                    print(f"[rest_sink] replayed {sent}, {backlog} still queued, "
                          f"retrying in {delay:g}s", file=sys.stderr)
            else:
                if sent:
                    print(f"[rest_sink] backlog cleared, {sent} edge(s) replayed",
                          file=sys.stderr)
                delay = config.retry_base

    threading.Thread(target=worker, daemon=True, name="rest_sink").start()

    def sink(edge):
        q.put(edge)

    return sink

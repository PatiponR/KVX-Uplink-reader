"""Pluggable outputs for signal edges.

A sink is just a callable taking one plc.signals.Edge. watch_signals.py calls
every configured sink for every edge it sees; nothing about the polling loop
or the display needs to change to add another one -- write a function here
with the same shape (open/connect once, return a closure that does the work)
and wire it up in watch_signals.py's build_sinks().
"""
import csv
import http.client
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import urllib.parse


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
# `pending` is the send queue: every edge the API hasn't confirmed yet, oldest
# first by id. `dead` is where edges go that the API will never accept (it
# listed them under "rejected", or answered a 4xx that isn't 408/429). Without
# that split one poison edge would sit at the head of `pending` forever and
# block every edge behind it, so a permanent rejection is set aside instead,
# still on disk for you to inspect, and the queue keeps moving.

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
    # watching. Edges are written in groups (one commit per group), so the
    # fsync cost is per burst, not per edge.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]


def _one_line(data, limit=300):
    """A response body squeezed onto one line and capped, for log lines."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    text = " ".join(data.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _log(msg):
    print(f"[rest_sink] {msg}", file=sys.stderr)


class _Http:
    """POSTs over one kept-alive connection (a fresh TLS handshake per request
    roughly doubles its cost over a ~80 ms link). Reconnects on demand; if a
    connection that sat idle was closed by the server, the first send quietly
    retries once on a fresh one. Only used from the sender thread."""

    def __init__(self, headers, timeout):
        self.headers = headers
        self.timeout = timeout
        self._conn = None
        self._netloc = None

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def post(self, url, body):
        """Returns (status, response_bytes). Raises on network errors/timeouts."""
        parts = urllib.parse.urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if self._netloc != parts.netloc:
            self.close()
            self._netloc = parts.netloc
        for attempt in (0, 1):
            reused = self._conn is not None
            if not reused:
                cls = (http.client.HTTPSConnection if parts.scheme == "https"
                       else http.client.HTTPConnection)
                self._conn = cls(parts.netloc, timeout=self.timeout)
            try:
                self._conn.request("POST", target, body=body, headers=self.headers)
                resp = self._conn.getresponse()
                data = resp.read()
                if resp.will_close:
                    self.close()
                return resp.status, data
            except ConnectionError:
                self.close()
                if reused and attempt == 0:
                    continue
                raise
            except Exception:
                self.close()
                raise


# Outcomes of one send attempt.
_OK, _RETRY, _SINGLE, _SMALLER = "ok", "retry", "single", "smaller"

# Edges are logged as dropped from a full spool in chunks of this size at most.
_DROP_CHUNK = 1000
# Longest the sender sleeps while edges are waiting, even if its backoff timer
# says later -- a floor so a mis-computed deadline can never strand the queue.
_RETRY_FLOOR = 15.0
_STATS_EVERY = 60.0


class _RestSender:
    """The thread that owns the spool and talks to the API. See rest_sink."""

    def __init__(self, config):
        self.cfg = config
        self.q = queue.Queue()
        self.inbox = []             # taken off q, not yet committed to the spool
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["x-api-key"] = config.api_key
        else:
            _log("warning: REST_API_KEY is not set, posting without x-api-key")
        self.http = _Http(headers, config.timeout)
        self.conn = None
        self.pending = 0
        self.delay = config.retry_base
        self.next_try = 0.0
        self.batch_off_until = 0.0  # /batch answered 404: use the old route until then
        self.cur_max = config.batch_max
        self.failing = False
        self.last_stats = time.monotonic()

    # -- called from the polling thread --------------------------------------

    def submit(self, edge):
        self.q.put(edge)

    # -- sender thread --------------------------------------------------------

    def run(self):
        self.conn = _open_spool(self.cfg.spool_path)
        self.pending = _count(self.conn)
        if self.pending:
            _log(f"{self.pending} edge(s) left over from a previous run, sending")
        while True:
            try:
                self.step()
            except Exception as e:
                # Never let the sender thread die: it would silently stop every
                # edge from leaving this Pi until the service is restarted.
                _log(f"unexpected error: {e!r}; continuing in 1s")
                time.sleep(1)

    def step(self):
        now = time.monotonic()
        if now - self.last_stats >= _STATS_EVERY:
            self.last_stats = now
            _log(f"queue length {self.pending}")
        if self.pending:
            wait = min(max(0.0, self.next_try - now), _RETRY_FLOOR)
        else:
            wait = max(0.0, _STATS_EVERY - (now - self.last_stats))
        self.ingest(wait)
        if self.pending and time.monotonic() >= self.next_try:
            self.flush()

    def ingest(self, wait):
        """Move edges from the in-memory hand-off queue into the spool, as one
        commit however many arrived. Blocks up to `wait` seconds for the first."""
        try:
            self.inbox.append(self.q.get(timeout=wait) if wait > 0 else self.q.get_nowait())
        except queue.Empty:
            if not self.inbox:
                return
        while True:
            try:
                self.inbox.append(self.q.get_nowait())
            except queue.Empty:
                break
        try:
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO pending (t, machine_id, event) VALUES (?, ?, ?)",
                    [(e.t, e.name, e.event) for e in self.inbox])
        except sqlite3.Error as e:
            # Disk full / corrupt: keep the edges in memory and try again next
            # loop rather than losing them.
            _log(f"could not write {len(self.inbox)} edge(s) to the spool: {e}")
            time.sleep(1)
            return
        self.pending += len(self.inbox)
        self.inbox.clear()
        if self.pending > self.cfg.queue_max:
            self.enforce_cap()

    def enforce_cap(self):
        drop = min(self.pending - self.cfg.queue_max + max(1, self.cfg.queue_max // 10),
                   _DROP_CHUNK, self.pending)
        with self.conn:
            self.conn.execute(
                "DELETE FROM pending WHERE id IN "
                "(SELECT id FROM pending ORDER BY id LIMIT ?)", (drop,))
        self.pending -= drop
        _log(f"spool is full ({self.cfg.queue_max} edges): dropped the {drop} oldest")

    # -- sending ---------------------------------------------------------------

    def flush(self):
        rows = self.conn.execute(
            "SELECT id, t, machine_id, event FROM pending ORDER BY id LIMIT ?",
            (self.cur_max,)).fetchall()
        if not rows:
            self.pending = 0
            return
        if time.monotonic() < self.batch_off_until:
            outcome = _SINGLE
        else:
            outcome = self.send_batch(rows)
        if outcome == _SINGLE:
            outcome = self.send_singles(rows)

        if outcome == _OK:
            if self.failing:
                self.failing = False
                _log(f"delivered again, {self.pending} edge(s) still queued")
            self.delay = self.cfg.retry_base
            self.next_try = 0.0
            self.cur_max = min(self.cfg.batch_max, self.cur_max * 2)
        elif outcome == _SMALLER:
            self.cur_max = max(1, len(rows) // 2)
            self.next_try = 0.0
        else:
            self.failing = True
            self.next_try = time.monotonic() + self.delay
            _log(f"{self.pending} edge(s) queued, retrying in {self.delay:g}s")
            self.delay = min(self.delay * 2, self.cfg.retry_max)

    def remove(self, delivered_ids, dead=()):
        """Atomically drop confirmed edges from `pending`, setting aside `dead`
        ((row, reason) pairs) in the dead table."""
        with self.conn:
            if dead:
                self.conn.executemany(
                    "INSERT INTO dead (t, machine_id, event, failed_at, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(r[1], r[2], r[3], time.time(), reason) for r, reason in dead])
            ids = list(delivered_ids) + [r[0] for r, _ in dead]
            cur = self.conn.executemany("DELETE FROM pending WHERE id = ?",
                                        [(i,) for i in ids])
        # rowcount, not len(ids): a full-spool drop may have removed some already.
        self.pending -= cur.rowcount

    @staticmethod
    def event_dict(row):
        _, t, machine_id, event = row
        d = {"machineId": machine_id, "edge": event}
        occurred_at = _occurred_at(t)
        if occurred_at:
            d["occurredAt"] = occurred_at
        return d

    def send_batch(self, rows):
        url = self.cfg.batch_url
        body = json.dumps({"events": [self.event_dict(r) for r in rows]}).encode()
        started = time.monotonic()
        try:
            status, resp = self.http.post(url, body)
        except Exception as e:
            self.log_request(url, len(rows), f"failed ({e})", b"", started, always=True)
            return _RETRY
        self.log_request(url, len(rows), f"HTTP {status}", resp, started,
                         always=(status != 201))

        if status == 201:
            rejected = {}
            try:
                for item in json.loads(resp).get("rejected") or []:
                    rejected[int(item["index"])] = str(item.get("error", "rejected"))
            except Exception as e:
                _log(f"batch stored, but the reply could not be read ({e}); "
                     f"treating all {len(rows)} as accepted")
            dead = []
            for idx, reason in sorted(rejected.items()):
                if 0 <= idx < len(rows):
                    r = rows[idx]
                    _log(f"{r[2]}/{r[3]} rejected permanently ({reason}), "
                         f"moved to the dead table")
                    dead.append((r, f"batch: {reason}"))
            dead_ids = {r[0] for r, _ in dead}
            self.remove([r[0] for r in rows if r[0] not in dead_ids], dead)
            return _OK
        if status == 400:
            _log(f"ERROR: API rejected the batch request shape (HTTP 400: "
                 f"{_one_line(resp)}); sending these {len(rows)} edge(s) one at a time")
            return _SINGLE
        if status == 404:
            self.batch_off_until = time.monotonic() + self.cfg.batch_recheck
            _log(f"{url} not found (server older than the batch feature?); using "
                 f"{self.cfg.transactions_url} one edge at a time, "
                 f"trying /batch again in {self.cfg.batch_recheck:g}s")
            return _SINGLE
        if status == 413:
            if len(rows) > 1:
                _log(f"batch of {len(rows)} too large (HTTP 413), halving")
                return _SMALLER
            return _SINGLE
        if status in (401, 403):
            _log(f"ALERT: API refused the key (HTTP {status}); nothing is being "
                 f"delivered until REST_API_KEY is fixed. {self.pending} edge(s) kept.")
            return _RETRY
        # 429, 5xx, a redirect or captive-portal page, anything else: temporary.
        return _RETRY

    def send_singles(self, rows):
        """Old one-edge-per-POST route, strictly in order. Stops at the first
        temporary failure. Keeps taking edges off the hand-off queue between
        requests so a slow fallback never leaves them only in memory."""
        url = self.cfg.transactions_url
        for row in rows:
            body = json.dumps(self.event_dict(row)).encode()
            started = time.monotonic()
            try:
                status, resp = self.http.post(url, body)
            except Exception as e:
                self.log_request(url, 1, f"failed ({e})", b"", started, always=True)
                return _RETRY
            self.log_request(url, 1, f"HTTP {status}", resp, started,
                             always=not (200 <= status < 300))
            if 200 <= status < 300:
                self.remove([row[0]])
            elif status in (401, 403):
                _log(f"ALERT: API refused the key (HTTP {status}); edges kept.")
                return _RETRY
            elif 400 <= status < 500 and status not in (408, 429):
                reason = f"HTTP {status}: {_one_line(resp)}" if resp else f"HTTP {status}"
                _log(f"{row[2]}/{row[3]} rejected permanently ({reason}), "
                     f"moved to the dead table")
                self.remove([], [(row, reason)])
            else:
                return _RETRY
            self.ingest(0)
        return _OK

    def log_request(self, url, n, outcome, resp, started, always):
        if not (always or self.cfg.log_requests):
            return
        ms = (time.monotonic() - started) * 1000
        print(f"[http] POST {url} ({n} edge{'s' if n != 1 else ''}) -> {outcome} "
              f"in {ms:.0f} ms" + (f": {_one_line(resp)}" if resp else ""),
              file=sys.stderr)


def rest_sink(config):
    """Send every edge to the API as
    {"machineId": edge.name, "edge": <event>, "occurredAt": <UTC ISO Z>}.
    <event> is "rise"/"fall" for a plc/signals.py SIGNALS entry, or
    "service"/"operate" for a SERVICE_SIGNALS entry. The signal's own name is
    the machineId, so each signal you add is sent under its own id.

    Edges are sent in batches to `POST /api/transactions/batch` (one request
    carries everything waiting, up to config.batch_max), because one request per
    edge caps throughput at ~1/round-trip (about 12 edges/s on the Singapore
    link) and the dashboard falls ever further behind. See
    PI-SENDER-BATCH-REQUIREMENTS.md for the contract; R-numbers below refer to it.

    sink() only hands the edge to a background thread and returns, so neither a
    slow API nor a long outage can stall the PLC polling loop. That thread:

      * writes every edge to the SQLite spool (config.spool_path) before any
        network attempt (R2), one commit per burst; the spool is on disk, so a
        reboot or crash replays the backlog instead of losing it;
      * sends the oldest edges first, one request in flight, skipping nothing
        past a failed batch (R4), and starts the next request the moment the
        previous one finishes if anything is waiting -- no timer to "fill" a
        batch (R5);
      * removes edges from the spool only after the API answers 201 (R6);
        edges listed under "rejected" can never succeed and move to the `dead`
        table instead;
      * retries temporary failures with backoff from config.retry_base doubling
        up to config.retry_max, reset after a success (R7);
      * reuses one keep-alive connection with config.timeout per request (R8);
      * caps the spool at config.queue_max, dropping the oldest in chunks (R9);
      * logs the queue length once a minute (R11).

    occurredAt is the moment the PLC edge was read, in UTC with a literal Z (see
    _occurred_at). The spool stores that time, and it is formatted from the
    stored value each time, so a retry or a replay after a reboot sends the
    identical string and the API can skip anything it already stored (R1).

    HTTP 400 on the batch route falls back to sending those edges one at a time
    to /api/transactions; 404 does the same and re-tries /batch every
    config.batch_recheck seconds. The full response table is in send_batch().

    With config.log_requests on, every request is logged to stderr (the journal
    under systemd): edge count, status, response and time taken. Failures are
    logged either way.
    """
    sender = _RestSender(config)
    threading.Thread(target=sender.run, daemon=True, name="rest_sink").start()
    return sender.submit

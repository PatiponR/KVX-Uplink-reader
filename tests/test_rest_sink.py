"""End-to-end tests for plc.sinks.rest_sink against a fake API on localhost.

    python3 -m unittest discover -s tests -v
"""
import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plc.config import RestConfig          # noqa: E402
from plc.signals import Edge               # noqa: E402
from plc.sinks import rest_sink            # noqa: E402

T0 = 1_800_000_000.0   # after the sink's "clock is sane" floor


class FakeApi:
    """Records every request. `batch_status`/`single_status` are the status the
    next requests get (a callable of the request body, or an int)."""

    def __init__(self, latency=0.0):
        self.latency = latency
        self.batch_status = 201
        self.single_status = 201
        self.batch_reply = None          # optional dict for a 201 body
        self.max_batch = None            # answer 413 above this many events
        self.requests = []               # (path, parsed body)
        self.stored = []                 # events accepted, in order
        self.lock = threading.Lock()
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                time.sleep(api.latency)
                with api.lock:
                    api.requests.append((self.path, body))
                    if self.path.endswith("/batch"):
                        status = api.batch_status() if callable(api.batch_status) else api.batch_status
                        if status == 201 and api.max_batch and len(body["events"]) > api.max_batch:
                            status = 413
                        if status == 201:
                            api.stored.extend(body["events"])
                        reply = api.batch_reply or {"accepted": len(body["events"]),
                                                    "duplicates": 0, "rejected": []}
                    else:
                        status = api.single_status() if callable(api.single_status) else api.single_status
                        if 200 <= status < 300:
                            api.stored.append(body)
                        reply = {"ok": status < 300}
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def batches(self):
        return [b for p, b in self.requests if p.endswith("/batch")]


def edge(i, name="TMC-400"):
    return Edge(t=T0 + i * 0.5, name=name, event="rise" if i % 2 == 0 else "fall",
                width=None, count=i)


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class RestSinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = os.path.join(self.tmp.name, "spool.db")
        self.api = FakeApi()

    def tearDown(self):
        self.api.close()
        self.tmp.cleanup()

    def cfg(self, **kw):
        base = dict(base_url=self.api.url, api_key="k", timeout=2.0, spool_path=self.spool,
                    batch_max=200, queue_max=50000, retry_base=0.05, retry_max=0.2,
                    batch_recheck=300.0, log_requests=False)
        base.update(kw)
        return RestConfig(**base)

    def db(self, sql):
        # The sender thread creates the tables; a query can beat it to it.
        for _ in range(100):
            c = sqlite3.connect(self.spool)
            try:
                return c.execute(sql).fetchall()
            except sqlite3.OperationalError:
                time.sleep(0.02)
            finally:
                c.close()
        raise AssertionError("spool tables never appeared")

    def test_batches_in_order_and_far_fewer_requests(self):
        self.api.latency = 0.03
        sink = rest_sink(self.cfg())
        n = 400
        for i in range(n):
            sink(edge(i))
        self.assertTrue(wait_for(lambda: len(self.api.stored) == n))
        self.assertEqual([e["occurredAt"] for e in self.api.stored],
                         [f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(int(T0 + i * 0.5)))}"
                          f".{int(round(((T0 + i * 0.5) % 1) * 1000)):03d}Z" for i in range(n)])
        self.assertLess(len(self.api.requests), n // 5)
        self.assertTrue(all(len(b["events"]) <= 200 for b in self.api.batches()))
        self.assertTrue(wait_for(lambda: self.db("select count(*) from pending")[0][0] == 0))

    def test_outage_keeps_everything_then_drains_in_order(self):
        self.api.batch_status = 503
        sink = rest_sink(self.cfg())
        for i in range(50):
            sink(edge(i))
        time.sleep(0.5)
        self.assertEqual(self.api.stored, [])
        self.assertTrue(len(self.api.requests) >= 2)          # retried with backoff
        self.assertEqual(self.db("select count(*) from pending")[0][0], 50)
        self.api.batch_status = 201
        self.assertTrue(wait_for(lambda: len(self.api.stored) == 50))
        self.assertEqual([e["machineId"] for e in self.api.stored], ["TMC-400"] * 50)
        self.assertEqual([e["edge"] for e in self.api.stored],
                         ["rise" if i % 2 == 0 else "fall" for i in range(50)])

    def test_retry_resends_identical_occurred_at(self):
        flaky = iter([503, 503])
        self.api.batch_status = lambda: next(flaky, 201)
        sink = rest_sink(self.cfg())
        sink(edge(1))
        self.assertTrue(wait_for(lambda: len(self.api.stored) == 1))
        sent = [b["events"][0]["occurredAt"] for b in self.api.batches()]
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(set(sent)), 1)

    def test_restart_replays_spool(self):
        # Simulate a power cut: edges on disk, nothing sent, new process starts.
        c = sqlite3.connect(self.spool)
        c.executescript(open(os.path.join(os.path.dirname(__file__), "..", "plc", "sinks.py")).read()
                        .split('_SCHEMA = """')[1].split('"""')[0])
        c.executemany("INSERT INTO pending (t, machine_id, event) VALUES (?,?,?)",
                      [(T0 + i, "TMC-250", "rise" if i % 2 == 0 else "fall") for i in range(30)])
        c.commit()
        c.close()
        rest_sink(self.cfg())
        self.assertTrue(wait_for(lambda: len(self.api.stored) == 30))
        self.assertEqual([e["machineId"] for e in self.api.stored], ["TMC-250"] * 30)

    def test_rejected_events_are_buried_not_retried(self):
        self.api.batch_reply = {"accepted": 2, "duplicates": 0,
                                "rejected": [{"index": 1, "error": "Unknown or inactive machine: X"}]}
        sink = rest_sink(self.cfg())
        for i in range(3):
            sink(edge(i))
        self.assertTrue(wait_for(lambda: self.db("select count(*) from pending")[0][0] == 0))
        dead = self.db("select event, reason from dead")
        self.assertEqual(len(dead), 1)
        self.assertIn("Unknown or inactive machine", dead[0][1])
        time.sleep(0.2)
        total_events = sum(len(b["events"]) for b in self.api.batches())
        self.assertEqual(total_events, 3)                      # nothing resent

    def test_404_on_batch_falls_back_to_single_route_in_order(self):
        self.api.batch_status = 404
        sink = rest_sink(self.cfg())
        for i in range(10):
            sink(edge(i))
        self.assertTrue(wait_for(lambda: len(self.api.stored) == 10))
        self.assertEqual([e["edge"] for e in self.api.stored],
                         ["rise" if i % 2 == 0 else "fall" for i in range(10)])
        self.assertEqual(len(self.api.batches()), 1)           # not retried until recheck
        sink(edge(10))
        self.assertTrue(wait_for(lambda: len(self.api.stored) == 11))
        self.assertEqual(len(self.api.batches()), 1)

    def test_400_on_batch_falls_back_to_single_route(self):
        self.api.batch_status = 400
        sink = rest_sink(self.cfg())
        for i in range(5):
            sink(edge(i))
        self.assertTrue(wait_for(lambda: len(self.api.stored) == 5))
        self.assertEqual(self.db("select count(*) from pending")[0][0], 0)
        self.assertEqual(self.db("select count(*) from dead")[0][0], 0)

    def test_401_keeps_everything(self):
        self.api.batch_status = 401
        sink = rest_sink(self.cfg())
        for i in range(5):
            sink(edge(i))
        time.sleep(0.4)
        self.assertEqual(self.db("select count(*) from pending")[0][0], 5)
        self.assertEqual(self.db("select count(*) from dead")[0][0], 0)
        self.api.batch_status = 201
        self.assertTrue(wait_for(lambda: len(self.api.stored) == 5))

    def test_413_halves_the_batch(self):
        self.api.max_batch = 25
        self.api.latency = 0.05          # let the backlog build so the first batch is big
        sink = rest_sink(self.cfg())
        for i in range(100):
            sink(edge(i))
        self.assertTrue(wait_for(lambda: len(self.api.stored) == 100))
        self.assertEqual(self.db("select count(*) from dead")[0][0], 0)

    def test_full_spool_drops_oldest_only(self):
        self.api.batch_status = 503
        sink = rest_sink(self.cfg(queue_max=100, retry_base=5, retry_max=5))
        for i in range(150):
            sink(edge(i))
        self.assertTrue(wait_for(lambda: self.db("select count(*) from pending")[0][0] <= 100))
        time.sleep(0.3)
        rows = self.db("select t from pending order by id")
        self.assertLessEqual(len(rows), 100)
        self.assertEqual(rows[-1][0], T0 + 149 * 0.5)          # newest kept


if __name__ == "__main__":
    unittest.main()

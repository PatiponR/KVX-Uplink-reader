"""Low-level KEYENCE host-link (port 8501) transport. READ-ONLY: only RDS is
sent. This module knows nothing about "signals" -- just channels and words --
so it stays unchanged no matter what gets built on top of it.
"""
import socket
import sys
import time


class HostLink:
    def __init__(self, host, port=8501, connect_timeout=5, recv_timeout=2, retry_interval=5):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout
        self.retry_interval = retry_interval
        self.reconnects = 0
        self.s = None
        self._connect_with_retry()

    def connect(self):
        """One connection attempt. Raises on failure."""
        self.s = socket.create_connection((self.host, self.port), self.connect_timeout)
        self.s.settimeout(self.recv_timeout)
        # small request/response pairs: Nagle would coalesce and add ~40 ms/poll
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _connect_with_retry(self):
        """Keep trying, every `retry_interval` seconds, until connect()
        succeeds. Used both for the first connection (so a Pi that boots
        before the PLC/network is reachable just waits instead of crashing)
        and for any later reconnect after the link drops."""
        attempt = 0
        while True:
            try:
                self.connect()
                if attempt:
                    print(f"[hostlink] connected to {self.host}:{self.port}", file=sys.stderr)
                return
            except OSError as e:
                attempt += 1
                print(f"[hostlink] cannot reach {self.host}:{self.port} ({e}); "
                      f"retrying in {self.retry_interval}s... (attempt {attempt})",
                      file=sys.stderr)
                time.sleep(self.retry_interval)

    def words(self, dev, start, n):
        """One round trip: read n consecutive channels starting at `start`.
        Returns a list of n ints, or None if the read failed -- in which
        case a reconnect (retrying every `retry_interval`s) has already run
        before this returns, so the next call starts on a fresh connection."""
        try:
            self.s.sendall(f"RDS {dev}{start}.U {n}\r".encode())
            buf = b""
            while not (buf.endswith(b"\r") or buf.endswith(b"\n")):
                d = self.s.recv(4096)
                if not d:
                    raise IOError("closed")
                buf += d
            vals = [int(x) for x in buf.decode("ascii", "replace").split()]
            return vals if len(vals) == n else None
        except Exception:
            self.reconnects += 1
            try:
                self.s.close()
            except Exception:
                pass
            self._connect_with_retry()
            return None

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

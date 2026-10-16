"""Deployment settings, in one place.

Every field can be overridden with an environment variable of the same name
(upper-cased) instead of editing this file -- handy when the same script
runs on several Pis/machines pointed at different endpoints:

    REST_BASE_URL=https://api.example.com \
    REST_API_KEY=<your key> \
    python3 watch_signals.py --rest

There's no separate machine ID here -- the machineId posted for each edge is
just that signal's name from plc/signals.py's SIGNALS list, so adding a
signal there automatically gives it its own machineId with no config change.

REST_API_KEY is a secret -- it deliberately has no default here and is never
committed. Set it in /etc/watch-signals.env on the Pi (see deploy/README.md),
which is EnvironmentFile'd into the systemd service and gitignored.
"""
import os
from dataclasses import dataclass


def _env(name, default):
    return os.environ.get(name, default)


def _default_spool_path():
    """Where plc/sinks.py's rest_sink keeps edges the API hasn't accepted yet.

    Under systemd the unit's StateDirectory= gives us /var/lib/watch-signals,
    which survives reboots and is owned by the service user -- systemd passes
    it in $STATE_DIRECTORY. Running by hand from a checkout there's no such
    variable, so the spool lands in the working directory (gitignored).
    """
    state_dir = os.environ.get("STATE_DIRECTORY", "")
    # systemd colon-separates these when a unit declares several; we only ever
    # declare one, but take the first entry rather than trusting that.
    base = state_dir.split(":")[0] if state_dir else "."
    return os.path.join(base, "spool.db")


@dataclass
class RestConfig:
    base_url: str = _env("REST_BASE_URL", "http://localhost:4000")
    api_key: str = _env("REST_API_KEY", "")
    timeout: float = float(_env("REST_TIMEOUT", "10"))

    # Durable spool: every edge is written here before any network attempt and
    # removed only once the API confirms it, so an internet outage or a power
    # cut costs nothing.
    spool_path: str = _env("REST_SPOOL_PATH", _default_spool_path())
    # Most edges sent in one POST to /api/transactions/batch (server allows 500).
    batch_max: int = int(_env("REST_BATCH_MAX", "200"))
    # Cap on the spool. When it is full the oldest edges are dropped (in chunks,
    # logged) so a very long outage can't fill the SD card.
    queue_max: int = int(_env("REST_QUEUE_MAX", "50000"))
    # Backoff between retries after a temporary failure: starts at retry_base,
    # doubles on each failure, caps at retry_max, resets after a success.
    retry_base: float = float(_env("REST_RETRY_BASE", "1"))
    retry_max: float = float(_env("REST_RETRY_MAX", "30"))
    # If the server answers 404 on /batch (older than the feature), edges go
    # one at a time to /api/transactions and /batch is tried again this often.
    batch_recheck: float = float(_env("REST_BATCH_RECHECK", "300"))

    # Log every outgoing POST (body, status, response, time) -- for tracking
    # down why one machine's edges never reach the API. Off by default so the
    # journal stays quiet; failures are logged regardless.
    log_requests: bool = _env("REST_LOG_REQUESTS", "0").lower() in ("1", "true", "yes", "on")

    @property
    def transactions_url(self):
        return f"{self.base_url.rstrip('/')}/api/transactions"

    @property
    def batch_url(self):
        return f"{self.transactions_url}/batch"


REST = RestConfig()

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


@dataclass
class RestConfig:
    base_url: str = _env("REST_BASE_URL", "http://localhost:4000")
    api_key: str = _env("REST_API_KEY", "")
    timeout: float = float(_env("REST_TIMEOUT", "3"))

    @property
    def transactions_url(self):
        return f"{self.base_url.rstrip('/')}/api/transactions"


REST = RestConfig()

#!/usr/bin/env python3
"""Live observer for known digital signals on the PLC, over KEYENCE host-link
(port 8501). READ-ONLY: only RDS commands are sent.

Currently watching:
  400T  =  R00001   (confirmed live: ~3.3 s ON, ~18 s period)

This file is just the CLI: connect, poll in a loop, hand every edge to the
display and any configured sinks. The actual logic lives in plc/:

  plc/hostlink.py  transport (talks host-link)
  plc/signals.py   SIGNALS registry + edge detection (add signals here)
  plc/display.py   terminal rendering
  plc/config.py    deployment settings (REST base URL, machine ID, ...)
  plc/sinks.py     pluggable outputs -- CSV and a REST POST today; adding
                   another is a new function there plus one line in
                   build_sinks() below, nothing else changes

  python3 watch_signals.py                  # live view
  python3 watch_signals.py --csv edges.csv  # also append every edge to CSV
  python3 watch_signals.py --rest           # also POST every edge, see plc/config.py
"""
import argparse
import sys
import time

from plc.hostlink import HostLink
from plc.signals import SignalWatcher, SIGNALS
from plc.sinks import csv_sink, rest_sink
from plc.config import REST
from plc import display


def build_sinks(args):
    sinks = []
    if args.csv:
        sinks.append(csv_sink(args.csv))
    if args.rest:
        sinks.append(rest_sink(REST))
        print(f"[rest_sink] posting to {REST.transactions_url} "
              f"(machineId = each signal's name from plc/signals.py)")
        print(f"[rest_sink] failed POSTs spool to {REST.spool_path} and replay "
              f"when the API is reachable again")
    return sinks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.210")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--csv", help="append one row per edge")
    ap.add_argument("--rest", action="store_true",
                     help="POST every edge to REST_BASE_URL/api/transactions (see plc/config.py)")
    ap.add_argument("--retry-interval", type=float, default=5,
                     help="seconds between reconnect attempts while the PLC is unreachable (default 5)")
    args = ap.parse_args()

    watcher = SignalWatcher(SIGNALS)
    sinks = build_sinks(args)

    # display.render repaints the whole screen with ANSI escapes ~4x a second.
    # That's the point interactively, but under systemd stdout is the journal,
    # where it becomes hundreds of MB a day of escape sequences -- churning the
    # SD card and burying the [rest_sink] lines you actually need to read. So
    # only draw when there's a terminal to draw on.
    draw = sys.stdout.isatty()

    recent = []
    polls = 0
    t0 = time.time()
    last_draw = 0
    hl = None

    try:
        # HostLink() itself retries indefinitely if the PLC is unreachable,
        # so Ctrl-C needs to work during that wait too, not just once polling
        # has started.
        hl = HostLink(args.host, args.port, retry_interval=args.retry_interval)
        while True:
            edges = watcher.poll(hl)
            polls += 1
            if edges is None:
                continue
            for edge in edges:
                recent.append(edge)
                for sink in sinks:
                    sink(edge)
            recent[:] = recent[-14:]

            t = time.time()
            if draw and t - last_draw > 0.25:
                last_draw = t
                display.render(watcher, args.host, args.port, polls, hl.reconnects, t0, recent)
    except KeyboardInterrupt:
        print()
        if not polls:
            print("interrupted before connecting")
        elif draw:
            display.summary(watcher, polls, t0)
    finally:
        if hl:
            hl.close()


if __name__ == "__main__":
    main()

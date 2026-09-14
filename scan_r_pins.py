#!/usr/bin/env python3
"""One-shot / continuous scanner over a range of PLC R channels, to find
which pins currently have (or start having) a signal "hot" (bit = 1) --
including pins not yet named in plc/signals.py. READ-ONLY: only RDS is sent.

Standalone from watch_signals.py on purpose: this is for exploring/discovering
pins, not the steady-state watch loop, so it is deliberately NOT wired into
plc/signals.py's SIGNALS list.

    python3 scan_r_pins.py                       # snapshot: every R bit ON right now (R0..R63)
    python3 scan_r_pins.py --start 0 --count 256  # scan a wider range
    python3 scan_r_pins.py --watch                # keep polling, print each bit as it turns on/off
"""
import argparse
import sys
import time

from plc.hostlink import HostLink
from plc.signals import ALL_SIGNALS

CHUNK = 16  # channels per RDS call

_NAMES = {(dev, chan, bit): name for name, dev, chan, bit in ALL_SIGNALS}


def label(dev, chan, bit):
    name = _NAMES.get((dev, chan, bit))
    tag = f"{dev}{chan:03d}{bit:02d}"
    return f"{tag} ({name})" if name else tag


def read_words(hl, dev, start, count):
    """Read `count` consecutive channels starting at `start`, chunked so a
    wide scan doesn't rely on the PLC accepting an arbitrarily large RDS.
    Returns {chan: word}, or None if any chunk failed."""
    words = {}
    chan = start
    remaining = count
    while remaining > 0:
        n = min(CHUNK, remaining)
        vals = hl.words(dev, chan, n)
        if vals is None:
            return None
        for i, v in enumerate(vals):
            words[chan + i] = v
        chan += n
        remaining -= n
    return words


def scan_once(hl, dev, start, count):
    words = read_words(hl, dev, start, count)
    if words is None:
        return None
    hot = []
    for chan, w in sorted(words.items()):
        for bit in range(16):
            if w >> bit & 1:
                hot.append((chan, bit))
    return hot


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.1.210")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--dev", default="R", help="device letter to scan (default R)")
    ap.add_argument("--start", type=int, default=0, help="first channel to scan (default 0)")
    ap.add_argument("--count", type=int, default=64,
                     help="number of channels to scan (default 64 = 1024 bits)")
    ap.add_argument("--watch", action="store_true",
                     help="keep polling and report bits as they turn on/off, "
                          "instead of a single snapshot")
    ap.add_argument("--interval", type=float, default=0.5,
                     help="seconds between polls in --watch mode (default 0.5)")
    ap.add_argument("--retry-interval", type=float, default=5,
                     help="seconds between reconnect attempts while the PLC is unreachable")
    args = ap.parse_args()

    end = args.start + args.count - 1
    hl = HostLink(args.host, args.port, retry_interval=args.retry_interval)
    try:
        if not args.watch:
            hot = scan_once(hl, args.dev, args.start, args.count)
            if hot is None:
                print("read failed", file=sys.stderr)
                sys.exit(1)
            if not hot:
                print(f"no {args.dev} bits ON in {args.dev}{args.start}..{args.dev}{end}")
                return
            print(f"{len(hot)} bit(s) ON in {args.dev}{args.start}..{args.dev}{end}:")
            for chan, bit in hot:
                print(f"  {label(args.dev, chan, bit)}")
            return

        prev = None
        print(f"watching {args.dev}{args.start}..{args.dev}{end} "
              f"({args.count} channels, Ctrl-C to stop)...")
        while True:
            words = read_words(hl, args.dev, args.start, args.count)
            if words is None:
                time.sleep(args.interval)
                continue
            if prev is not None:
                for chan, w in sorted(words.items()):
                    pw = prev.get(chan)
                    if pw is None:
                        continue
                    changed = w ^ pw
                    if not changed:
                        continue
                    for bit in range(16):
                        if changed >> bit & 1:
                            state = "ON " if (w >> bit & 1) else "off"
                            ts = time.strftime("%H:%M:%S")
                            print(f"[{ts}] {label(args.dev, chan, bit)} -> {state}")
            prev = words
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
    finally:
        hl.close()


if __name__ == "__main__":
    main()

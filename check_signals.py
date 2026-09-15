#!/usr/bin/env python3
"""Debugging check for the PLC signals mapped in plc/signals.py, over KEYENCE
host-link (port 8501). READ-ONLY: only RDS (read words) and ?M (read PLC mode)
are sent.

watch_signals.py is built to keep running no matter what -- it retries
forever, and a failed read silently skips the whole poll. That's right for
production and no help when a machine goes quiet and you need to know why.
This script fails fast and says what it saw, one layer at a time:

  signals.py mapping -> network/TCP -> PLC mode -> channel reads -> bit levels -> edges

If it shows a machine's signal toggling, the PLC side is fine and the problem
is downstream (the watch-signals service, REST, the API).

  python3 check_signals.py                         # menu -- pick a mode
  python3 check_signals.py machine B8-45           # diagnose one machine (start here)
  python3 check_signals.py check                   # quick health check of everything
  python3 check_signals.py config                  # lint signals.py only, no PLC needed
  python3 check_signals.py snapshot                # read every signal's level once
  python3 check_signals.py watch -m B8-45 -s 120   # live edges, flags stuck signals
  python3 check_signals.py scan --channels 0-4     # find active bits NOT in signals.py

Common options: --host, --port, --timeout, -s/--seconds (0 = until Ctrl-C),
--no-color. Safe to run while the watch-signals service is running.

Exit code: 0 nothing wrong found, 1 problems found, 2 PLC unreachable.
"""
import argparse
import difflib
import os
import socket
import sys
import time
from collections import defaultdict

from plc.hostlink import HostLink
from plc.signals import SIGNALS, SERVICE_SIGNALS, ALL_SIGNALS, make_ranges

DEFAULT_HOST = "192.168.1.210"   # same defaults as watch_signals.py
DEFAULT_PORT = 8501
POLL_GAP = 0.05                  # between poll cycles: fast enough for ~s pulses, easy on the PLC

# Every mapped signal as (name, kind, dev, chan, bit); kind is which list it's in.
ENTRIES = ([(n, "run", d, c, b) for n, d, c, b in SIGNALS]
           + [(n, "service", d, c, b) for n, d, c, b in SERVICE_SIGNALS])

# (dev, chan, bit) -> [(name, kind), ...]; more than one entry is a mapping error.
ADDR = defaultdict(list)
for _n, _k, _d, _c, _b in ENTRIES:
    ADDR[(_d, _c, _b)].append((_n, _k))

# The batched reads watch_signals.py actually sends, via SignalWatcher.
PROD_RANGES = make_ranges({(d, c) for _, d, c, _ in ALL_SIGNALS})

KNOWN_DEVICES = {"R", "MR", "LR", "CR", "B", "DM", "EM", "FM", "W", "ZF", "TM"}

PLC_ERRORS = {
    "E0": "device number error -- that device/channel doesn't exist on this PLC",
    "E1": "command error -- the PLC didn't understand the command",
    "E2": "program not registered",
    "E4": "write protected",
    "E5": "unit error",
    "E6": "no comment",
}
PLC_MODES = {"0": "PROGRAM", "1": "RUN"}


# --- output -----------------------------------------------------------------

COLOR = False


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if COLOR else str(s)


def bold(s):   return _c("1", s)
def green(s):  return _c("92", s)
def yellow(s): return _c("93", s)
def red(s):    return _c("91", s)
def grey(s):   return _c("90", s)


def heading(title):
    print(f"\n{bold('-- ' + title)}")


def label(dev, chan, bit):
    return f"{dev}{chan:03d}{bit:02d}"


def who(key):
    """(name, kind) for an address, or ("(unmapped)", "") if signals.py doesn't list it."""
    items = ADDR.get(key)
    if not items:
        return "(unmapped)", ""
    return "/".join(n for n, _ in items), "/".join(k for _, k in items)


class Report:
    """Prints findings as they're found and decides the exit code."""

    def __init__(self):
        self.problems = 0
        self.unreachable = False

    def _line(self, tag, msg):
        print(f"  {tag} {msg}", flush=True)

    def ok(self, msg):    self._line(green("OK   "), msg)
    def note(self, msg):  self._line(grey("NOTE "), msg)
    def hint(self, msg):  print(f"        {grey(msg)}", flush=True)

    def warn(self, msg):
        self.problems += 1
        self._line(yellow("WARN "), msg)

    def error(self, msg):
        self.problems += 1
        self._line(red("ERROR"), msg)

    @property
    def exit_code(self):
        return 2 if self.unreachable else (1 if self.problems else 0)


def finish(report):
    heading("result")
    if report.unreachable:
        print(red("  PLC unreachable -- fix the network/PLC first (see above)"))
    elif report.problems:
        print(yellow(f"  {report.problems} problem(s) found -- see the WARN/ERROR lines above"))
    else:
        print(green("  nothing wrong found"))
    return report.exit_code


# --- PLC access -------------------------------------------------------------

class Link(HostLink):
    """HostLink that tries once and raises instead of retrying forever, and
    hands back the PLC's raw reply so errors like E0 can be shown."""

    def _connect_with_retry(self):
        self.connect()

    def ask(self, cmd):
        """Send one command, return the reply text. Socket trouble raises OSError."""
        self.s.sendall(f"{cmd}\r".encode())
        buf = b""
        while not buf.endswith((b"\r", b"\n")):
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("PLC closed the connection")
            buf += chunk
        return buf.decode("ascii", "replace").strip()


def read_words(link, dev, start, n=1):
    """RDS n words starting at dev/start -- same command watch_signals.py sends.
    Returns (values, None) or (None, what went wrong). A dead link raises OSError."""
    reply = link.ask(f"RDS {dev}{start}.U {n}")
    parts = reply.split()
    if len(parts) == n and all(p.isdigit() for p in parts):
        return [int(p) for p in parts], None
    if reply in PLC_ERRORS:
        return None, f"PLC replied {reply}: {PLC_ERRORS[reply]}"
    return None, f"unexpected reply {reply!r} (wanted {n} number{'s' if n > 1 else ''})"


class Session:
    def __init__(self, args, report):
        self.args = args
        self.report = report
        self.link = None

    def _new_link(self):
        a = self.args
        return Link(a.host, a.port, connect_timeout=a.timeout, recv_timeout=a.timeout)

    def open(self):
        """Connect once and check the PLC answers. Reports why if not."""
        a, r = self.args, self.report
        heading(f"connection to {a.host}:{a.port}")
        t0 = time.perf_counter()
        try:
            self.link = self._new_link()
        except TimeoutError:
            r.error(f"no answer from {a.host}:{a.port} within {a.timeout:g}s")
            r.hint(f"PLC powered off, wrong IP, or network/cable/switch down. Try: ping {a.host}")
        except ConnectionRefusedError:
            r.error(f"{a.host} refused port {a.port}")
            r.hint("the host is up but nothing is listening on that port -- check the port and the")
            r.hint("PLC's Ethernet/host-link settings, or it may have too many clients connected")
        except socket.gaierror as e:
            r.error(f"can't resolve host {a.host!r}: {e}")
        except OSError as e:
            r.error(f"cannot reach {a.host}:{a.port}: {e}")
            r.hint("check this machine's own network (cable/wifi, IP and subnet) and that the PLC is on it")
        else:
            r.ok(f"TCP connected in {(time.perf_counter() - t0) * 1000:.0f} ms")
            return self._check_mode()
        r.unreachable = True
        return False

    def _check_mode(self):
        r = self.report
        try:
            reply = self.link.ask("?M")
        except OSError as e:
            r.error(f"connected, but no host-link reply: {e}")
            r.hint("something is listening on this port, but it may not be the KEYENCE PLC -- check --host/--port")
            r.unreachable = True
            self.close()
            return False
        mode = PLC_MODES.get(reply)
        if mode == "RUN":
            r.ok("PLC answers host-link, mode RUN")
        elif mode:
            r.warn(f"PLC is in {mode} mode -- the ladder isn't running, so relays won't update")
        else:
            r.note(f"PLC answered ?M with {reply!r} (mode unknown, carrying on)")
        return True

    def reconnect(self):
        """Mid-watch, after the link drops: one attempt, then back to the loop."""
        self.close()
        try:
            self.link = self._new_link()
        except OSError:
            time.sleep(1)

    def close(self):
        if self.link:
            self.link.close()
            self.link = None


# --- watching ---------------------------------------------------------------

class Tracker:
    """Edge counting for every bit of a set of channels, fed one word at a time."""

    def __init__(self, channels):
        self.channels = sorted(channels)
        self.words = {}                  # (dev, chan) -> latest word
        self.rises = defaultdict(int)    # (dev, chan, bit) -> count
        self.falls = defaultdict(int)
        self.rise_t = {}
        self.widths = defaultdict(list)
        self.errors = defaultdict(int)   # (dev, chan) -> failed reads
        self.last_error = {}
        self.link_drops = 0
        self.polls = 0
        self.t0 = time.time()
        self.elapsed = 0.0

    def feed(self, key, word, t):
        """Returns [(bit, "rise"|"fall", width_or_None)] for the bits that changed."""
        prev = self.words.get(key)
        self.words[key] = word
        if prev is None:
            return []
        out = []
        changed = prev ^ word
        for bit in range(16):
            if not changed >> bit & 1:
                continue
            k = (*key, bit)
            if word >> bit & 1:
                self.rises[k] += 1
                self.rise_t[k] = t
                out.append((bit, "rise", None))
            else:
                self.falls[k] += 1
                width = t - self.rise_t[k] if k in self.rise_t else None
                if width is not None:
                    self.widths[k].append(width)
                out.append((bit, "fall", width))
        return out

    def level(self, dev, chan, bit):
        w = self.words.get((dev, chan))
        return None if w is None else w >> bit & 1

    def changes(self, k):
        return self.rises[k] + self.falls[k]


def run_loop(session, tracker, seconds, on_edge):
    """Poll each channel on its own until time's up or Ctrl-C."""
    tracker.t0 = time.time()
    end = tracker.t0 + seconds if seconds else None
    span = f"{seconds:g}s" if seconds else "until Ctrl-C"
    print(grey(f"  polling {', '.join(f'{d}{c}' for d, c in tracker.channels)} for {span} "
               f"(Ctrl-C stops early and still prints the summary)"), flush=True)
    try:
        while end is None or time.time() < end:
            if session.link is None:
                session.reconnect()
                continue
            for key in tracker.channels:
                try:
                    vals, err = read_words(session.link, *key)
                except OSError as e:
                    tracker.link_drops += 1
                    print(f"  {time.time() - tracker.t0:8.2f}s  {red('link dropped')}: {e} -- reconnecting",
                          flush=True)
                    session.reconnect()
                    break
                if err:
                    if not tracker.errors[key]:
                        print(f"  {time.time() - tracker.t0:8.2f}s  {red('read failed')} "
                              f"{key[0]}{key[1]}: {err}", flush=True)
                    tracker.errors[key] += 1
                    tracker.last_error[key] = err
                    continue
                t = time.time()
                for bit, event, width in tracker.feed(key, vals[0], t):
                    on_edge((*key, bit), event, width, t)
            tracker.polls += 1
            time.sleep(POLL_GAP)
    except KeyboardInterrupt:
        print(grey("\n  stopped"))
    tracker.elapsed = time.time() - tracker.t0


def edge_line(tracker, k, event, width, t):
    name, kind = who(k)
    name_col = f"{name:<12}"
    if not kind:
        name_col = yellow(name_col)
    tag = green("RISE") if event == "rise" else "FALL"
    w = f"  width {width * 1000:.0f} ms" if width else ""
    print(f"  {t - tracker.t0:8.2f}s  {name_col} {kind:<8} {label(*k)}  {tag}{w}", flush=True)


def summarize_signals(report, tracker, entries):
    heading(f"summary after {tracker.elapsed:.0f}s ({tracker.polls} polls)")
    print(grey(f"  {'signal':<12} {'kind':<8} {'addr':<7} {'pulses':>6}  {'width avg/min/max ms':<21} status"))
    stuck, no_data = [], []
    for name, kind, dev, chan, bit in entries:
        k = (dev, chan, bit)
        lvl = tracker.level(dev, chan, bit)
        ws = tracker.widths[k]
        width = (f"{sum(ws) / len(ws) * 1000:.0f}/{min(ws) * 1000:.0f}/{max(ws) * 1000:.0f}"
                 if ws else "-")
        now = "ON" if lvl else "off"
        if lvl is None:
            status = red("NO DATA")
            no_data.append(name)
        elif tracker.changes(k):
            status = green(f"toggling, now {now}")
        elif kind == "run":
            status = yellow(f"STUCK {now}")
            stuck.append(f"{name} ({now})")
        else:
            status = grey(f"steady {now}")
        print(f"  {name:<12} {kind:<8} {label(*k):<7} {tracker.rises[k]:>6}  {width:<21} {status}")

    for (dev, chan), n in sorted(tracker.errors.items()):
        report.error(f"{dev}{chan}: {n} failed read(s) -- last: {tracker.last_error[(dev, chan)]}")
    if no_data:
        report.error(f"never got a value for: {', '.join(no_data)}")
    if tracker.link_drops:
        report.warn(f"the link dropped {tracker.link_drops}x while watching -- an unstable network loses data too")
    if stuck:
        report.warn(f"{len(stuck)} run signal(s) never changed: {', '.join(stuck)}")
        if tracker.elapsed < 60:
            report.hint("short watch -- a machine that's just idle looks stuck too; try -s 120 or longer")
        else:
            report.hint("either the machine was idle the whole time, or its input isn't reaching the PLC")
    elif not no_data and not tracker.errors:
        report.ok("every run signal changed at least once")


# --- modes --------------------------------------------------------------------

def lint_config(report, focus=None):
    """Static checks on signals.py. `focus` = set of names to limit the output to."""
    heading("signals.py mapping")
    before = report.problems

    def relevant(names):
        return focus is None or any(n in focus for n in names)

    for name, kind, dev, chan, bit in ENTRIES:
        if not relevant([name]):
            continue
        where = f"{name} ({kind}) dev {dev} chan {chan} bit {bit}"
        if not (isinstance(bit, int) and 0 <= bit <= 15):
            report.error(f"{where}: bit must be 0-15")
        if not (isinstance(chan, int) and chan >= 0):
            report.error(f"{where}: channel must be a whole number >= 0")
        if dev not in KNOWN_DEVICES:
            report.warn(f"{where}: unusual device {dev!r}")
        if not name or name != name.strip():
            report.error(f"{where}: name is empty or has spaces around it -- it's posted as the machineId")

    for addr, users in ADDR.items():
        if len(users) > 1 and relevant(n for n, _ in users):
            report.error(f"{label(*addr)} is mapped more than once: "
                         f"{', '.join(f'{n} ({k})' for n, k in users)} -- one input can't be two signals")

    for kind, lst in (("run", SIGNALS), ("service", SERVICE_SIGNALS)):
        seen = defaultdict(int)
        for n, *_ in lst:
            seen[n] += 1
        for n, count in seen.items():
            if count > 1 and relevant([n]):
                report.error(f"{n} appears {count}x in the {kind} list -- two addresses claim one machine")

    run = {n for n, *_ in SIGNALS}
    svc = {n for n, *_ in SERVICE_SIGNALS}
    for n in sorted(run - svc):
        if relevant([n]):
            report.note(f"{n} has a run signal but no service signal")
    for n in sorted(svc - run):
        if relevant([n]):
            report.note(f"{n} has a service signal but no run signal")

    if report.problems == before:
        report.ok(f"no mapping problems for {', '.join(sorted(focus))}" if focus else
                  f"{len(SIGNALS)} run + {len(SERVICE_SIGNALS)} service signals, no conflicts")
    if focus is None:
        report.hint("watch_signals.py reads: " + ", ".join(f"RDS {d}{s}.U {n}" for d, s, n in PROD_RANGES))


def read_levels(session, report, entries):
    """Read each channel on its own, then the batched reads production sends.
    Returns ({(dev, chan): word}, batched_read_failed)."""
    link = session.link
    channels = sorted({(d, c) for _, _, d, c, _ in entries})
    words = {}

    heading("channel reads, one at a time")
    print(grey(f"  {'chan':<5} {'bit 15 ...... bit 0':<19}  ON bits (* = not in signals.py)"))
    for dev, chan in channels:
        try:
            vals, err = read_words(link, dev, chan)
        except OSError as e:
            report.error(f"link dropped while reading {dev}{chan}: {e}")
            return words, True
        if err:
            names = sorted({n for n, _, d, c, _ in ENTRIES if (d, c) == (dev, chan)})
            report.error(f"{dev}{chan}: {err}")
            report.hint(f"no data for anything on this channel: {', '.join(names)}")
            continue
        w = words[(dev, chan)] = vals[0]
        binary = " ".join(f"{w:016b}"[i:i + 4] for i in range(0, 16, 4))
        on = [str(b) if (dev, chan, b) in ADDR else yellow(f"{b}*") for b in range(16) if w >> b & 1]
        print(f"  {dev + str(chan):<5} {binary}  {', '.join(on) or grey('none')}")

    heading("batched reads, exactly as watch_signals.py sends them")
    batch_failed = False
    for dev, start, n in PROD_RANGES:
        cmd = f"RDS {dev}{start}.U {n}"
        try:
            vals, err = read_words(link, dev, start, n)
        except OSError as e:
            report.error(f"link dropped on {cmd}: {e}")
            return words, True
        if err:
            batch_failed = True
            report.error(f"{cmd} -> {err}")
            report.hint("watch_signals.py drops the WHOLE poll when any read fails, so NO machine gets data")
        else:
            report.ok(f"{cmd} -> {' '.join(map(str, vals))}")

    heading("signal levels right now")
    print(grey(f"  {'machine':<12} {'run':<12} service"))
    names = list(dict.fromkeys(e[0] for e in entries))
    for name in names:
        cells = []
        for kind in ("run", "service"):
            parts = []
            for _, _, d, c, b in (e for e in entries if e[0] == name and e[1] == kind):
                w = words.get((d, c))
                lvl = grey("n/a") if w is None else (green("ON ") if w >> b & 1 else "off")
                parts.append(f"{label(d, c, b)} {lvl}")
            cells.append("  ".join(parts) if parts else grey("-         "))
        print(f"  {name:<12} {cells[0]}  {cells[1]}")
    return words, batch_failed


def cmd_config(args):
    report = Report()
    lint_config(report)
    return finish(report)


def cmd_check(args):
    report = Report()
    lint_config(report)
    session = Session(args, report)
    if session.open():
        read_levels(session, report, ENTRIES)
        session.close()
    return finish(report)


def cmd_snapshot(args):
    report = Report()
    session = Session(args, report)
    if session.open():
        read_levels(session, report, ENTRIES)
        session.close()
    return finish(report)


def find_machine(name, report):
    """All ENTRIES for a machine name (run + service), or None after reporting why."""
    hits = [e for e in ENTRIES if e[0] == name]
    if not hits:
        hits = [e for e in ENTRIES if e[0].lower() == name.lower()]
        if hits:
            report.note(f"using {hits[0][0]} (names are case-sensitive in signals.py and the API)")
    if hits:
        return hits
    names = machine_names()
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
    report.error(f"{name!r} isn't in plc/signals.py")
    report.hint(("did you mean: " + ", ".join(close)) if close else "known: " + ", ".join(names))
    return None


def machine_names():
    return list(dict.fromkeys(e[0] for e in ENTRIES))


def cmd_watch(args):
    report = Report()
    entries = ENTRIES
    if args.machine:
        entries = find_machine(args.machine, report)
        if entries is None:
            return finish(report)
    session = Session(args, report)
    if not session.open():
        return finish(report)

    heading("live edges")
    wanted = {(d, c, b) for _, _, d, c, b in entries}
    tracker = Tracker({(d, c) for _, _, d, c, _ in entries})

    def on_edge(k, event, width, t):
        if k in wanted:
            edge_line(tracker, k, event, width, t)

    run_loop(session, tracker, args.seconds, on_edge)
    session.close()
    summarize_signals(report, tracker, entries)
    return finish(report)


def unmapped_active(tracker):
    return [(d, c, b) for d, c in tracker.channels for b in range(16)
            if (d, c, b) not in ADDR and tracker.changes((d, c, b))]


def cmd_scan(args):
    report = Report()
    channels = {(d, c) for _, _, d, c, _ in ENTRIES} | {(args.dev, c) for c in args.channels}
    session = Session(args, report)
    if not session.open():
        return finish(report)

    heading("scanning all 16 bits -- live output shows only bits NOT in signals.py")
    tracker = Tracker(channels)

    def on_edge(k, event, width, t):
        if k not in ADDR:
            edge_line(tracker, k, event, width, t)

    run_loop(session, tracker, args.seconds, on_edge)
    session.close()

    heading(f"scan result after {tracker.elapsed:.0f}s ({tracker.polls} polls) -- quiet unmapped bits hidden")
    for dev, chan in tracker.channels:
        if (dev, chan) not in tracker.words:
            report.error(f"{dev}{chan}: never read successfully -- {tracker.last_error.get((dev, chan), 'link down')}")
            continue
        print(f"  {bold(dev + str(chan))}")
        shown = 0
        for bit in range(16):
            k = (dev, chan, bit)
            name, kind = who(k)
            lvl = tracker.level(*k)
            if not kind and not lvl and not tracker.changes(k):
                continue
            shown += 1
            name_col = f"{name + (' (' + kind + ')' if kind else ''):<22}"
            activity = (f"{tracker.rises[k]} pulse(s), now {'ON' if lvl else 'off'}" if tracker.changes(k)
                        else f"steady {'ON' if lvl else 'off'}")
            print(f"    {label(*k)}  {name_col if kind else yellow(name_col)} {activity}")
        if not shown:
            print(grey("    nothing mapped, all bits stayed off"))

    active = unmapped_active(tracker)
    if active:
        report.warn("bits changing that aren't in signals.py: "
                    + ", ".join(f"{label(*k)} ({tracker.rises[k]} pulses)" for k in active))
        for dev, chan in sorted({k[:2] for k in active}):
            quiet = [n for n, kd, d, c, b in ENTRIES
                     if (d, c) == (dev, chan) and kd == "run" and not tracker.changes((d, c, b))]
            if quiet:
                report.hint(f"{dev}{chan} also has quiet run signals: {', '.join(quiet)} -- was an input moved?")
    else:
        report.ok("no unmapped bits changed")
    on_unmapped = [label(d, c, b) for d, c in tracker.channels for b in range(16)
                   if (d, c, b) not in ADDR and tracker.level(d, c, b) and not tracker.changes((d, c, b))]
    if on_unmapped:
        report.note(f"unmapped bits that stayed ON: {', '.join(on_unmapped)}")
    if not args.channels:
        report.hint("only mapped channels were scanned; add e.g. --channels 0-9 to look further")
    return finish(report)


def cmd_machine(args):
    report = Report()
    heading(f"machine {args.name}")
    entries = find_machine(args.name, report)
    if entries is None:
        return finish(report)
    name = entries[0][0]
    for _, kind, d, c, b in entries:
        print(f"  {kind:<8} {label(d, c, b)}")

    lint_config(report, focus={name})
    session = Session(args, report)
    if not session.open():
        heading("verdict")
        print(red("  can't reach the PLC -- network, PLC power, or wrong --host/--port. "
                  "No machine gets data until this is fixed."))
        return finish(report)
    _, batch_failed = read_levels(session, report, entries)

    heading(f"watching {name} (plus any unmapped bit on the same channels)")
    mine = {(d, c, b) for _, _, d, c, b in entries}
    tracker = Tracker({(d, c) for _, _, d, c, _ in entries})

    def on_edge(k, event, width, t):
        if k in mine or k not in ADDR:
            edge_line(tracker, k, event, width, t)

    run_loop(session, tracker, args.seconds, on_edge)
    session.close()
    summarize_signals(report, tracker, entries)
    active = unmapped_active(tracker)
    if active:
        report.warn(f"unmapped bits changed on {name}'s channels: "
                    + ", ".join(f"{label(*k)} ({tracker.rises[k]} pulses)" for k in active))

    heading("verdict")
    healthy = not batch_failed
    if batch_failed:
        print(red("  watch_signals.py's batched read fails, so it gets no data for ANY machine "
                  "(see 'batched reads' above)"))
    for _, kind, d, c, b in entries:
        k = (d, c, b)
        lvl = tracker.level(d, c, b)
        now = "ON" if lvl else "off"
        if lvl is None:
            healthy = False
            print(red(f"  {kind} {label(*k)}: no successful reads -- "
                      f"{tracker.last_error.get((d, c), 'link problem')}"))
        elif tracker.changes(k):
            print(green(f"  {kind} {label(*k)}: toggling ({tracker.rises[k]} pulses) -- PLC side OK"))
        elif kind == "run":
            healthy = False
            nearby = [label(*u) for u in active if u[:2] == (d, c)]
            if nearby:
                print(yellow(f"  {kind} {label(*k)}: stuck {now}, but unmapped {', '.join(nearby)} changed "
                             f"-- the input has probably moved; check wiring / update signals.py"))
            else:
                print(yellow(f"  {kind} {label(*k)}: stuck {now} for {tracker.elapsed:.0f}s -- machine idle, "
                             f"or its signal isn't reaching the PLC (sensor, wiring, PLC input LED)"))
        else:
            print(grey(f"  {kind} {label(*k)}: steady {now} (normal unless service/operate changed meanwhile)"))
    if healthy:
        print(green(f"  PLC side looks healthy for {name}. If its data still isn't arriving, look downstream:"))
        print(green("  is watch-signals running (systemctl status watch-signals) and is REST posting "
                    "(journalctl -u watch-signals)?"))
    return finish(report)


# --- CLI ----------------------------------------------------------------------

def parse_channels(text):
    """'0-4,7' -> {0, 1, 2, 3, 4, 7}"""
    out = set()
    try:
        for part in filter(None, (p.strip() for p in text.split(","))):
            if "-" in part:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            else:
                out.add(int(part))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected channels like 0-4,7 -- got {text!r}")
    return out


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=DEFAULT_HOST, help=f"PLC address (default {DEFAULT_HOST})")
    common.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"host-link port (default {DEFAULT_PORT})")
    common.add_argument("--timeout", type=float, default=3,
                        help="seconds to wait to connect / for each reply (default 3)")
    common.add_argument("--no-color", action="store_true", help="plain output")
    timed = argparse.ArgumentParser(add_help=False)
    timed.add_argument("-s", "--seconds", type=float, default=60,
                       help="how long to watch, 0 = until Ctrl-C (default 60)")

    ap = argparse.ArgumentParser(
        description="Check PLC signals against plc/signals.py. Run with no arguments for a menu.",
        epilog="exit code: 0 nothing wrong found, 1 problems found, 2 PLC unreachable")
    sub = ap.add_subparsers(dest="mode", metavar="MODE")

    p = sub.add_parser("machine", parents=[common, timed], help="diagnose one machine end to end (start here)")
    p.add_argument("name", help="machine name as in signals.py, e.g. B8-45")
    p.set_defaults(func=cmd_machine)

    p = sub.add_parser("check", parents=[common], help="quick health check: mapping + connection + levels")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("watch", parents=[common, timed], help="live edges; flags signals that never change")
    p.add_argument("-m", "--machine", help="only this machine's signals")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("scan", parents=[common, timed], help="find changing bits that aren't in signals.py")
    p.add_argument("--channels", type=parse_channels, default=set(),
                   help="extra channels to scan besides the mapped ones, e.g. 0-9")
    p.add_argument("--dev", default="R", help="device for --channels (default R)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("snapshot", parents=[common], help="read every signal's current level once")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("config", parents=[common], help="check signals.py only, no PLC needed")
    p.set_defaults(func=cmd_config)
    return ap


MENU = [
    ("machine",  "Diagnose one machine     <- start here when a machine has no data"),
    ("check",    "Quick health check       mapping + connection + current levels"),
    ("watch",    "Watch live edges         flags signals that never change"),
    ("scan",     "Scan for unmapped bits   finds inputs that moved off their mapped bit"),
    ("snapshot", "Read current levels once"),
    ("config",   "Check signals.py only    no PLC needed"),
]


def _ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    # lstrip a BOM: Windows PowerShell adds one when input is piped in
    answer = input(f"{prompt}{suffix}: ").lstrip("﻿").strip()
    return answer or ("" if default is None else str(default))


def menu():
    """Numbered menu that builds the same argv a subcommand would get."""
    print(bold("PLC signal check") + grey("  (read-only -- safe while watch-signals is running)"))
    for i, (_, desc) in enumerate(MENU, 1):
        print(f"  {i}) {desc}")
    print("  q) quit")
    try:
        while True:
            c = _ask("choose", 1)
            if c.lower() in ("q", "quit", "exit"):
                return None
            if c.isdigit() and 1 <= int(c) <= len(MENU):
                mode = MENU[int(c) - 1][0]
                break
            if c in dict(MENU):
                mode = c
                break
            print("  pick a number from the list")
        argv = [mode]

        if mode in ("machine", "watch"):
            names = machine_names()
            for i in range(0, len(names), 4):
                print("  " + "".join(f"{j + 1:>3}) {names[j]:<10}" for j in range(i, min(i + 4, len(names)))))
            while True:
                pick = _ask("machine (number or name)" + (", blank = all" if mode == "watch" else ""))
                if pick.isdigit() and 1 <= int(pick) <= len(names):
                    pick = names[int(pick) - 1]
                if pick or mode == "watch":
                    break
            if mode == "machine":
                argv.append(pick)
            elif pick:
                argv += ["-m", pick]

        if mode == "scan":
            extra = _ask("extra channels to scan, e.g. 0-9 (blank = only mapped ones)")
            if extra:
                argv += ["--channels", extra]
        if mode != "config":
            host = _ask("PLC host", DEFAULT_HOST)
            if host != DEFAULT_HOST:
                argv += ["--host", host]
        if mode in ("machine", "watch", "scan"):
            argv += ["-s", _ask("seconds to watch (0 = until Ctrl-C)", 60)]

        print(grey("  same as: python3 check_signals.py " + " ".join(argv)))
        return argv
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def main(argv=None):
    global COLOR
    sys.stdout.reconfigure(line_buffering=True)
    COLOR = sys.stdout.isatty() and "--no-color" not in sys.argv
    if COLOR and os.name == "nt":
        os.system("")   # turns on ANSI colours in the Windows console

    if argv is None and len(sys.argv) == 1:
        argv = menu()
        if argv is None:
            return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    COLOR = COLOR and not args.no_color
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())

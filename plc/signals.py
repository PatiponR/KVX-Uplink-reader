"""Named-signal registry and edge-detection state machine.

A "signal" is just a named bit inside a device channel. To watch more, add a
line to SIGNALS -- SignalWatcher automatically batches the underlying reads
and tracks state for whatever you list here. This module does no I/O and
knows nothing about the terminal or CSV files, so it can be driven by a CLI,
a REST server, a test, whatever -- see watch_signals.py for the reference
caller, and sinks.py for how outputs plug into it.
"""
import time
from dataclasses import dataclass, field

SIGNALS = [
    # name   dev   chan  bit
    ("TMC-400", "R",  0,    0),
    ("TMC-200", "R",  0,    1),
    ("B1-160", "R",  0,    2),
    ("B2-160", "R",  0,    3),
    ("B3-110", "R",  0,    4),
    ("B4-110", "R",  0,    5),
    ("B5-110", "R",  0,    6),
    ("B6-80", "R",  0,    7),
    ("B7-60", "R",  0,    8),
    ("B8-45", "R",  0,    9),
    ("B9-60", "R",  0,   10),
    ("B10-60", "R",  0,   11),
    ("C1-200", "R",  0,   12),
    ("C2-160", "R",  0,   13),
    ("C3-160", "R",  0,   14),
    ("C4-160", "R",  0,   15),
    
    ("C5-160", "R",  3,    0),
    ("C6-60", "R",  3,    1),
    ("C7-60", "R",  3,    2),
    ("C8-80", "R",  3,    3),
]

# Same shape as SIGNALS -- a plain 24V digital bit on the PLC, watched and
# CSV-logged exactly the same way. The only difference is what rest_sink
# posts for the "edge" field: "service" on rising edge and "operate" on
# falling edge, instead of "rise"/"fall". Add entries here (not to SIGNALS)
# for pins that need that labeling.
SERVICE_SIGNALS = [
    # name   dev   chan  bit
    ("TMC-400", "R",  1,    0),
    ("TMC-200", "R",  1,    1),
    ("B1-160", "R",  1,    2),
    ("B2-160", "R",  1,    3),
    ("B3-110", "R",  1,    4),
    ("B4-110", "R",  1,    5),
    ("B5-110", "R",  1,    6),
    ("B6-80", "R",  1,    7),
    ("B7-60", "R",  1,    8),
    ("B8-45", "R",  1,    9),
    ("B9-60", "R",  1,   10),
    ("B10-60", "R",  1,   11),
    ("C1-200", "R",  1,   12),
    ("C2-160", "R",  1,   13),
    ("C3-160", "R",  1,   14),
    ("C4-160", "R",  1,   15),
]

# All watched signals, combined -- what SignalWatcher polls by default.
ALL_SIGNALS = SIGNALS + SERVICE_SIGNALS

# (dev, chan, bit) -> (rise_event, fall_event) for SERVICE_SIGNALS entries;
# anything not in here falls back to the normal "rise"/"fall" pair in poll()
# below. Keyed by address, not name -- a name (machineId) can appear in both
# SIGNALS and SERVICE_SIGNALS for the same machine, and only the
# SERVICE_SIGNALS occurrence should get "service"/"operate".
_SERVICE_EVENTS = {(dev, chan, bit): ("service", "operate") for _, dev, chan, bit in SERVICE_SIGNALS}


@dataclass
class Edge:
    t: float
    name: str
    event: str              # "rise" | "fall", or "service" | "operate" for SERVICE_SIGNALS
    width: float | None     # only set on the falling-edge event
    count: int              # rise count so far, including this edge


@dataclass
class SignalState:
    name: str
    dev: str
    chan: int
    bit: int
    level: int | None = None
    count: int = 0
    rise_t: float | None = None
    widths: list = field(default_factory=list)

    @property
    def label(self):
        return f"{self.dev}{self.chan:03d}{self.bit:02d}"


def make_ranges(channels):
    """Collapse a set of (dev, chan) pairs into the fewest (dev, start, n)
    contiguous runs, so each run can be fetched in a single RDS call."""
    by_dev = {}
    for dev, chan in sorted(channels):
        by_dev.setdefault(dev, []).append(chan)
    ranges = []
    for dev, chans in by_dev.items():
        start = prev = chans[0]
        for c in chans[1:]:
            if c == prev + 1:
                prev = c
                continue
            ranges.append((dev, start, prev - start + 1))
            start = prev = c
        ranges.append((dev, start, prev - start + 1))
    return ranges


class SignalWatcher:
    """Tracks a fixed set of named signals and turns raw channel words into edges."""

    def __init__(self, signals=None):
        self.signals = signals if signals is not None else ALL_SIGNALS
        # Keyed by physical address, not name -- a name (machineId) may
        # legitimately appear twice (e.g. a machine's run signal in SIGNALS
        # and its service signal in SERVICE_SIGNALS at a different address),
        # and each needs its own independently-tracked state.
        self.state = {(dev, chan, bit): SignalState(name, dev, chan, bit)
                      for name, dev, chan, bit in self.signals}
        self.channels = sorted({(dev, chan) for _, dev, chan, _ in self.signals})
        self.ranges = make_ranges(self.channels)
        self._prev_word = {ch: None for ch in self.channels}

    def poll(self, hl):
        """One polling cycle. Returns a list of Edge for any transitions
        seen this cycle (possibly empty), or None if the read failed."""
        words = {}
        for dev, start, n in self.ranges:
            vals = hl.words(dev, start, n)
            if vals is None:
                self._prev_word = {ch: None for ch in self.channels}
                return None
            for i, v in enumerate(vals):
                words[(dev, start + i)] = v

        t = time.time()
        edges = []
        for name, dev, chan, bit in self.signals:
            w = words[(dev, chan)]
            pw = self._prev_word[(dev, chan)]
            lvl = w >> bit & 1
            st = self.state[(dev, chan, bit)]
            if st.level is None:
                st.level = lvl
            elif pw is not None and (pw >> bit & 1) != lvl:
                rise_event, fall_event = _SERVICE_EVENTS.get((dev, chan, bit), ("rise", "fall"))
                if lvl:  # rising edge
                    st.rise_t = t
                    st.count += 1
                    edges.append(Edge(t, name, rise_event, None, st.count))
                else:    # falling edge
                    wd = t - st.rise_t if st.rise_t else None
                    if wd:
                        st.widths.append(wd)
                    edges.append(Edge(t, name, fall_event, wd, st.count))
                st.level = lvl

        for ch in self.channels:
            self._prev_word[ch] = words[ch]
        return edges

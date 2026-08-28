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
    ("TMC-400", "R",  0,    1),
]


@dataclass
class Edge:
    t: float
    name: str
    event: str              # "rise" | "fall"
    width: float | None     # only set on "fall"
    count: int              # rise count so far, including this edge


@dataclass
class SignalState:
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
        self.signals = signals if signals is not None else SIGNALS
        self.state = {name: SignalState(dev, chan, bit) for name, dev, chan, bit in self.signals}
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
            st = self.state[name]
            if st.level is None:
                st.level = lvl
            elif pw is not None and (pw >> bit & 1) != lvl:
                if lvl:  # rising edge
                    st.rise_t = t
                    st.count += 1
                    edges.append(Edge(t, name, "rise", None, st.count))
                else:    # falling edge
                    wd = t - st.rise_t if st.rise_t else None
                    if wd:
                        st.widths.append(wd)
                    edges.append(Edge(t, name, "fall", wd, st.count))
                st.level = lvl

        for ch in self.channels:
            self._prev_word[ch] = words[ch]
        return edges

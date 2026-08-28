"""Modular pieces behind watch_signals.py.

  hostlink.py  -- transport: talk host-link, know nothing about "signals"
  signals.py   -- domain logic: named bits -> edges, knows nothing about I/O
  display.py   -- presentation: render a SignalWatcher to the terminal
  sinks.py      -- pluggable outputs (CSV today; REST/MQTT/etc. later)

Each layer only depends on the one below it, so a new output (a REST API
push, a database write, ...) is a new function in sinks.py (or a new module
next to it) -- nothing else here needs to change.
"""

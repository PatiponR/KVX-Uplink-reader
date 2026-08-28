"""ANSI terminal live view of a SignalWatcher. Pure presentation -- reads
watcher state and a recent-edges log, writes to stdout, nothing else."""
import sys
import time


def render(watcher, host, port, polls, reconnects, t0, recent):
    up = time.time() - t0
    out = [
        f"\033[H\033[J\033[1m  SIGNAL WATCH\033[0m  {host}:{port}",
        f"  {up:7.1f}s   {polls} polls ({polls/up:.0f}/s)   reconnects {reconnects}",
        "",
    ]
    for name, st in watcher.state.items():
        ws = st.widths
        stat = (f"avg {sum(ws)/len(ws)*1000:6.0f}ms  "
                f"min {min(ws)*1000:5.0f}  max {max(ws)*1000:5.0f}") if ws else ""
        lvl = st.level or 0
        out.append(f"  \033[1m{name:<12}\033[0m {st.label:<9} "
                   f"{'\033[92mON \033[0m' if lvl else 'off'}  "
                   f"count \033[1m{st.count:6d}\033[0m   {stat}")

    out += ["", "  \033[1mrecent edges\033[0m"]
    for e in recent[-12:]:
        out.append(f"   {e.t - t0:8.3f}s  {e.name:<12} {e.event.upper()}"
                   + (f"   width {e.width*1000:.0f} ms" if e.width else ""))
    if not recent:
        out.append("   \033[90m(none yet)\033[0m")
    out.append("\n  \033[90mCtrl-C to stop\033[0m")

    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def summary(watcher, polls, t0):
    up = time.time() - t0
    print(f"\n\n=== {up:.1f}s, {polls} polls ({polls/up:.0f}/s) ===")
    for name, st in watcher.state.items():
        if st.count:
            ws = st.widths
            print(f"  {name} ({st.label}): {st.count} pulses  ({st.count/up*60:.1f}/min)"
                  + (f"  width avg {sum(ws)/len(ws)*1000:.0f}ms "
                     f"min {min(ws)*1000:.0f} max {max(ws)*1000:.0f}" if ws else ""))
        else:
            print(f"  {name} ({st.label}): no pulses seen")

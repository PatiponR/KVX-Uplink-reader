# Run on boot (Raspberry Pi / systemd)

## Fresh Pi? Use the script

`deploy/setup-pi.sh` does everything below plus the network setup, in one
idempotent pass -- safe to re-run, and it verifies itself at the end:

```bash
git clone https://github.com/PatiponR/KVX-Uplink-reader.git
cd KVX-Uplink-reader && ./deploy/setup-pi.sh
```

It prompts for `REST_API_KEY` (hidden) and leaves an existing one alone. The
rest of this file is the manual version, and what to check when something
misbehaves.


One-time setup, on the Pi, after `git clone`ing the repo into `/home/shubu/KVX-Uplink-reader`
(the unit file's `User=`/paths match that; if you clone under a different user
or path, run `whoami` and `pwd` in the repo and edit `watch-signals.service` to match first):

```bash
# 1. REST_BASE_URL / REST_API_KEY / REST_TIMEOUT overrides for plc/config.py,
#    kept outside the repo (this file isn't in git) since REST_API_KEY is a
#    secret. Fill in the real key -- this is the "add it later" step.
sudo tee /etc/watch-signals.env >/dev/null <<'EOF'
REST_BASE_URL=http://localhost:4000
REST_API_KEY=
EOF
sudo chmod 600 /etc/watch-signals.env

# 2. install the unit
sudo cp deploy/watch-signals.service /etc/systemd/system/
sudo systemctl daemon-reload

# 3. start it now, and make it start on every future boot
sudo systemctl enable --now watch-signals
```

Drop `--rest` from `ExecStart` in the unit file first if you don't want the
REST POSTs, just the live signal tracking.

## Checking on it

```bash
sudo systemctl status watch-signals     # running? since when? recent restarts?
journalctl -u watch-signals -f          # live log (Ctrl-C to stop watching, service keeps running)
journalctl -u watch-signals --since "1 hour ago"
```

## The spool (undelivered edges)

With `--rest`, any edge the API doesn't accept is written to a SQLite spool
instead of being dropped, and replayed in order once the API is reachable
again -- an internet outage costs nothing, and neither does a reboot in the
middle of one. The unit's `StateDirectory=` puts it at
`/var/lib/watch-signals/spool.db`, deliberately outside the git checkout so
re-cloning the repo can't throw away undelivered data.

```bash
# how many edges are waiting to go out? (0 = everything delivered)
sudo sqlite3 /var/lib/watch-signals/spool.db 'SELECT COUNT(*) FROM pending;'

# edges the API rejected permanently (a 4xx that isn't 408/429) -- these are
# set aside so one bad record can't block the queue behind it. Should be empty;
# anything here means the body or the machineId isn't what the API expects.
sudo sqlite3 /var/lib/watch-signals/spool.db \
  'SELECT datetime(t,"unixepoch","localtime"), machine_id, event, reason FROM dead;'
```

Each POST carries `occurredAt` -- when the PLC actually fired -- so a replayed
edge reports its real time rather than its delivery time. The API requires UTC
with a literal `Z`; a `+07:00` offset or bare Bangkok local time is rejected
with a 400, so the timestamp is built as UTC explicitly and never from
`datetime.now()`.

That makes the Pi's clock part of the data path, and a Pi has no
battery-backed clock. Confirm NTP is actually syncing:

```bash
timedatectl show -p NTPSynchronized -p TimeUSec
```

`NTPSynchronized=yes` is what you want. If the clock is obviously unset (before
2026) the sink omits `occurredAt` rather than sending a wrong one, and the
server stamps the edge instead -- a degraded but honest result, logged once at
startup. Worth knowing that a power cut while the uplink is also down is the
case where this bites: the Pi boots with no idea what time it is and can't ask
until WiFi returns.

Retries back off from 5s to a maximum of 5 minutes between attempts. Both are
tunable with `REST_RETRY_BASE` / `REST_RETRY_MAX` in `/etc/watch-signals.env`,
along with `REST_SPOOL_PATH` if you'd rather the spool lived elsewhere (a USB
stick, to spare the SD card).

Sanity-check the whole thing by pointing the Pi at a dead endpoint for a
minute -- `REST_BASE_URL=http://192.0.2.1:4000` in the env file, restart, watch
`pending` grow in the journal, then put the real URL back and watch it drain.

## After a `git pull` with new code

```bash
sudo systemctl restart watch-signals
```

## Stopping / disabling

```bash
sudo systemctl stop watch-signals       # stop now
sudo systemctl disable watch-signals    # stop starting on boot
```

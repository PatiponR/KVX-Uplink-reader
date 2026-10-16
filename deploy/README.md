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

## Debugging a machine with no data

`check_signals.py` checks the PLC side against `plc/signals.py`, one layer at
a time: mapping, network, PLC mode, channel reads, bit levels, live edges.
It's read-only and safe to run while the service is running:

```bash
python3 check_signals.py machine B8-45      # start here: ends with a verdict
python3 check_signals.py                    # or pick a mode from the menu
```

If it shows the machine's signal toggling, the PLC side is fine and the
problem is downstream (service, REST, API). `scan` finds inputs that are
changing on a bit `signals.py` doesn't map, which usually means something
was rewired.

To see what actually gets sent to the API, turn on request logging. Add
`REST_LOG_REQUESTS=1` to `/etc/watch-signals.env`, then:

```bash
sudo systemctl restart watch-signals
journalctl -u watch-signals -f | grep TMC-400
```

Each request is one `[http]` line: how many edges, the HTTP status, the API's
response and how long it took. Reading the result:

- **No line for the machine:** its edges never reached the sink. Check its
  pins in `signals.py` and that the service was restarted after editing them.
- **`HTTP 4xx` with a response:** the API rejected the edge, and the response
  says why.

Remove the line (and restart) when you're done, to keep the journal small.

## Batched sending and the spool

With `--rest`, edges are sent in batches to `POST /api/transactions/batch`: one
request carries everything that is waiting (up to `REST_BATCH_MAX`, default 200).
One request per edge capped throughput at ~12 edges/s over the ~80 ms link to
Singapore, so the dashboard fell further behind all day. The contract and the
reasoning are in `PI-SENDER-BATCH-REQUIREMENTS.md`.

Every edge is written to a SQLite spool *before* any network attempt and removed
only after the API answers `201`, so an outage or a power cut loses nothing. It
lives at `/var/lib/watch-signals/spool.db` (the unit's `StateDirectory=`),
outside the git checkout so re-cloning can't throw away undelivered data.

```bash
# how many edges are waiting to go out? (should stay in single digits)
sudo sqlite3 /var/lib/watch-signals/spool.db 'SELECT COUNT(*) FROM pending;'

# edges the API will never accept: listed under "rejected" in a batch reply,
# or a 4xx (not 408/429) on the single-edge route. Set aside so one bad record
# can't block the queue. Should be empty; anything here means the machineId or
# body isn't what the API expects.
sudo sqlite3 /var/lib/watch-signals/spool.db \
  'SELECT datetime(t,"unixepoch","localtime"), machine_id, event, reason FROM dead;'
```

The service also logs `queue length N` once a minute (`journalctl -u watch-signals
| grep "queue length"`).

How each reply is handled:

| Reply | What happens |
|---|---|
| `201` | Batch removed from the spool; anything under `rejected` moves to `dead` |
| `400` | Logged loudly; those edges are sent one at a time to `/api/transactions` |
| `404` on `/batch` | Server predates the feature: one at a time, `/batch` retried every `REST_BATCH_RECHECK` s (300) |
| `401`/`403` | Logged as an ALERT; everything kept and retried -- fix `REST_API_KEY` |
| `413` | Batch halved |
| `429`, `5xx`, timeout, network error | Everything kept; same batch retried with backoff |

Edges are always sent oldest first with one request in flight, because the
server pairs rise/fall by order. Retries back off 1 s, 2 s, 4 s ... up to 30 s
(`REST_RETRY_BASE` / `REST_RETRY_MAX`), and reset after a success. One
keep-alive connection is reused; the request timeout is `REST_TIMEOUT` (10 s).
If the spool exceeds `REST_QUEUE_MAX` (50,000 edges) the oldest are dropped in
chunks and logged. `REST_SPOOL_PATH` moves the spool (e.g. to a USB stick, to
spare the SD card).

Each edge carries `occurredAt` -- when the PLC actually fired -- in UTC with a
literal `Z` and milliseconds (a `+07:00` offset or bare Bangkok time is
rejected). It is stored with the edge and never re-stamped, so a retry or a
replay after a reboot sends the identical value; that is what lets the server
skip edges it already stored (`duplicates` in the reply) instead of
double-counting them.

That makes the Pi's clock part of the data path, and a Pi has no battery-backed
clock. Confirm NTP is actually syncing:

```bash
timedatectl show -p NTPSynchronized -p TimeUSec
```

`NTPSynchronized=yes` is what you want. If the clock is obviously unset (before
2026) `occurredAt` is omitted and the server stamps the edge instead -- logged
once at startup. The batch route wants `occurredAt`, so a batch holding such an
edge may be answered `400`, in which case those edges go one at a time as above.

Sanity-check the whole thing by pointing the Pi at a dead endpoint for a
minute -- `REST_BASE_URL=http://192.0.2.1:4000` in the env file, restart, watch
the queue length grow in the journal, then put the real URL back and watch it
drain within about a minute.

To confirm the lag is flat, run this on the VPS (average should stay under 2 s
in every hour):

```sql
SELECT date_trunc('hour', received_at) AS hr,
       round(avg(extract(epoch FROM received_at - occurred_at))::numeric, 1) AS avg_lag_s,
       count(*) AS edges
FROM transactions WHERE received_at > now() - interval '1 day' GROUP BY 1 ORDER BY 1;
```

Tests (fake API on localhost, no network needed):

```bash
python3 -m unittest discover -s tests -v
```

## After a `git pull` with new code

```bash
sudo systemctl restart watch-signals
```

## Stopping / disabling

```bash
sudo systemctl stop watch-signals       # stop now
sudo systemctl disable watch-signals    # stop starting on boot
```

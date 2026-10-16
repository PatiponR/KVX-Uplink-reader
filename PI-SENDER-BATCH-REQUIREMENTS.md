# Pi sender: switch to batched ingest

**For:** the developer who maintains the Raspberry Pi code that reads RISE/FALL from the PLC
and sends it to the cloud.
**Status:** the backend endpoint is built and is deployed to the VPS before the Pi switches.
The Pi side is what this document asks for.
**Effort:** about half a day for Option A, or 1–2 days for Option B.

---

## 1. Why

The dashboard falls further behind the floor as the day goes on. With 30+ machines it is
minutes behind by the afternoon.

The cause is that the Pi sends **one HTTPS request per edge, waiting for each reply before
sending the next**. Every request pays a full round trip to the server in Singapore, about
80 ms. So the Pi can send at most about **12 edges per second**, however fast the server is.

The floor produces more than that:

| | Edges per second |
|---|---|
| 30 machines × 1 cycle per ~4 s × 2 edges (rise + fall) | **~15/s arrive** |
| One request at a time at ~80 ms each | **~12/s leave** |

The difference piles up in the queue, so the lag only ever grows.

Measured with a simulated 80 ms link and 40 machines:

| Sender | Lag after 10 s | 20 s | 30 s | 40 s |
|---|---|---|---|---|
| One request per edge (today) | 2.6 s | 4.9 s | 9.0 s | 13.2 s, still growing |
| Batched (this spec) | 0.14 s | 0.12 s | 0.11 s | 0.11 s, flat |

The fix is to send **every edge that is waiting in one request**. Throughput then scales with
the load instead of being capped by network latency.

### See it yourself

Every row in the database records both the device time and the server time. Run this on the VPS:

```sql
SELECT date_trunc('hour', received_at) AS hr,
       round(avg(extract(epoch FROM received_at - occurred_at))::numeric, 1) AS avg_lag_s,
       round(max(extract(epoch FROM received_at - occurred_at))::numeric, 1) AS max_lag_s,
       count(*) AS edges
FROM transactions
WHERE received_at > now() - interval '1 day'
GROUP BY 1 ORDER BY 1;
```

Today, `avg_lag_s` climbs hour by hour. After this change it should stay flat and under 2 s.

---

## 2. Choose an approach

### Option A: hand edges to the reference relay (recommended, smallest change)

The repo ships `relay/index.js`: zero dependencies, plain Node 20.6+ (for `--env-file`). It already does the
queueing, batching, retries and crash safety. Run it on the Pi with `RELAY_SOURCE=http`, and
change your PLC code so that it POSTs each edge to `http://127.0.0.1:8787/edge` instead of to
the cloud. Nothing else in your code changes.

```bash
# relay/.env
RELAY_API_URL=https://api.sn-metalpart.com/api/transactions
RELAY_API_KEY=<the Pi's ingest key>
RELAY_SOURCE=http
# optional: RELAY_HTTP_PORT=8787  RELAY_BATCH_MAX=200  RELAY_QUEUE_FILE=/var/lib/sn-relay/queue.jsonl

node --env-file=relay/.env relay/index.js      # run it under systemd, Restart=always
```

Local hand-off contract:

```
POST http://127.0.0.1:8787/edge
Content-Type: application/json

{ "machineId": "TMC-400", "edge": "rise", "occurredAt": "2026-10-16T01:15:00.123Z" }
```

- The relay replies `202 {"queued":1}` once the edge is **on disk**. Wait for it. A failed or
  refused connection means the relay is down: retry.
- You may also send an array of these objects, which are kept in order.
- `400` means the edge was malformed, and the body says why.
- Send `occurredAt` as the time **you read the edge from the PLC**. If you leave it out, the
  relay stamps its own time, which is close enough when it runs on the same Pi.

Under Option A, your code must only:
- POST edges in the order they happened, one hand-off at a time (or as an ordered array);
- retry the hand-off if the relay is unreachable.

### Option B: batch inside your own code

Implement the requirements in §4 against the endpoint in §3. Use `relay/index.js` as the
reference implementation: the `queue` and `sender` sections are the parts to copy.

---

## 3. Endpoint contract

```
POST https://api.sn-metalpart.com/api/transactions/batch
x-api-key: <the Pi's ingest key>
Content-Type: application/json

{ "events": [
  { "machineId": "TMC-400", "edge": "rise", "occurredAt": "2026-10-16T01:15:00.123Z" },
  { "machineId": "TMC-400", "edge": "fall", "occurredAt": "2026-10-16T01:15:04.333Z" },
  { "machineId": "TMC-250", "edge": "rise", "occurredAt": "2026-10-16T01:15:04.910Z" }
] }
```

| Field | Rule |
|---|---|
| `events` | 1–500 items, **oldest first**. They are stored and applied in array order. |
| `machineId` | The machine id as registered in the back-office. |
| `edge` | `rise`, `fall`, `service` or `operate`. |
| `occurredAt` | ISO-8601, **UTC, with milliseconds**, taken when the edge was read from the PLC. Required for resend safety (see below). |

Success response, `201`:

```json
{ "accepted": 2, "duplicates": 0, "rejected": [ { "index": 1, "error": "Unknown or inactive machine: TMC-999" } ] }
```

- `rejected`: events that can never succeed. Log them, **drop them**, and never retry them.
- **Resend-safe:** if a batch is sent again (for example, the reply was lost), events already
  stored are skipped and counted in `duplicates`. They are matched on machine, edge and
  `occurredAt` to the millisecond. This is why `occurredAt` must be the original read time,
  stored with the edge and never re-stamped on retry.

Try it:

```bash
curl -s -X POST https://api.sn-metalpart.com/api/transactions/batch \
  -H "x-api-key: $KEY" -H 'content-type: application/json' \
  -d '{"events":[{"machineId":"TMC-400","edge":"rise","occurredAt":"2026-10-16T01:15:00.123Z"}]}'
```

### What to do on each response

| Response | Meaning | Action |
|---|---|---|
| `201` | Batch stored | Remove those events from the queue. Drop the `rejected` ones with a log line. |
| `400` | The request shape is wrong | **Do not drop the batch.** Log loudly and fall back to the single-edge route `POST /api/transactions` for these events. |
| `401` | Wrong or revoked key | Keep everything and retry with backoff. Alert: someone must fix the key. |
| `404` on `/batch` | Server is older than this feature | Fall back to `POST /api/transactions`, one edge at a time. Re-try `/batch` every few minutes. |
| `413` | Body too large | Send a smaller batch. |
| `429`, `5xx`, timeout, network error | Temporary | Keep everything and retry the same batch with backoff. |

---

## 4. Requirements (Option B)

**R1: Timestamp at the source.** Set `occurredAt` the moment the edge is read from the PLC, in
UTC with milliseconds. Never re-stamp it when sending or retrying.

**R2: Durable before sending.** Write every edge to a local queue on disk before any network
attempt. A power cut must never lose an edge that the PLC reported.

**R3: Append-only queue.** Add each edge by appending one line to the queue file. Do **not**
rewrite the whole file per edge: on an SD card that gets slower as the backlog grows, which is
exactly when the sender must keep up. To track what the server has confirmed, write a small
sidecar file (for example a count of confirmed records), and compact the queue file
occasionally (every ~1000 confirmed edges or every 30 s). `relay/index.js` shows a crash-safe
way to do this, using a generation number in the header.

**R4: Strict order, one request in flight.** Send the oldest events first. Never start a
second request before the first has finished. Never skip ahead past a failed batch. Rise and
fall are paired by order on the server.

**R5: Send everything waiting.** Each request takes everything in the queue, up to 200
(`RELAY_BATCH_MAX`; the server allows 500). Edges that arrive while a request is in flight go
in the next one. Do not wait or poll on a timer to "fill" a batch: send as soon as the
previous request finishes and something is queued.

**R6: Remove only on success.** Remove events from the queue only after a `201`. Every other
outcome keeps them (see the table in §3).

**R7: Retry with backoff.** On any temporary failure, wait 1 s, then 2 s, 4 s and so on, capped
at 30 s, and reset to 1 s after a success. Also retry on a 15 s floor timer, in case a backoff
timer is lost.

**R8: One kept-alive connection.** Reuse a single HTTPS connection with keep-alive. A new TLS
handshake per request roughly doubles its cost. Set a 10 s request timeout.

**R9: Bounded disk.** Cap the queue (default 50,000 edges, many hours of outage). When it is
full, drop the oldest in chunks and log it.

**R10: Correct clock.** Keep the Pi NTP-synced (`timedatectl` should show
`System clock synchronized: yes`). A clock ahead of the server is clamped to server time. A
clock behind it is trusted, and would shift edges into the wrong hour.

**R11: Visibility.** Log the queue length once a minute. In normal running it should stay
close to 0.

---

## 5. Acceptance criteria

1. **Lag is flat.** On a normal production day with all machines connected, the SQL in §1
   shows `avg_lag_s` **below 2 s in every hour**, with no upward trend.
2. **No loss on outage.** Unplug the Pi's network for 10 minutes during production and plug it
   back in. The backlog drains within about 1 minute, and a full day's press count matches the
   PLC's own counter.
3. **No loss or double count on power loss.** Pull the Pi's power while a backlog is draining,
   then let it reboot. Afterwards there are no gaps and no duplicate rows:

   ```sql
   SELECT machine_id, edge, occurred_at, count(*) FROM transactions
   WHERE occurred_at > now() - interval '1 day'
   GROUP BY 1,2,3 HAVING count(*) > 1;      -- expect 0 rows
   ```
4. **Order kept.** For each machine, rows in insert order alternate rise/fall as the PLC
   produced them (apart from `service`/`operate`).
5. **Queue stays small.** The logged queue length stays close to 0 (single digits) during
   normal running.

---

## 6. Rollout and rollback

- Once the backend is deployed, the batch route sits next to the old one, and the old
  `POST /api/transactions` stays unchanged. Check with the curl in §3 (a `404` means it is not
  deployed yet). The reference relay falls back to the old route on a `404` by itself, so even
  the order of the two deploys does not matter.
- Switch over outside production hours if you can, then watch the SQL in §1 for the next shift.
- **Rollback:** point the sender back at `POST /api/transactions` (Option B), or stop the relay
  and point the PLC code back at the cloud URL (Option A). Nothing on the server needs to change.

---

## 7. Reference

- Reference implementation: [`relay/index.js`](../relay/index.js). Its crash-recovery and
  load behaviour have been tested end to end against the real backend.
- Full API reference: [`docs/API.md`](API.md), §12 Ingest.
- Questions about the server side: ask the SN-SCADA backend owner.

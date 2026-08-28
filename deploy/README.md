# Run on boot (Raspberry Pi / systemd)

One-time setup, on the Pi, after `git clone`ing the repo into `/home/shubu/KVX-Uplink-reader`
(the unit file's `User=`/paths match that; if you clone under a different user
or path, run `whoami` and `pwd` in the repo and edit `watch-signals.service` to match first):

```bash
# 1. (optional) REST_BASE_URL / REST_TIMEOUT overrides for plc/config.py,
#    kept outside the repo since it's deployment-specific
sudo tee /etc/watch-signals.env >/dev/null <<'EOF'
REST_BASE_URL=http://localhost:4000
EOF

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

## After a `git pull` with new code

```bash
sudo systemctl restart watch-signals
```

## Stopping / disabling

```bash
sudo systemctl stop watch-signals       # stop now
sudo systemctl disable watch-signals    # stop starting on boot
```

# HeartBeat Logger

A full logging + health service for all the **systemd** services on a Raspberry
Pi. It captures logs, detects crashes, failures, restart loops and OOM kills,
groups repeats into incidents, stores everything in a compact local SQLite
database (SD-card-wear aware), and gives you a CLI to see what's wrong — all over
SSH, no cloud required.

- `hblogd` — the **daemon**: collects logs + health and writes them to the DB.
- `hblog` — the **CLI**: read-only views you run to inspect the data.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Install on the Pi](#install-on-the-pi)
- [Try it without a Pi](#try-it-without-a-pi)
- [Command reference (`hblog`)](#command-reference-hblog)
  - [Global options](#global-options)
  - [`status` — health overview](#status--health-overview)
  - [`crashes` — recent failures](#crashes--recent-failures)
  - [`incidents` — grouped problems](#incidents--grouped-problems)
  - [`logs` — search & follow](#logs--search--follow)
  - [`stats` — error counts](#stats--error-counts)
  - [`prune` / `vacuum` — maintenance](#prune--vacuum--maintenance)
  - [`demo` — synthetic data](#demo--synthetic-data)
- [Daemon reference (`hblogd`)](#daemon-reference-hblogd)
- [Managing the service](#managing-the-service)
- [Configuration](#configuration)
- [Updating](#updating)
- [Common workflows](#common-workflows)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Development](#development)
- [Roadmap](#roadmap)

---

## What it does

- **Follows the journal** for every service in real time, resuming exactly where
  it left off after a restart — no gaps, no duplicates (persisted cursor).
- **Watches unit state** with systemd so a service that dies silently or is
  crash-looping is caught even when it logs nothing. Doubles as a per-service
  **heartbeat**.
- **Classifies problems** — errors, crashes (exit code / signal), OOM kills,
  failed units, restart loops — and **groups repeats into incidents** so a service
  crash-looping 200× is one incident, not 200 rows.
- **Retention built in** — batched writes, age + size-capped pruning, incremental
  vacuum. Kind to SD cards and bounded disk use.
- **Runs itself as a systemd service** with an sd_notify watchdog, so systemd
  restarts the logger if it ever hangs.

## Architecture

```
journald ─┐
          ├─► collectors ─► classify/group ─► SQLite (WAL, batched) ─► hblog.db
unit state┘   (interface)                          │
(systemd)                                     retention/vacuum
                                                   │
                                         hblog CLI (read-only) ◄── you (SSH)
```

The CLI reads the DB **read-only**, so it is always safe to run while the daemon
is writing (SQLite WAL allows concurrent readers).

## Requirements

- Raspberry Pi OS / any systemd Linux, Python ≥ 3.11.
- The journald binding: `sudo apt install python3-systemd` (preferred over pip on
  the Pi — avoids compiling libsystemd).

The core (DB, classifier, CLI) is pure stdlib and runs anywhere, including
Windows — that is how the test suite runs off-device.

---

## Install on the Pi

```sh
# 1. Dependencies + a dedicated unprivileged user
sudo apt install python3-systemd
sudo useradd --system --no-create-home --shell /usr/sbin/nologin hblog

# 2. Code
sudo mkdir -p /opt/heartbeat-logger
sudo cp -r . /opt/heartbeat-logger
cd /opt/heartbeat-logger

# 3. venv that can see the apt-installed systemd binding
python3 -m venv --system-site-packages venv
sudo ./venv/bin/pip install .

# 4. Config
sudo mkdir -p /etc/heartbeat-logger
sudo cp config/config.example.toml /etc/heartbeat-logger/config.toml
sudo nano /etc/heartbeat-logger/config.toml     # tune retention / watched units

# 5. Service
sudo cp systemd/heartbeat-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now heartbeat-logger
systemctl status heartbeat-logger
```

To make `hblog` available on your `PATH` for interactive use:

```sh
sudo ln -s /opt/heartbeat-logger/venv/bin/hblog /usr/local/bin/hblog
hblog status
```

(Without the symlink, run it as `/opt/heartbeat-logger/venv/bin/hblog …`.)

## Try it without a Pi

```sh
pip install -e ".[dev]"
python -m hblog.cli --db demo.db demo      # insert synthetic events
python -m hblog.cli --db demo.db status
python -m hblog.cli --db demo.db incidents
```

---

## Command reference (`hblog`)

General shape:

```
hblog [--db PATH | --config PATH] <command> [options]
```

### Global options

| Option | Description |
| --- | --- |
| `--db PATH` | Path to `hblog.db`. Overrides whatever the config says. |
| `--config PATH` | Path to a `config.toml` (used to locate the DB and thresholds). |
| `--version` | Print the version and exit. |
| `-h`, `--help` | Help. Works on the top level **and** every subcommand (`hblog logs -h`). |

**How the DB is located** (first match wins):
1. `--db` on the command line.
2. `--config` on the command line → its `db_path`.
3. `$HBLOG_CONFIG` if set → its `db_path`.
4. `/etc/heartbeat-logger/config.toml` if it exists → its `db_path`.
5. Built-in default: `/var/lib/heartbeat-logger/hblog.db`.

So on the Pi with the standard install you just run `hblog status`. Off the Pi (or
against a copied DB) pass `--db /path/to/hblog.db`.

**`--since` format** (used by `crashes`, `logs`, `stats`): an integer + a unit —
`s` seconds, `m` minutes, `h` hours, `d` days. Examples: `30m`, `24h`, `7d`.

---

### `status` — health overview

At-a-glance health of every watched service.

```sh
hblog status
```

```
    UNIT            STATE   HEARTBEAT  RESTARTS  LAST CRASH  ERR/24h
--  --------------  ------  ---------  --------  ----------  -------
X   myapp.service   failed  2m ago     7         1m ago      12
!   nginx.service   active  4s ago     0         -           3
OK  worker.service  active  4s ago     0         -           0
```

Columns:
- **health marker** — `OK` healthy · `!` errors in the last 24 h but running ·
  `X` failed now, or crashed within the last hour.
- **STATE** — systemd `ActiveState` (`active` / `failed` / …). `-` if the unit
  monitor hasn't recorded it yet.
- **HEARTBEAT** — age of the last time the service was seen active.
- **RESTARTS** — systemd restart count for the unit.
- **LAST CRASH** — age of the most recent crash/OOM/failed event.
- **ERR/24h** — number of error-or-worse log lines in the last 24 hours.

### `crashes` — recent failures

Recent crashes, unit failures and OOM kills (newest first).

```sh
hblog crashes                     # last 100 crash-type events
hblog crashes --since 24h         # only the last 24 hours
hblog crashes --unit myapp.service
hblog crashes --since 7d --limit 500
```

| Option | Default | Description |
| --- | --- | --- |
| `--unit NAME` | all | Restrict to one service. |
| `--since AGE` | all time | Only events newer than e.g. `24h`. |
| `--limit N` | `100` | Max rows. |

Output columns: **TIME · UNIT · KIND** (`crash`/`oom`/`failed`) **· DETAIL**
(`exit N` or `signal N`) **· MESSAGE**.

### `incidents` — grouped problems

Deduplicated problems. Every repeat of the same problem on the same unit is folded
into one incident with a `COUNT`, so a crash loop is a single line.

```sh
hblog incidents                   # everything, newest activity first
hblog incidents --open            # only still-open (unresolved) incidents
hblog incidents --unit myapp.service
hblog incidents --open --limit 20
```

| Option | Default | Description |
| --- | --- | --- |
| `--unit NAME` | all | Restrict to one service. |
| `--open` | off | Only incidents that are still open. |
| `--limit N` | `100` | Max rows. |

Columns: **STATUS** (`open`/`resolved`) **· UNIT · KIND · COUNT · FIRST SEEN ·
LAST · SIGNATURE**. Incidents **auto-resolve** when the unit monitor sees the
service healthy again — you don't close them by hand.

### `logs` — search & follow

Search stored log lines, or follow them live like `tail -f`.

```sh
hblog logs                                   # recent lines
hblog logs --unit nginx.service              # one service
hblog logs --since 2h --priority warning     # warnings+errors in last 2h
hblog logs --grep "connection timeout"       # substring search
hblog logs --unit myapp.service -f           # follow (Ctrl-C to stop)
hblog logs --priority err --grep timeout --limit 500
```

| Option | Default | Description |
| --- | --- | --- |
| `--unit NAME` | all | Restrict to one service. |
| `--since AGE` | all time | Only lines newer than e.g. `2h`. |
| `--priority P` | all | Max severity to show: name (`emerg` `alert` `crit` `err` `warning` `notice` `info` `debug`) or number `0`–`7`. `err` shows err **and** worse. |
| `--grep TEXT` | none | Substring match on the message (case-insensitive for ASCII, per SQLite `LIKE`). |
| `--limit N` | `200` | Max rows (ignored in follow mode). |
| `-f`, `--follow` | off | Stream new matching lines until Ctrl-C. |

Each line prints as `TIME  severity  unit [kind]: message`.

### `stats` — error counts

Error counts per service over a window — find the noisiest services fast.

```sh
hblog stats                       # last 24h (default)
hblog stats --since 7d
```

| Option | Default | Description |
| --- | --- | --- |
| `--since AGE` | `24h` | Window to count over. |

### `prune` / `vacuum` — maintenance

These normally run automatically inside the daemon; the CLI lets you force them.

```sh
hblog prune       # apply retention now: drop old events (age) + enforce size cap
hblog vacuum      # full VACUUM to shrink the file on disk
```

`prune` prints what it removed, e.g. `Pruned: events_pruned_age=1240,
events_pruned_size=0, incidents_pruned=3`. **These commands write to the DB** — run
them as a user that can write `hblog.db` (i.e. the `hblog` user or root on the Pi).

### `demo` — synthetic data

Populate a database with a realistic synthetic stream (errors, a crash loop, an
OOM, a segfault) so you can try the CLI without a Pi. **Writes to the DB.**

```sh
hblog --db demo.db demo
hblog --db demo.db status
```

---

## Daemon reference (`hblogd`)

You normally run `hblogd` via systemd, not by hand. Directly:

```
hblogd [--config PATH] [--db PATH] [--log-level LEVEL]
```

| Option | Default | Description |
| --- | --- | --- |
| `--config PATH` | auto | Config file. Falls back to `$HBLOG_CONFIG`, then `/etc/heartbeat-logger/config.toml`, then built-in defaults. |
| `--db PATH` | from config | Override the DB path. |
| `--log-level LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |

Foreground run for debugging (writes to a scratch DB, verbose):

```sh
/opt/heartbeat-logger/venv/bin/hblogd \
  --config /etc/heartbeat-logger/config.toml --log-level DEBUG
```

The daemon degrades gracefully: if the journald binding is missing it keeps
running with just the unit monitor, and vice-versa.

---

## Managing the service

Standard `systemctl` / `journalctl`:

```sh
sudo systemctl status heartbeat-logger        # is it running / healthy?
sudo systemctl start heartbeat-logger
sudo systemctl stop heartbeat-logger
sudo systemctl restart heartbeat-logger       # e.g. after a config change
sudo systemctl enable heartbeat-logger        # start on boot
sudo systemctl disable heartbeat-logger        # don't start on boot

# The logger's OWN logs (the daemon itself), via systemd:
journalctl -u heartbeat-logger -f             # follow
journalctl -u heartbeat-logger --since today
```

> The logger records *other* services into `hblog.db` (view with `hblog logs`).
> The logger's own diagnostics go to the journal (view with `journalctl -u
> heartbeat-logger`). Two different places on purpose.

---

## Configuration

Config lives at `/etc/heartbeat-logger/config.toml`. Every key is optional and
falls back to the default. See
[`config/config.example.toml`](config/config.example.toml) for the annotated
template. Key settings:

| Key | Default | What it controls |
| --- | --- | --- |
| `db_path` | `/var/lib/heartbeat-logger/hblog.db` | Where the SQLite DB lives. |
| `watch_units` | `[]` (all) | Units to watch; `[]` = every service. Entries may end with `*` (prefix match), e.g. `"myapp*"`. |
| `exclude_units` | a few `systemd-*` | Always excluded, even when `watch_units` is empty. |
| `poll_interval_sec` | `15` | How often the unit-state monitor polls. |
| `batch_size` / `flush_interval_sec` | `200` / `5` | Write batching (fewer SD-card flushes). |
| `error_priority` | `3` (err) | Log at this syslog priority or worse counts as an error. Set `4` to also capture warnings. |
| `restart_loop_threshold` / `restart_loop_window_sec` | `5` / `120` | N restarts within the window ⇒ a `restart_loop` incident. |
| `retention_days` | `14` | Raw events pruned after this. |
| `incident_retention_days` | `90` | Incidents kept longer than raw events. |
| `max_db_mb` | `256` | Hard size cap; oldest events pruned first to stay under it. |
| `maintenance_interval_sec` | `3600` | How often the daemon prunes/vacuums. |
| `watchdog` | `true` | Answer the systemd `WatchdogSec` ping. |

**Apply a config change:**

```sh
sudo nano /etc/heartbeat-logger/config.toml
sudo systemctl restart heartbeat-logger
```

The watchdog timeout itself is set in the unit file (`WatchdogSec=60` in
`systemd/heartbeat-logger.service`); change it there and
`sudo systemctl daemon-reload && sudo systemctl restart heartbeat-logger`.

---

## Updating

### Update the software to a new version

```sh
cd /opt/heartbeat-logger
sudo cp -r /path/to/new/code/* .        # or: sudo git pull, if it's a checkout
sudo ./venv/bin/pip install --upgrade .
# if the unit file changed:
sudo cp systemd/heartbeat-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart heartbeat-logger
systemctl status heartbeat-logger
```

Your `hblog.db` and `config.toml` are untouched by an upgrade.

### Update retention / disk usage

Edit `retention_days` and/or `max_db_mb` in the config, restart the service, then
optionally reclaim space immediately:

```sh
sudo systemctl restart heartbeat-logger
sudo -u hblog /opt/heartbeat-logger/venv/bin/hblog prune
sudo -u hblog /opt/heartbeat-logger/venv/bin/hblog vacuum
```

### Update which services are watched

Edit `watch_units` / `exclude_units` in the config and restart. New units are
picked up automatically on the next poll — no restart needed for services that
match an existing rule.

---

## Common workflows

**"Is anything broken right now?"**
```sh
hblog status
hblog incidents --open
```

**"What happened to `myapp` overnight?"**
```sh
hblog crashes --unit myapp.service --since 12h
hblog logs   --unit myapp.service --since 12h --priority err
```

**"Watch a flaky service live while I poke it."**
```sh
hblog logs --unit myapp.service -f
```

**"Which service is the noisiest this week?"**
```sh
hblog stats --since 7d
```

**"The SD card is filling up."**
```sh
hblog prune && hblog vacuum
# then lower retention_days / max_db_mb in the config and restart
```

---

## Troubleshooting

**`hblog: command not found`** — you didn't symlink it; run
`/opt/heartbeat-logger/venv/bin/hblog …` or create the symlink shown in
[Install](#install-on-the-pi).

**`error: database not found: …`** — the daemon hasn't created the DB yet, or
you're pointing at the wrong path. Check the service is running
(`systemctl status heartbeat-logger`) and that `--db` / config `db_path` match.

**`status` shows `STATE = -` for everything** — the unit monitor hasn't completed
its first poll yet (wait `poll_interval_sec`), or the daemon can't run
`systemctl`. Check `journalctl -u heartbeat-logger`.

**No logs are being captured** — confirm `python3-systemd` is installed and the
service user is in the `systemd-journal` group (the unit file adds it via
`SupplementaryGroups=systemd-journal`). `journalctl -u heartbeat-logger` will show
`journal source unavailable …` if the binding is missing.

**The daemon keeps getting killed/restarted** — check
`journalctl -u heartbeat-logger` for the reason; if it's `MemoryMax`, raise it in
the unit file. If it's the watchdog, raise `WatchdogSec`.

**Inspect the raw DB directly** (read-only, safe):
```sh
sqlite3 -readonly /var/lib/heartbeat-logger/hblog.db \
  "SELECT kind, count(*) FROM events GROUP BY kind;"
```

---

## Uninstall

```sh
sudo systemctl disable --now heartbeat-logger
sudo rm /etc/systemd/system/heartbeat-logger.service
sudo systemctl daemon-reload
sudo rm -rf /opt/heartbeat-logger /etc/heartbeat-logger
sudo rm -rf /var/lib/heartbeat-logger      # deletes the collected history
sudo userdel hblog
sudo rm -f /usr/local/bin/hblog
```

---

## Development

```sh
pip install -e ".[dev]"
pytest            # 40 tests, all run off-Pi via the mock source
```

Collectors sit behind a small `Source` interface (`hblog/sources/`). The journald
and systemd collectors import their platform bindings lazily, so the package
imports and the full pipeline/DB/CLI test on any OS by swapping in `MockSource`.
Layout:

```
hblog/
  config.py      models.py      db.py          classify.py
  pipeline.py    daemon.py      cli.py
  sources/       base.py journal.py units.py mock.py
systemd/heartbeat-logger.service
config/config.example.toml
tests/
```

## Roadmap

Not in this version (the design leaves clean seams for each):
- Web dashboard and push/email/Telegram alerts (pluggable sink after classify).
- D-Bus signal subscription to replace unit-state polling for lower latency.
- Optional off-Pi log forwarding (Loki/syslog) for durability.

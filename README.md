# HeartBeat Logger

A full logging and health-monitoring service for every **systemd** service on a
Raspberry Pi. It captures logs, detects crashes, failures, restart loops and
out-of-memory kills, groups repeated problems into incidents, and stores
everything in a compact, crash-safe local SQLite database — all inspectable from a
single command-line tool over SSH, with no cloud and no external dependencies.

Copyright (c) 2026 Angad Singh Bains. All rights reserved.

**Repository:** https://github.com/Angad7600123/HeartBeat-Logger-Service

> **License:** Source Available for personal and non-commercial use. See
> [License summary](#license-summary) and [LICENSE](LICENSE). This is **not** an
> OSI open-source license; commercial use requires a separate written license.

---

## Table of contents

- [Introduction](#introduction)
- [Why this project exists](#why-this-project-exists)
- [The problem it solves](#the-problem-it-solves)
- [Features](#features)
- [Architecture](#architecture)
- [Workflow](#workflow)
- [Durability guarantees](#durability-guarantees)
- [Installation](#installation)
- [Building from source](#building-from-source)
- [Running](#running)
- [Configuration](#configuration)
- [Administration](#administration)
- [Updating](#updating)
- [Security model](#security-model)
- [Performance](#performance)
- [Design philosophy](#design-philosophy)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Documentation index](#documentation-index)
- [License summary](#license-summary)
- [Disclaimer](#disclaimer)

---

## Introduction

HeartBeat Logger is composed of two programs that share one local database:

- **`hblogd`** — a supervised daemon that collects logs and health signals from
  the services on the machine and writes them to a local SQLite database.
- **`hblog`** — a read-only command-line interface that queries that database to
  show service health, crashes, incidents and logs.

It is built for small, always-on Linux devices — the Raspberry Pi in particular —
where there is no room for a heavyweight observability stack, where the storage
medium is a wear-sensitive SD card, and where the only interface is usually an SSH
session.

## Why this project exists

On a single-board computer running a handful of services, problems tend to stay
invisible until something breaks in a way you happen to notice. The journal
contains the evidence, but it is transient, unstructured, and awkward to query
after the fact. A service can crash and be restarted dozens of times an hour, or
die quietly and never come back, without anything drawing your attention to it.

The existing answers are either too small or too large. Plain `journalctl` shows
you raw lines but does not track *state* — it will not tell you that a unit has
been crash-looping since Tuesday. Full monitoring stacks (a metrics database, a
log aggregator, a dashboard, an alert manager) answer these questions well but are
disproportionately heavy for a Pi and usually assume a network destination and a
server to run it on.

HeartBeat Logger exists to fill that gap: a purpose-built, self-contained health
recorder that is light enough to run permanently on the device it is watching, and
structured enough to answer "what is wrong, and since when?" directly.

## The problem it solves

HeartBeat Logger turns a stream of raw journal lines and systemd state into
durable, queryable answers to the questions that actually matter on a device:

- **Which services are unhealthy right now, and for how long?**
- **What crashed, when, and how** — with the exit code or signal, not just a
  message?
- **Is anything stuck in a restart loop?**
- **Did a service die silently** and simply stop, without logging anything?
- **Was a process killed by the kernel for running out of memory?**
- **What were the errors leading up to a failure**, grouped so a burst of the same
  error is one line and not a thousand?

It answers all of this locally, keeps the answers across reboots and power loss,
and bounds its own disk footprint so it never fills the card it lives on.

## Features

- **Real-time journal capture** that resumes exactly where it left off after a
  restart, using a persisted cursor — no gaps and no duplicates.
- **Authoritative unit-state monitoring** via systemd, so a silently dead or
  crash-looping service is detected even when it logs nothing. Each poll also
  records a per-service **heartbeat**.
- **Problem classification** into errors, crashes (by exit code or signal),
  out-of-memory kills, failed units, and restart loops.
- **Incident grouping**: repeated occurrences of the same problem collapse into a
  single incident with a count and a time span, and **auto-resolve** when the
  service becomes healthy again.
- **Crash-safe storage** in SQLite with write-ahead logging and single-transaction
  batches.
- **Built-in retention** — age-based pruning, a hard size cap, and incremental
  vacuuming — tuned to minimize SD-card wear.
- **A focused CLI** designed for SSH: service health, crashes, incidents, log
  search and follow, error statistics, and maintenance.
- **Runs as a hardened systemd service** under an unprivileged account, with an
  `sd_notify` watchdog so systemd restarts the logger itself if it ever hangs.
- **Lightweight and dependency-free at the core** — the collector, classifier,
  storage and CLI use only the Python standard library.

## Architecture

```
                         ┌──────────────────────────────────────────┐
   systemd journal ──────►  journald reader   (hblog/sources/journal)│
   (all services)         │        │                                 │
                          │        ▼                                  │
   systemd unit state ────►  unit monitor      (hblog/sources/units) │
   (systemctl show)       │        │                                 │
                          │        ▼                                  │
                          │   classify + group (hblog/classify)      │
                          │        │                                 │
                          │        ▼                                  │
                          │   batched writer   (hblog/pipeline,db)   │
                          └────────┼─────────────────────────────────┘
                                   ▼
                          ┌──────────────────┐        ┌──────────────────┐
                          │   hblog.db        │◄───────│  hblog  (CLI)     │
                          │   SQLite (WAL)    │  read- │  status/crashes/  │
                          │   events          │  only  │  incidents/logs/  │
                          │   incidents       │        │  stats            │
                          │   service_status  │        └──────────────────┘
                          │   meta (cursor)   │
                          └──────────────────┘
                                   ▲
                          retention / vacuum (scheduled in hblogd)
```

Collectors implement a small `Source` interface (`hblog/sources/base.py`), which
is what allows the entire pipeline, storage and CLI to be developed and tested on
any operating system: on a non-Linux host the journald and systemd collectors are
simply replaced by a mock source. The daemon (`hblog/daemon.py`) orchestrates the
collectors, the write pipeline, the unit-monitor poll loop, scheduled
maintenance, and the systemd watchdog.

## Workflow

1. **Collect.** `hblogd` follows the journal in a background thread and, on an
   interval, polls systemd for the state of every watched unit.
2. **Classify.** Each event is examined for problem signatures — error severity,
   crash messages, OOM kills, failed states — and tagged accordingly. A stable
   *signature* is computed so that repeats can be grouped.
3. **Group.** Problems are folded into incidents keyed by unit, kind and
   signature. A crash loop becomes one incident with a rising count rather than
   thousands of rows.
4. **Store.** Events and incidents are written to SQLite in batches, inside single
   transactions, and the journal cursor is advanced so restarts resume cleanly.
5. **Maintain.** On a schedule, old events are pruned by age, a hard size cap is
   enforced by removing the oldest events first, and space is reclaimed by
   incremental vacuum.
6. **Inspect.** You run `hblog` over SSH to see current health, recent crashes,
   open incidents, and searchable logs. Incidents resolve themselves when the
   service recovers.

## Durability guarantees

HeartBeat Logger is designed to behave predictably across the ungraceful
shutdowns that single-board computers routinely experience (a pulled power lead is
the normal case, not the exception). The following guarantees are honest about
what is and is not protected:

- **Consistency across power loss.** The database uses SQLite in write-ahead
  logging (WAL) mode, and every batch of events is written inside a single
  transaction. An interruption at any point leaves the database *consistent and
  uncorrupted*: you either have a complete batch or you do not.
- **Bounded loss window.** Incoming events are buffered and flushed on a batch-size
  or time-interval boundary (both configurable). The most that an abrupt power
  loss can cost you is the events buffered since the last flush, plus — under the
  `synchronous=NORMAL` setting chosen to protect the SD card — potentially the
  most recent committed transaction that had not yet been checkpointed. The
  database itself remains valid in all cases.
- **Gap-free resume.** The journal read position is persisted as a cursor in the
  database. After a restart — expected or not — collection resumes from that
  cursor, so logs are neither lost nor duplicated across the boundary.
- **Self-healing supervision.** The daemon runs under systemd with
  `Restart=always` and an `sd_notify` watchdog; if the process exits or stops
  responding, systemd restarts it, and it picks up from the persisted cursor.
- **Bounded footprint.** Retention by age and a hard size cap ensure the database
  cannot grow without limit and exhaust the device's storage.

You can tighten the loss window at the cost of more frequent writes (more SD-card
wear) by lowering `batch_size` and `flush_interval_sec`; see
[Configuration](#configuration).

## Installation

**Requirements:** Raspberry Pi OS or any systemd-based Linux, and Python 3.11 or
newer. The journald reader needs the system `python3-systemd` binding, which is
best installed from the distribution rather than compiled from source.

```sh
# 1. Dependencies and a dedicated unprivileged service account
sudo apt install python3-systemd
sudo useradd --system --no-create-home --shell /usr/sbin/nologin hblog

# 2. Install the code
sudo mkdir -p /opt/heartbeat-logger
sudo cp -r . /opt/heartbeat-logger
cd /opt/heartbeat-logger

# 3. A virtual environment that can see the apt-installed systemd binding
python3 -m venv --system-site-packages venv
sudo ./venv/bin/pip install .

# 4. Configuration
sudo mkdir -p /etc/heartbeat-logger
sudo cp config/config.example.toml /etc/heartbeat-logger/config.toml
sudo nano /etc/heartbeat-logger/config.toml     # tune retention / watched units

# 5. Install and enable the service
sudo cp systemd/heartbeat-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now heartbeat-logger
systemctl status heartbeat-logger
```

Make the CLI convenient to run:

```sh
sudo ln -s /opt/heartbeat-logger/venv/bin/hblog /usr/local/bin/hblog
hblog status
```

Without the symlink, invoke it as `/opt/heartbeat-logger/venv/bin/hblog …`.

## Building from source

The core is pure standard-library Python and builds and tests on any platform,
including Windows and macOS; only the live journald and systemd collectors require
Linux. This is what makes the project testable off-device.

```sh
git clone https://github.com/Angad7600123/HeartBeat-Logger-Service.git
cd HeartBeat-Logger-Service

python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows:     .venv\Scripts\activate

pip install -e ".[dev]"     # editable install with test dependencies
pytest                      # run the full test suite (no Pi required)
```

To build a distributable package:

```sh
python -m pip install build
python -m build             # produces wheel + sdist in dist/
```

## Running

### As a service (recommended)

On the Pi, `hblogd` runs under systemd from the unit installed above. See
[Administration](#administration) for the day-to-day commands.

### In the foreground (debugging)

```sh
/opt/heartbeat-logger/venv/bin/hblogd \
  --config /etc/heartbeat-logger/config.toml --log-level DEBUG
```

`hblogd` accepts:

| Option | Default | Description |
| --- | --- | --- |
| `--config PATH` | auto | Config file. Falls back to `$HBLOG_CONFIG`, then `/etc/heartbeat-logger/config.toml`, then built-in defaults. |
| `--db PATH` | from config | Override the database path. |
| `--log-level LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |

The daemon degrades gracefully: if the journald binding is unavailable it runs
with the unit monitor only, and vice versa.

### Trying it without a Pi

```sh
python -m hblog.cli --db demo.db demo      # insert a synthetic event stream
python -m hblog.cli --db demo.db status
python -m hblog.cli --db demo.db incidents
```

### The `hblog` command line

General shape:

```
hblog [--db PATH | --config PATH] <command> [options]
```

**Global options**

| Option | Description |
| --- | --- |
| `--db PATH` | Path to `hblog.db`; overrides the config. |
| `--config PATH` | Path to a `config.toml` (used to locate the database and thresholds). |
| `--version` | Print the version and exit. |
| `-h`, `--help` | Help; available on the top level and on every subcommand. |

The database is located by the first of these that applies: `--db`, then
`--config`'s `db_path`, then `$HBLOG_CONFIG`, then
`/etc/heartbeat-logger/config.toml`, then the built-in default
`/var/lib/heartbeat-logger/hblog.db`. Time windows (`--since`) are written as an
integer plus a unit: `s`, `m`, `h`, or `d` (for example `30m`, `24h`, `7d`).

**Commands**

| Command | Purpose | Key options |
| --- | --- | --- |
| `status` | Health overview of every watched service. | — |
| `crashes` | Recent crashes, failures and OOM kills. | `--unit`, `--since`, `--limit` |
| `incidents` | Grouped, deduplicated problems. | `--unit`, `--open`, `--limit` |
| `logs` | Search or follow stored log lines. | `--unit`, `--since`, `--priority`, `--grep`, `--limit`, `-f/--follow` |
| `stats` | Error counts per service over a window. | `--since` |
| `prune` | Apply retention now (age + size cap). | — |
| `vacuum` | Reclaim space with a full vacuum. | — |
| `demo` | Insert a synthetic event stream (no Pi needed). | — |

Notes:

- `--priority` accepts a severity name (`emerg`, `alert`, `crit`, `err`,
  `warning`, `notice`, `info`, `debug`) or a number `0`–`7`, and matches that
  severity **and worse**.
- `--grep` is a substring match on the message (case-insensitive for ASCII, per
  SQLite `LIKE`).
- `prune`, `vacuum` and `demo` write to the database and must run as a user that
  can write it (the `hblog` user or root on the Pi). All other commands are
  read-only and safe to run while the daemon is writing.

Examples:

```sh
hblog status
hblog crashes --unit myapp.service --since 24h
hblog incidents --open
hblog logs --unit myapp.service -f
hblog logs --priority err --grep timeout --since 2h
hblog stats --since 7d
```

## Configuration

Configuration lives at `/etc/heartbeat-logger/config.toml`. Every key is optional
and falls back to the default shown below. The annotated template is
[`config/config.example.toml`](config/config.example.toml).

| Key | Default | Controls |
| --- | --- | --- |
| `db_path` | `/var/lib/heartbeat-logger/hblog.db` | Location of the SQLite database. |
| `watch_units` | `[]` (all) | Units to watch; empty means every service. Entries may end with `*` for a prefix match. |
| `exclude_units` | a few `systemd-*` | Always excluded, even when `watch_units` is empty. |
| `poll_interval_sec` | `15` | How often the unit-state monitor polls. |
| `batch_size` | `200` | Flush after this many buffered events. |
| `flush_interval_sec` | `5` | Flush at least this often when idle. |
| `error_priority` | `3` (err) | Log at this syslog priority or worse counts as an error. Set `4` to also capture warnings. |
| `restart_loop_threshold` | `5` | Restarts within the window that constitute a loop. |
| `restart_loop_window_sec` | `120` | Window for restart-loop detection. |
| `retention_days` | `14` | Age after which raw events are pruned. |
| `incident_retention_days` | `90` | Incidents are kept longer than raw events. |
| `max_db_mb` | `256` | Hard size cap; oldest events pruned first to stay under it. |
| `maintenance_interval_sec` | `3600` | How often the daemon prunes and vacuums. |
| `watchdog` | `true` | Answer the systemd `WatchdogSec` ping. |

Apply a change:

```sh
sudo nano /etc/heartbeat-logger/config.toml
sudo systemctl restart heartbeat-logger
```

The watchdog *timeout* is set in the unit file (`WatchdogSec=60`); change it there
and run `sudo systemctl daemon-reload && sudo systemctl restart heartbeat-logger`.

## Administration

Manage the daemon with standard systemd tooling:

```sh
sudo systemctl status heartbeat-logger      # running / healthy?
sudo systemctl restart heartbeat-logger     # after a config change
sudo systemctl stop heartbeat-logger
sudo systemctl enable heartbeat-logger      # start on boot
sudo systemctl disable heartbeat-logger

# The logger's OWN diagnostics (not the data it collects):
journalctl -u heartbeat-logger -f
```

There are two distinct log locations by design: the services HeartBeat Logger
*watches* are recorded into `hblog.db` and viewed with `hblog logs`, while the
logger's own diagnostics go to the journal and are viewed with
`journalctl -u heartbeat-logger`.

Force maintenance manually when needed:

```sh
sudo -u hblog /opt/heartbeat-logger/venv/bin/hblog prune
sudo -u hblog /opt/heartbeat-logger/venv/bin/hblog vacuum
```

## Updating

**Upgrade the software:**

```sh
cd /opt/heartbeat-logger
sudo cp -r /path/to/new/code/* .          # or: sudo git pull, for a checkout
sudo ./venv/bin/pip install --upgrade .
# If the unit file changed:
sudo cp systemd/heartbeat-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart heartbeat-logger
systemctl status heartbeat-logger
```

Your database and configuration are untouched by an upgrade. Review
[CHANGELOG.md](CHANGELOG.md) before upgrading across versions.

**Change retention or disk usage:** edit `retention_days` and/or `max_db_mb`,
restart the service, and optionally reclaim space immediately with
`hblog prune` followed by `hblog vacuum`.

**Change which services are watched:** edit `watch_units` / `exclude_units` and
restart. Newly started services that match an existing rule are picked up
automatically on the next poll.

## Security model

HeartBeat Logger is an observer and is deployed with least privilege:

- **Unprivileged account.** The daemon runs as the dedicated `hblog` user, not
  root. It reads the journal through membership of the `systemd-journal` group,
  granted via `SupplementaryGroups=systemd-journal` in the unit file.
- **Filesystem confinement.** The provided unit applies systemd hardening —
  `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
  `NoNewPrivileges=yes`, `ProtectKernelTunables=yes`, `ProtectControlGroups=yes` —
  and grants write access only to its state directory via `ReadWritePaths`.
- **No network exposure.** The service does not open network sockets;
  `RestrictAddressFamilies=AF_UNIX` limits it to local communication (the systemd
  notification socket). Collected data never leaves the device.
- **Bounded resources.** `MemoryMax=128M` caps the daemon's memory.
- **Read-only inspection.** The `hblog` CLI opens the database read-only for all
  query commands, so inspecting data cannot corrupt or alter it, and is safe
  alongside the writing daemon.
- **Local-only, no telemetry.** All data is stored locally in SQLite. Nothing is
  transmitted anywhere.

To report a vulnerability, follow the process in [SECURITY.md](SECURITY.md). Do
not open a public issue for security reports.

## Performance

HeartBeat Logger is engineered to be inconspicuous on a small device rather than
to maximize throughput:

- **Write batching** groups events into single transactions, so steady logging
  produces few, coalesced disk writes instead of one per line — the main lever for
  reducing SD-card wear.
- **SQLite tuning** — WAL journaling, `synchronous=NORMAL`, and incremental
  auto-vacuum — favors low write amplification and predictable latency over
  maximum durability of the very last transaction.
- **Targeted indexes** on timestamp, unit, priority and kind keep the CLI's
  queries responsive as the database grows.
- **Polling, not busy-waiting.** The unit monitor runs on an interval
  (`poll_interval_sec`), and the journal reader blocks until new entries arrive, so
  idle CPU use is negligible.
- **Bounded memory.** A fixed-size internal queue and the `MemoryMax` cap keep the
  footprint stable even under a burst of log activity.

The batching, polling and retention parameters are all configurable, letting you
trade write frequency, detection latency and history depth to suit your hardware.
No specific throughput figures are quoted here because the meaningful numbers
depend heavily on your device, storage medium, and service mix; measure on your
own hardware if throughput is a concern.

## Design philosophy

- **Self-contained.** Observability should not require a second machine. The whole
  system runs on the device it watches and needs no server, network destination,
  or cloud account.
- **Light by default.** The core depends only on the Python standard library, so
  it installs and runs cleanly on a constrained device.
- **Kind to the hardware.** Every storage decision is made with SD-card longevity
  and bounded disk use in mind.
- **State, not just lines.** The value is in tracking health over time — what is
  failing and since when — not merely echoing log messages.
- **Testable everywhere.** Collection sits behind a small interface so the storage,
  classification and interface layers can be exercised on any OS without a Pi,
  which keeps the project reliable and easy to contribute to.
- **Honest guarantees.** The system states plainly what it protects and what it
  does not, rather than promising durability it cannot deliver.

## Troubleshooting

| Symptom | Cause and resolution |
| --- | --- |
| `hblog: command not found` | The CLI is not on your `PATH`. Run it via `/opt/heartbeat-logger/venv/bin/hblog …` or create the symlink from [Installation](#installation). |
| `error: database not found: …` | The daemon has not created the database yet, or you pointed at the wrong path. Confirm the service is running and that `--db`/`db_path` match. |
| `status` shows `STATE = -` for everything | The unit monitor has not completed its first poll (wait `poll_interval_sec`), or it cannot run `systemctl`. Check `journalctl -u heartbeat-logger`. |
| No logs are captured | Confirm `python3-systemd` is installed and the service account is in the `systemd-journal` group. `journalctl -u heartbeat-logger` reports `journal source unavailable …` when the binding is missing. |
| The daemon is repeatedly restarted | Inspect `journalctl -u heartbeat-logger`. If it is `MemoryMax`, raise it in the unit file; if it is the watchdog, raise `WatchdogSec`. |

Inspect the raw database directly (read-only and safe):

```sh
sqlite3 -readonly /var/lib/heartbeat-logger/hblog.db \
  "SELECT kind, count(*) FROM events GROUP BY kind;"
```

## FAQ

**Is this open source?**
No. HeartBeat Logger is *source available*. You may read, learn from, and use it
for personal and non-commercial purposes under the [LICENSE](LICENSE), but it is
not licensed under an OSI-approved open-source license, and commercial use
requires separate written permission. See [License summary](#license-summary).

**Does it work with Docker containers or non-systemd services?**
Version 1.0 targets systemd services via journald and `systemctl`. The collector
layer is deliberately pluggable, so additional sources can be added; see the
[Roadmap](#roadmap).

**Where is my data sent?**
Nowhere. All data is stored locally in SQLite. There is no telemetry and no
network destination.

**Will it wear out my SD card?**
It is designed specifically to minimize that risk through batched writes,
`synchronous=NORMAL`, and bounded retention. You can reduce writes further by
increasing `batch_size` and `flush_interval_sec`.

**How much history does it keep?**
By default, raw events for 14 days and incidents for 90 days, subject to a 256 MB
hard cap that prunes the oldest events first. All of these are configurable.

**Can I run the CLI while the daemon is running?**
Yes. Query commands open the database read-only, and SQLite's WAL mode permits
concurrent readers while the daemon writes.

**Does it run on anything other than a Raspberry Pi?**
The service runs on any systemd-based Linux. The core (storage, classification,
CLI) also runs on other operating systems for development and testing.

**How do I report a security issue?**
Privately, following [SECURITY.md](SECURITY.md) — never through a public issue.

## Roadmap

Planned directions, in rough priority order. The architecture leaves a clean seam
for each:

- Additional collectors (for example, Docker/container and non-systemd process
  sources) behind the existing `Source` interface.
- Optional alerting sinks (email, Telegram, and similar) after classification.
- A lightweight local web dashboard for at-a-glance health.
- D-Bus signal subscription to complement unit-state polling for lower-latency
  detection.
- Optional off-device log forwarding for long-term durability.

Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## Documentation index

| Document | Purpose |
| --- | --- |
| [README.md](README.md) | This document: overview, installation, usage, administration. |
| [LICENSE](LICENSE) | The full Angad Singh Personal & Non-Commercial Source Available License. |
| [NOTICE](NOTICE) | Copyright, license summary, trademark and commercial-licensing notice. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute and the terms that apply to contributions. |
| [SECURITY.md](SECURITY.md) | Supported versions and private vulnerability reporting. |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Expected conduct for participation. |
| [CHANGELOG.md](CHANGELOG.md) | Release history, starting at 1.0.0. |
| [config/config.example.toml](config/config.example.toml) | Annotated configuration template. |

## License summary

HeartBeat Logger is distributed under the **Angad Singh Personal & Non-Commercial
Source Available License, Version 1.0** — see [LICENSE](LICENSE) for the
authoritative terms.

In brief, and subject to the full text:

- **Permitted:** personal use, educational and academic use, research, hobby
  projects, private modifications and private forks, and viewing and learning from
  the source.
- **Requires prior written permission:** any commercial use, including selling the
  software or modified versions, offering it as a hosted or paid service (SaaS),
  bundling it with commercial products or hardware, using it in commercial
  products, commercial consulting built around it, relicensing, and commercial use
  of the project's branding.
- **Ownership:** copyright remains with Angad Singh Bains at all times; all rights
  not expressly granted are reserved. Commercial licenses may be granted solely by
  the copyright holder — see the contact in [NOTICE](NOTICE).

This is a **Source Available** license, **not** an Open Source license as defined
by the Open Source Initiative. Contribution terms are described in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This software is provided "as is", without warranty of any kind, express or
implied, including but not limited to the warranties of merchantability, fitness
for a particular purpose, and non-infringement. To the maximum extent permitted by
applicable law, the copyright holder shall not be liable for any claim, damages, or
other liability arising from, out of, or in connection with the software or its
use. HeartBeat Logger is a diagnostic aid and does not guarantee the detection of
every failure; it must not be relied upon as the sole safeguard in safety-critical
or life-critical systems. See the [LICENSE](LICENSE) for the complete warranty and
liability terms.

---

Copyright (c) 2026 Angad Singh Bains. All rights reserved.

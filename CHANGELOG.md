# Changelog

Copyright (c) 2026 Angad Singh Bains. All rights reserved.

All notable changes to HeartBeat Logger are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Deprecated

### Removed

### Fixed

### Security

## [1.0.0] - 2026-07-09

Initial public release.

### Added

- **Project creation** — first release of HeartBeat Logger, a logging and health
  service for systemd-managed services on a Raspberry Pi.
- **Architecture** — a supervised collector daemon (`hblogd`) and a read-only
  command-line interface (`hblog`) sharing a single local SQLite database, with
  collectors sitting behind a pluggable `Source` interface.
- **Journal collection** — real-time journald reader that resumes from a
  persisted cursor after a restart, without gaps or duplicates.
- **Unit-state monitoring** — periodic polling of systemd unit state to detect
  crashes (by exit code or signal), failed units, restart loops, and
  out-of-memory kills, and to record a per-service liveness heartbeat.
- **Problem classification and incidents** — automatic classification of events
  and grouping of repeated problems into deduplicated incidents that resolve
  automatically when a service recovers.
- **Automatic log archival and storage management** — batched writes, age-based
  and size-capped retention, and incremental vacuuming to bound disk use and
  reduce SD-card wear.
- **Crash safety** — write-ahead logging and single-transaction batches so the
  database stays consistent across power loss or an unexpected shutdown, with the
  daemon resuming cleanly on the next start.
- **Command-line interface** — `status`, `crashes`, `incidents`, `logs`
  (including follow mode), `stats`, `prune`, `vacuum`, and `demo` commands for
  inspecting service health and managing the store over SSH.
- **Service integration** — a hardened systemd unit running under a dedicated
  unprivileged account, with an `sd_notify` watchdog so systemd restarts the
  logger if it stops responding.
- **Configuration** — a TOML configuration file covering watched services,
  polling and batching intervals, detection thresholds, and retention limits,
  with a documented example at `config/config.example.toml`.
- **Documentation and governance** — repository documentation including the
  README, LICENSE, NOTICE, contribution guidelines, security policy, and code of
  conduct.

[Unreleased]: https://github.com/Angad7600123/HeartBeat-Logger-Service/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Angad7600123/HeartBeat-Logger-Service/releases/tag/v1.0.0

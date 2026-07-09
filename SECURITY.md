# Security Policy

Copyright (c) 2026 Angad Singh Bains. All rights reserved.

The maintainers take the security of HeartBeat Logger seriously. This document
explains which versions receive security fixes and how to report a vulnerability
responsibly.

## Supported versions

Security fixes are provided for the most recent release line. Older releases are
not maintained; please upgrade before reporting an issue against them.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

See the [CHANGELOG](CHANGELOG.md) for the list of releases.

## Reporting a vulnerability

**Do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.** Public disclosure before a fix is available puts
users at risk.

Instead, report vulnerabilities privately by email to:

    singh4anga@gmail.com

Please include as much of the following as you can:

- A description of the vulnerability and its potential impact.
- The affected version or commit.
- Step-by-step instructions to reproduce the issue.
- Any proof-of-concept code, logs, or configuration required to demonstrate it.
- Your assessment of severity, if you have one.

If you would like to encrypt your report, say so in an initial message and the
maintainers will coordinate a secure channel.

## Responsible disclosure process

We follow a coordinated disclosure model:

1. **Acknowledgement** — we aim to acknowledge your report within a few business
   days of receipt.
2. **Assessment** — we investigate, confirm the issue, and determine the affected
   versions and severity.
3. **Remediation** — we develop and test a fix, and prepare an advisory.
4. **Release** — we publish the fix in a new release and document it in the
   [CHANGELOG](CHANGELOG.md).
5. **Disclosure** — we publicly credit the reporter, if desired, once a fix is
   available.

We ask that you give us a reasonable opportunity to remediate an issue before any
public disclosure, and that you avoid privacy violations, data destruction, or
service disruption while investigating.

## Scope

This policy covers the source code and packaging contained in this repository.
Vulnerabilities in third-party dependencies (for example, the system Python
runtime or the systemd bindings) should be reported to their respective
maintainers; where such an issue affects HeartBeat Logger, we will track and
mitigate it here.

## Hardening guidance

Deployment and operational hardening guidance — including the unprivileged
service account, filesystem protections, and the read-only database access model
used by the command-line interface — is described in the **Security model**
section of the [README](README.md#security-model).

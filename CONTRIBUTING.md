# Contributing to HeartBeat Logger

Copyright (c) 2026 Angad Singh Bains. All rights reserved.

Thank you for your interest in improving HeartBeat Logger. Contributions are
welcome from anyone. Before you invest time, please read this document in full —
in particular the [Contribution terms](#contribution-terms), which apply
automatically to every pull request.

This project is distributed under the **Angad Singh Personal & Non-Commercial
Source Available License** (see [LICENSE](LICENSE)). Contributing does not change
the ownership or licensing model described there.

---

## Contribution terms

These terms form an agreement between you (the "Contributor") and the repository
owner and copyright holder, Angad Singh Bains (the "Owner"). **By submitting a
pull request, patch, or any other contribution ("Contribution") to this project,
you agree to all of the terms in this section.** If you do not agree, do not
submit a Contribution.

### Ownership of your Contribution

- Submitting a Contribution does **not** transfer ownership of the project to
  you, and does not grant you any ownership interest in the project as a whole.
- You retain copyright in the original work of authorship that you contribute.

### License grant to the Owner

By submitting a Contribution, you automatically grant the Owner a **perpetual,
worldwide, irrevocable, royalty-free, sublicensable, and transferable** license
to:

- use,
- reproduce,
- modify,
- adapt and rewrite,
- redistribute,
- publicly display and perform,
- relicense,
- commercially license, and
- merge into or remove from the project

your Contribution, in whole or in part, in any form and for any purpose,
including commercial purposes.

### Rights reserved by the Owner

You acknowledge and agree that the Owner may, at the Owner's sole discretion:

- relicense future versions of the project under different terms;
- make future versions of the project closed source;
- sell commercial licenses to the project;
- dual-license the project;
- accept, reject, or defer any Contribution;
- rewrite, modify, or refactor any Contribution; and
- remove any Contribution from the project at any time.

### Your representations

By submitting a Contribution, you represent and warrant that:

- the Contribution is your original work, or you have the necessary rights to
  submit it under these terms;
- your Contribution does not knowingly infringe the intellectual property rights
  of any third party; and
- if your employer or any third party has rights to work you create, you have
  received the necessary permission to submit the Contribution, or your employer
  or that third party has waived such rights for this Contribution.

These terms take effect the moment a Contribution is submitted and require no
separate signature.

---

## How to contribute

1. **Open an issue first for anything non-trivial.** Describe the problem or
   proposal so the approach can be agreed before code is written. Security issues
   must follow [SECURITY.md](SECURITY.md) and must **not** be filed as public
   issues.
2. **Fork** the repository and create a topic branch (see
   [Branch naming](#branch-naming)).
3. **Make your change** following the guidelines below.
4. **Add or update tests** and ensure the full suite passes.
5. **Open a pull request** against the default branch with a clear description of
   what changed and why, and a reference to the related issue.

Small, focused pull requests are reviewed faster than large, mixed ones. Please
keep unrelated changes in separate pull requests.

---

## Development setup

HeartBeat Logger's core is pure standard-library Python and runs on any platform.
The systemd and journald integrations only run on Linux, but the whole pipeline
is testable elsewhere via the mock source.

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows:     .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

See the [README](README.md#building-from-source) for more detail on building and
running from source.

---

## Coding guidelines

- **Language and version.** Target Python 3.11 or newer. Use only the standard
  library in the core; platform bindings (for example `systemd`) must be imported
  lazily so the package still imports on any OS.
- **Style.** Follow [PEP 8](https://peps.python.org/pep-0008/). Keep lines within
  a reasonable width (the existing code targets roughly 90 columns). Match the
  surrounding code's naming, structure, and comment density.
- **Type hints.** Public functions and dataclasses should be typed, consistent
  with the existing modules.
- **Docstrings.** Every module and public function should have a concise
  docstring explaining intent, not restating the code.
- **Architecture.** Respect the existing separation of concerns: sources under
  `hblog/sources/`, classification in `hblog/classify.py`, storage in
  `hblog/db.py`, orchestration in `hblog/daemon.py` and `hblog/pipeline.py`, and
  presentation in `hblog/cli.py`. New collectors should implement the `Source`
  interface in `hblog/sources/base.py`.
- **Dependencies.** Do not add third-party runtime dependencies to the core
  without prior discussion. The project deliberately stays lightweight so it runs
  comfortably on a Raspberry Pi.

---

## Testing expectations

- The full suite must pass before a pull request is merged: run `pytest` from the
  repository root.
- New behavior requires new tests. Bug fixes should include a test that fails
  without the fix.
- Tests must run on any platform without requiring a Pi. Exercise systemd- and
  journald-facing code through injected runners or the mock source, following the
  patterns in `tests/test_units.py` and `tests/test_pipeline.py`.
- Keep tests deterministic — inject clocks and inputs rather than relying on real
  time, sleeps, or the host's services.

---

## Documentation guidelines

- Update the relevant documentation in the same pull request as the code change.
  User-facing behavior changes must be reflected in the [README](README.md); the
  example configuration in `config/config.example.toml` must stay in sync with
  the options defined in `hblog/config.py`.
- Use clear, professional English and valid GitHub-flavored Markdown.
- Cross-reference related documents with relative links rather than duplicating
  content.
- Record every user-visible change in [CHANGELOG.md](CHANGELOG.md) under the
  `Unreleased` section, following the Keep a Changelog format.

---

## Commit message style

Write clear, imperative-mood commit messages that explain the change and its
motivation.

- **Subject line:** imperative mood, capitalized, no trailing period, ideally
  50 characters or fewer. Example: `Add restart-loop detection to unit monitor`.
- **Body:** separated from the subject by a blank line; wrap at about 72
  characters; explain *what* changed and *why*, not just *how*.
- **References:** mention the related issue where applicable (for example,
  `Refs #12` or `Closes #12`).
- Keep each commit focused on a single logical change.

---

## Branch naming

Create topic branches with a short, descriptive, hyphenated name prefixed by a
category:

- `feature/<summary>` — new functionality (for example, `feature/telegram-alerts`)
- `fix/<summary>` — bug fixes (for example, `fix/cursor-resume-gap`)
- `docs/<summary>` — documentation-only changes
- `test/<summary>` — test-only changes
- `chore/<summary>` — maintenance, tooling, or packaging

Do not commit directly to the default branch.

---

## Code of conduct

All participation in this project is governed by our
[Code of Conduct](CODE_OF_CONDUCT.md). By contributing, you agree to uphold it.

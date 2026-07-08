"""Source interface: a collector that yields normalized Events.

Keeping collection behind this tiny interface is what lets the whole pipeline,
DB and CLI be developed and tested on Windows: the journald/systemd sources are
swapped for `MockSource` off-Pi.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator

from ..models import Event


class Source(abc.ABC):
    """A stream of events. `events()` may block/follow indefinitely (real sources)
    or yield a finite sequence and return (mock/replay)."""

    name: str = "base"

    @abc.abstractmethod
    def events(self) -> Iterator[Event]:
        ...

    def close(self) -> None:  # pragma: no cover - default no-op
        pass

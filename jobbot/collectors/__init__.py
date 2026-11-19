from __future__ import annotations

from ..models import Job


class Collector:
    """Base class. A collector never raises out of collect(): failures are
    recorded in .errors and whatever was collected so far is still returned."""

    name = "collector"

    def __init__(self) -> None:
        self.errors: list[str] = []

    def collect(self) -> list[Job]:  # pragma: no cover - interface
        raise NotImplementedError

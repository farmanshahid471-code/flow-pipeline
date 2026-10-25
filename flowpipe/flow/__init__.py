"""Driver registry."""
from __future__ import annotations

from ..config import SETTINGS
from .base import FlowDriver
from .gflow import GflowDriver
from .mock import MockDriver


def get_driver(kind: str | None = None) -> FlowDriver:
    kind = kind or SETTINGS.driver
    if kind == "gflow":
        return GflowDriver()
    return MockDriver()


def driver_name() -> str:
    return get_driver().name

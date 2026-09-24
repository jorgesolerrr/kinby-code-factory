"""Serialize factory reports for routine data."""

import json
from collections.abc import Sequence
from dataclasses import Field, asdict
from typing import ClassVar, Protocol


class FactoryReport(Protocol):
    """A dataclass report returned by a factory routine."""

    __dataclass_fields__: ClassVar[dict[str, Field[object]]]


def report_json(report: FactoryReport | Sequence[FactoryReport]) -> str:
    """Return compact JSON for a structured factory report."""
    value = [asdict(item) for item in report] if isinstance(report, Sequence) else asdict(report)
    return json.dumps(value, separators=(",", ":"))

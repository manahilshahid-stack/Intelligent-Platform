"""Standard KPI field definitions shared across extraction, review, and embeddings."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StandardKpiDef:
    key: str
    label: str
    field_type: str  # matches KpiFieldType values


STANDARD_KPIS: list[StandardKpiDef] = [
    StandardKpiDef("cash_position",  "Cash position",  "currency"),
    StandardKpiDef("monthly_burn",   "Monthly burn",   "currency"),
    StandardKpiDef("runway_months",  "Runway (months)", "number"),
    StandardKpiDef("revenue",        "Revenue",        "currency"),
    StandardKpiDef("arr",            "ARR",            "currency"),
    StandardKpiDef("headcount",      "Headcount",      "number"),
    StandardKpiDef("customers",      "Customers",      "number"),
]

STANDARD_KPI_KEYS: frozenset[str] = frozenset(k.key for k in STANDARD_KPIS)

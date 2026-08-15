"""How each service is made to fail.

Faults are configuration, not code paths. A test case switches them on in YAML,
and the service behaves badly in a way that leaves an observable symptom behind.
The agent never sees these objects - it only sees the symptoms.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class WmsFaults:
    """Ways the warehouse system can mislead us."""

    #: The query never comes back.
    timeout: bool = False
    #: We are reading a copy that last synchronised on this date. Anything
    #: dispatched after it is simply missing, with no error raised.
    stale_as_of: date | None = None
    #: The same shipment comes back twice.
    duplicate_rows: bool = False
    #: The duplicate names a different batch, so the rows contradict each other.
    conflicting_rows: bool = False


@dataclass(frozen=True)
class LookupFaults:
    """Ways a paid external lookup can fail."""

    unavailable: bool = False
    #: Return the records but leave the allocation history out.
    partial: bool = False


@dataclass(frozen=True)
class ServiceFaults:
    wms: WmsFaults = WmsFaults()
    registry: LookupFaults = LookupFaults()
    ledger: LookupFaults = LookupFaults()

    @staticmethod
    def none() -> ServiceFaults:
        return ServiceFaults()

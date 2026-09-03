"""Audit-first acquisition control plane.

The package deliberately separates acquisition policy, execution state and
immutable evidence from the legacy financial analysis models.  Importing the
package performs no filesystem or network I/O.
"""

from .source_gate import CrossProcessSourceGate, LocalSourceGateTimeout

__all__ = ["CrossProcessSourceGate", "LocalSourceGateTimeout"]

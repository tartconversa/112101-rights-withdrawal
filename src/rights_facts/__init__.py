"""Local, reproducible rights-coverage data lake for synthetic content licences.

The package deliberately uses only the Python standard library.  It ingests
immutable source snapshots ("waves"), validates them field by field, appends
normalised facts to an append-only SQLite lake, and derives a coverage view for
each source snapshot.  No verdict is stored: every evaluation is reproducible
from the snapshot alone and carries an explanation back to the source records.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"

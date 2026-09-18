"""Push a local FAIRSCAPE RO-Crate, with all of its local files, to a data repository."""

__version__ = "0.1.0"

from fairscape_publish.crate import Crate, CrateError, load_crate
from fairscape_publish.models import CratePayload, Deposit, Issue, Layout, LocalFile

__all__ = [
    "__version__",
    "Crate",
    "CrateError",
    "load_crate",
    "CratePayload",
    "Deposit",
    "Issue",
    "Layout",
    "LocalFile",
]

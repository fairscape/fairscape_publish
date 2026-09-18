"""Repository targets."""

from fairscape_publish.targets.base import Target, TargetError
from fairscape_publish.targets.datacite import DataCiteTarget
from fairscape_publish.targets.dataverse import DataverseTarget
from fairscape_publish.targets.figshare import FigshareTarget
from fairscape_publish.targets.zenodo import ZenodoTarget

__all__ = [
    "Target",
    "TargetError",
    "DataCiteTarget",
    "DataverseTarget",
    "FigshareTarget",
    "ZenodoTarget",
]

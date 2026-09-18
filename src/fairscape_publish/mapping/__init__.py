"""Pure crate-metadata -> repository-metadata transforms. No I/O."""

from fairscape_publish.mapping import common, dataverse, figshare, licenses, zenodo
from fairscape_publish.mapping import datacite

__all__ = ["common", "datacite", "dataverse", "figshare", "licenses", "zenodo"]

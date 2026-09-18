"""Shared data structures for fairscape_publish.

These are deliberately small and target-agnostic: `crate.py` produces them,
`mapping/` reads them, and `targets/` consumes them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Layout(str, Enum):
    """How a crate's directory tree is projected onto a repository."""

    ZIP = "zip"
    FLAT = "flat"
    PRESERVE = "preserve"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class Issue(BaseModel):
    """A preflight finding. `ERROR` blocks the run, `WARNING` is advisory."""

    severity: Severity
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.field}: {self.message}"


class LocalFile(BaseModel):
    """A file on disk that belongs to the crate."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    relpath: str = Field(description="POSIX path relative to the crate root.")
    abspath: Path
    size: int
    entity_id: Optional[str] = Field(
        default=None, description="@id of the crate entity whose contentUrl resolved here, if any."
    )
    description: Optional[str] = None
    md5: Optional[str] = Field(default=None, description="Checksum declared in the crate, not computed.")

    @property
    def directory(self) -> str:
        """POSIX dirname, '' for files at the crate root."""
        parent = str(Path(self.relpath).parent)
        return "" if parent == "." else parent.replace("\\", "/")


class RemoteEntity(BaseModel):
    """A crate entity whose contentUrl points off-machine. Never downloaded."""

    entity_id: str
    name: Optional[str] = None
    content_url: str
    content_size: Optional[str] = None


class FileUpload(BaseModel):
    """One planned upload: a local path plus the name it takes on the far side."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    key: str = Field(description="Filename as it will appear in the repository.")
    abspath: Path
    size: int
    relpath: Optional[str] = Field(
        default=None, description="Crate-relative source path; None for generated artifacts such as the crate zip."
    )
    directory_label: str = Field(
        default="", description="Subdirectory for repositories that support one (Dataverse)."
    )
    description: Optional[str] = None
    is_generated: bool = Field(
        default=False, description="True for the crate zip and other files built at publish time."
    )

    @property
    def identity(self) -> str:
        """What makes this upload distinct within one deposit.

        Under `preserve` layout the crate-relative path is the only unique
        handle: three MLflow runs each publish a `model.pkl`, and keying on the
        filename alone would make them the same file.
        """
        return self.relpath or self.key


class CratePayload(BaseModel):
    """Everything the mapping layer needs, extracted from a loaded crate."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    crate_root: Path
    metadata_path: Path
    guid: str
    name: str
    description: str = ""
    keywords: List[str] = Field(default_factory=list)
    version: Optional[str] = None
    license_url: Optional[str] = None
    date_published: Optional[str] = None
    date_created: Optional[str] = None
    authors: List["Author"] = Field(default_factory=list)
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    publisher: Optional[str] = None
    associated_publication: Optional[str] = None
    url: Optional[str] = None
    root: Dict[str, Any] = Field(default_factory=dict, description="Raw root entity node.")
    files: List[LocalFile] = Field(default_factory=list)
    remote_entities: List[RemoteEntity] = Field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


class Author(BaseModel):
    """A normalized creator, whatever shape it had in the crate."""

    name: str = Field(description="Display name, preferably 'Family, Given'.")
    affiliation: Optional[str] = None
    orcid: Optional[str] = Field(default=None, description="Bare ORCID, e.g. 0000-0002-1825-0097.")


CratePayload.model_rebuild()


class Deposit(BaseModel):
    """A record created on a repository but not necessarily published."""

    target: str
    deposit_id: str
    base_url: str
    doi: Optional[str] = None
    landing_url: Optional[str] = None
    bucket_url: Optional[str] = Field(default=None, description="Zenodo file-bucket link.")
    persistent_id: Optional[str] = Field(default=None, description="Dataverse persistentId.")
    doi_registered: bool = Field(
        default=False,
        description=(
            "False while the DOI is only reserved against a draft. A reserved DOI is not "
            "resolvable and can still change - Zenodo sandbox reserves under the production "
            "prefix 10.5281 but assigns 10.5072 at publish time."
        ),
    )
    state: str = "draft"
    raw: Dict[str, Any] = Field(default_factory=dict)


class UploadedFile(BaseModel):
    key: str
    relpath: Optional[str] = None
    size: int = 0
    md5: Optional[str] = None
    remote_id: Optional[str] = None
    uploaded_at: str = Field(default_factory=utcnow)

    @property
    def identity(self) -> str:
        """Matches FileUpload.identity, so a resume compares like with like."""
        return self.relpath or self.key

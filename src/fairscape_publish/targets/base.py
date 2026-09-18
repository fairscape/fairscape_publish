"""The contract every repository target implements."""

from __future__ import annotations

import io
import mimetypes
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

from fairscape_publish.models import CratePayload, Deposit, FileUpload, Issue, Layout, UploadedFile
from fairscape_publish.transport import Session


class Target(ABC):
    """A repository we can deposit into.

    Lifecycle: ``preflight`` (offline) -> ``create_deposit`` -> ``upload_file`` per
    file -> ``finalize`` -> optionally ``publish``.
    """

    #: Short name used in receipts and on the command line.
    name: str = ""
    #: Layout used when the caller does not specify one.
    default_layout: Layout = Layout.ZIP
    #: Layouts this repository can actually represent.
    supported_layouts: Sequence[Layout] = (Layout.ZIP, Layout.FLAT, Layout.PRESERVE)

    def __init__(self, session: Session, base_url: str):
        self.session = session
        self.base_url = base_url.rstrip("/")

    @abstractmethod
    def preflight(self, payload: CratePayload, uploads: Sequence[FileUpload]) -> List[Issue]:
        """Report what would go wrong, without making a request."""

    @abstractmethod
    def build_metadata(self, payload: CratePayload) -> Dict[str, Any]:
        """The repository-shaped metadata document. Pure; safe to print."""

    @abstractmethod
    def create_deposit(self, payload: CratePayload) -> Deposit:
        """Create the draft record."""

    @abstractmethod
    def upload_file(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        """Upload one file into an existing draft."""

    def finalize(self, deposit: Deposit) -> None:
        """Hook for anything a repository needs after the last upload. Usually nothing."""

    @abstractmethod
    def publish(self, deposit: Deposit) -> Deposit:
        """Make the record public. Only ever called under an explicit --publish."""

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def guess_mimetype(path: Path) -> str:
        return mimetypes.guess_type(str(path))[0] or "application/octet-stream"

    @contextmanager
    def open_upload(self, upload: FileUpload) -> Iterator[Any]:
        """Open a file for upload.

        Under --dry-run the crate zip is planned but never built, so there is
        nothing on disk to open; an empty stream keeps the request shape honest
        without fabricating content.
        """
        if self.session.dry_run and not Path(upload.abspath).exists():
            stream = io.BytesIO(b"")
            stream.name = upload.key  # requests reads .name when guessing a filename
            yield stream
            return
        with open(upload.abspath, "rb") as handle:
            yield handle

    def checksum(self, upload: FileUpload) -> str:
        """MD5 of the file, or a placeholder when dry-running an unbuilt artifact."""
        from fairscape_publish.packaging import md5_of

        if self.session.dry_run and not Path(upload.abspath).exists():
            return "0" * 32
        return md5_of(upload.abspath)

    @staticmethod
    def dry_run_id(prefix: str) -> str:
        return f"{prefix}-dry-run"


class TargetError(Exception):
    """A repository rejected something in a way the user needs to see."""

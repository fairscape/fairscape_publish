"""Publication receipts and optional write-back into the crate.

The receipt lives at ``.fairscape-publish.json`` next to ro-crate-metadata.json.
It is what makes `--resume` possible on a crate with hundreds of files, and what
`status` reads.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from fairscape_publish.crate import METADATA_FILENAME, root_entity
from fairscape_publish.models import Deposit, UploadedFile, utcnow

RECEIPT_FILENAME = ".fairscape-publish.json"
SCHEMA_VERSION = 1


class DepositRecord(BaseModel):
    target: str
    base_url: str
    deposit_id: str
    doi: Optional[str] = None
    landing_url: Optional[str] = None
    persistent_id: Optional[str] = None
    doi_registered: bool = False
    #: Zenodo's file-bucket link. Kept so --resume can go on using the streaming
    #: endpoint instead of silently dropping to the legacy 100 MB one.
    bucket_url: Optional[str] = None
    state: str = "draft"
    layout: str = "zip"
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    files: List[UploadedFile] = Field(default_factory=list)

    def key(self) -> str:
        return f"{self.target}:{self.base_url}:{self.deposit_id}"

    def has_uploaded(self, identity: str, size: int, md5: Optional[str] = None) -> bool:
        """Was this exact file already sent? `identity` is the crate-relative
        path where there is one -- a bare filename repeats across directories."""
        for uploaded in self.files:
            if uploaded.identity != identity:
                continue
            if uploaded.size != size:
                return False
            if md5 and uploaded.md5 and uploaded.md5 != md5:
                return False
            return True
        return False

    def record_upload(self, uploaded: UploadedFile) -> None:
        self.files = [f for f in self.files if f.identity != uploaded.identity]
        self.files.append(uploaded)
        self.updated_at = utcnow()


class Receipt(BaseModel):
    schema_version: int = SCHEMA_VERSION
    deposits: List[DepositRecord] = Field(default_factory=list)

    def find(self, target: str, base_url: str, deposit_id: Optional[str] = None) -> Optional[DepositRecord]:
        for record in reversed(self.deposits):
            if record.target != target or record.base_url != base_url:
                continue
            if deposit_id is None or record.deposit_id == deposit_id:
                return record
        return None

    def upsert(self, record: DepositRecord) -> DepositRecord:
        for index, existing in enumerate(self.deposits):
            if existing.key() == record.key():
                self.deposits[index] = record
                return record
        self.deposits.append(record)
        return record


def receipt_path(crate_root: Path) -> Path:
    return Path(crate_root) / RECEIPT_FILENAME


def load_receipt(crate_root: Path) -> Receipt:
    path = receipt_path(crate_root)
    if not path.exists():
        return Receipt()
    try:
        with path.open("r", encoding="utf-8") as handle:
            return Receipt.model_validate(json.load(handle))
    except (json.JSONDecodeError, ValueError):
        # A corrupt receipt should not block a publish; it is a cache, not the source of truth.
        return Receipt()


def save_receipt(crate_root: Path, receipt: Receipt) -> Path:
    path = receipt_path(crate_root)
    _write_json_atomic(path, receipt.model_dump(mode="json", exclude_none=True))
    return path


def record_from_deposit(deposit: Deposit, layout: str) -> DepositRecord:
    return DepositRecord(
        target=deposit.target,
        base_url=deposit.base_url,
        deposit_id=deposit.deposit_id,
        doi=deposit.doi,
        landing_url=deposit.landing_url,
        persistent_id=deposit.persistent_id,
        doi_registered=deposit.doi_registered,
        bucket_url=deposit.bucket_url,
        state=deposit.state,
        layout=layout,
    )


def write_back(crate_root: Path, deposit: Deposit) -> Optional[Path]:
    """Record the assigned identifier on the crate's root entity.

    Only the root crate's ro-crate-metadata.json is touched; subcrates are left
    alone. ``identifier`` accumulates (a crate can be deposited more than once);
    ``url`` is set only if the crate does not already have one.
    """
    metadata_path = Path(crate_root) / METADATA_FILENAME
    if not metadata_path.exists():
        return None

    with metadata_path.open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)

    root = root_entity(metadata.get("@graph", []))
    if root is None:
        return None

    new_identifier = deposit.doi or deposit.persistent_id
    if new_identifier:
        existing = root.get("identifier")
        identifiers = existing if isinstance(existing, list) else ([existing] if existing else [])
        if new_identifier not in identifiers:
            identifiers.append(new_identifier)
        root["identifier"] = identifiers if len(identifiers) > 1 else identifiers[0]

    if deposit.landing_url and not root.get("url"):
        root["url"] = deposit.landing_url

    _write_json_atomic(metadata_path, metadata)
    return metadata_path


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)

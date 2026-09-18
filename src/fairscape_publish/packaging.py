"""Turn discovered crate files into a concrete upload plan."""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from fairscape_publish.crate import METADATA_FILENAME
from fairscape_publish.models import FileUpload, Layout, LocalFile

CHUNK_SIZE = 8 * 1024 * 1024


def md5_of(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.md5()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def flat_name(relpath: str) -> str:
    """`data/raw.csv` -> `data__raw.csv`, so a flat repository keeps the provenance."""
    return relpath.replace("/", "__")


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def zip_basename(crate_root: Path, guid: str = "") -> str:
    """A stable, filesystem-safe name for the crate archive."""
    stem = crate_root.name or "rocrate"
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in stem).strip("-")
    return f"{safe or 'rocrate'}.zip"


def build_crate_zip(
    crate_root: Path,
    files: Sequence[LocalFile],
    out_dir: Optional[Path] = None,
    name: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Path:
    """Zip exactly the discovered files, preserving crate-relative paths.

    Built from the discovered file list rather than a fresh directory walk so
    that --exclude and --only-registered mean the same thing in every layout.
    """
    out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="fairscape-publish-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (name or zip_basename(crate_root))

    ordered = sorted(files, key=lambda f: f.relpath)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for item in ordered:
            if progress:
                progress(item.relpath)
            archive.write(item.abspath, arcname=item.relpath)
    return target


def plan_uploads(
    crate_root: Path,
    files: Sequence[LocalFile],
    layout: Layout,
    guid: str = "",
    work_dir: Optional[Path] = None,
    include_metadata_sidecar: bool = True,
    build: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> List[FileUpload]:
    """Decide what actually gets uploaded, and under what names.

    - ``zip``      one archive of the crate, plus ro-crate-metadata.json alongside
                   it so the landing page shows readable metadata
    - ``flat``     every file, path-encoded into the filename
    - ``preserve`` every file, with its directory carried in ``directory_label``
                   (Dataverse honours this; flat repositories ignore it)

    ``build=False`` plans the zip layout without writing the archive, so a dry run
    on a terabyte-scale crate stays instant. The reported size is then the
    uncompressed total, which is an upper bound.
    """
    if layout is Layout.ZIP:
        if build:
            archive = build_crate_zip(crate_root, files, out_dir=work_dir, progress=progress)
            archive_size = archive.stat().st_size
        else:
            archive = (Path(work_dir) if work_dir else crate_root) / zip_basename(crate_root, guid)
            archive_size = sum(f.size for f in files)
        uploads = [
            FileUpload(
                key=archive.name,
                abspath=archive,
                size=archive_size,
                relpath=None,
                description="RO-Crate archive containing all crate files.",
                is_generated=True,
            )
        ]
        if include_metadata_sidecar:
            sidecar = crate_root / METADATA_FILENAME
            if sidecar.is_file():
                uploads.append(
                    FileUpload(
                        key=METADATA_FILENAME,
                        abspath=sidecar,
                        size=sidecar.stat().st_size,
                        relpath=METADATA_FILENAME,
                        description="RO-Crate metadata for this deposit.",
                    )
                )
        return uploads

    uploads = []
    for item in files:
        key = flat_name(item.relpath) if layout is Layout.FLAT else Path(item.relpath).name
        uploads.append(
            FileUpload(
                key=key,
                abspath=item.abspath,
                size=item.size,
                relpath=item.relpath,
                directory_label=item.directory if layout is Layout.PRESERVE else "",
                description=item.description,
            )
        )
    return _dedupe_keys(uploads)


def _dedupe_keys(uploads: List[FileUpload]) -> List[FileUpload]:
    """Two files can share a basename under `preserve`; a flat repo would clobber one.

    Uniqueness is per (directory_label, key), which is exactly the namespace
    Dataverse enforces and a superset of what flat repositories need.
    """
    taken = set()
    for upload in uploads:
        stem, ext = os.path.splitext(upload.key)
        counter = 1
        while (upload.directory_label, upload.key) in taken:
            upload.key = f"{stem}-{counter}{ext}"
            counter += 1
        taken.add((upload.directory_label, upload.key))
    return uploads

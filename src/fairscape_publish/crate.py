"""Load a local RO-Crate and work out which files belong to it.

The FAIRSCAPE convention for local content is ``contentUrl: "file:///<path>"``
where the path after the scheme is relative to the *crate root directory*, not
to the filesystem root. See ``resolve_content_url``.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fairscape_publish.models import LocalFile, RemoteEntity

METADATA_FILENAME = "ro-crate-metadata.json"

EVI_ROCRATE_TYPE = "https://w3id.org/EVI#ROCrate"

REMOTE_SCHEMES = ("http://", "https://", "ftp://", "ftps://", "s3://", "gs://", "sftp://")

#: Never shipped to a repository, no matter how the crate is laid out.
DEFAULT_EXCLUDES: Tuple[str, ...] = (
    ".git/*",
    ".git",
    ".gitignore",
    ".DS_Store",
    "*/.DS_Store",
    "__pycache__/*",
    "*/__pycache__/*",
    "*.pyc",
    ".ipynb_checkpoints/*",
    "*/.ipynb_checkpoints/*",
    ".fairscape-publish.json",
    ".fairscape-state.json",
    "*.egg-info/*",
)


class CrateError(Exception):
    """Raised when a crate cannot be read or does not validate."""


class Crate:
    """A crate directory plus its parsed (and optionally validated) metadata."""

    def __init__(self, root: Path, metadata_path: Path, metadata: Dict[str, Any]):
        self.root = root
        self.metadata_path = metadata_path
        self.metadata = metadata

    @property
    def graph(self) -> List[Dict[str, Any]]:
        return self.metadata.get("@graph", [])

    @property
    def root_entity(self) -> Dict[str, Any]:
        entity = root_entity(self.graph)
        if entity is None:
            raise CrateError(f"Could not find a root dataset node in {self.metadata_path}")
        return entity

    def node(self, guid: str) -> Optional[Dict[str, Any]]:
        for item in self.graph:
            if isinstance(item, dict) and item.get("@id") == guid:
                return item
        return None


def load_crate(path: Path, validate: bool = True) -> Crate:
    """Read a crate from a directory or a direct path to ro-crate-metadata.json."""
    path = Path(path)
    if path.is_dir():
        metadata_path = path / METADATA_FILENAME
        root = path
    elif path.name == METADATA_FILENAME:
        metadata_path = path
        root = path.parent
    else:
        raise CrateError(f"Expected a crate directory or a {METADATA_FILENAME}, got {path}")

    if not metadata_path.exists():
        raise CrateError(f"No {METADATA_FILENAME} found at {metadata_path}")

    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except json.JSONDecodeError as exc:
        raise CrateError(f"Invalid JSON in {metadata_path}: {exc}") from exc

    if validate:
        validate_metadata(metadata, metadata_path)

    return Crate(root=root.resolve(), metadata_path=metadata_path.resolve(), metadata=metadata)


def validate_metadata(metadata: Dict[str, Any], metadata_path: Path) -> None:
    """Validate against the FAIRSCAPE RO-Crate model.

    ``ROCrateV1_2``'s before-validator rewrites ``@graph`` entries into pydantic
    models *in place*, so validation runs against a deep copy and the caller keeps
    plain dicts to read and write.
    """
    from fairscape_models.rocrate import ROCrateV1_2

    try:
        ROCrateV1_2.model_validate(copy.deepcopy(metadata))
    except Exception as exc:  # pydantic ValidationError, or a TypeError from a malformed graph
        raise CrateError(
            f"{metadata_path} failed RO-Crate validation:\n{exc}\n"
            "Pass --no-validate to publish it anyway."
        ) from exc


def root_entity(graph: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """Find the node the crate is *about*.

    Follows the metadata descriptor's ``about`` link first, then falls back to
    the first EVI#ROCrate-typed node, then the first Dataset.
    """
    nodes = [item for item in graph if isinstance(item, dict)]

    descriptor = next((n for n in nodes if n.get("@id") == METADATA_FILENAME), None)
    if descriptor:
        about = descriptor.get("about")
        if isinstance(about, list):
            about = about[0] if about else None
        about_id = about.get("@id") if isinstance(about, dict) else about
        if about_id:
            match = next((n for n in nodes if n.get("@id") == about_id), None)
            if match:
                return match

    fallback = None
    for node in nodes:
        if node.get("@id") == METADATA_FILENAME:
            continue
        types = _as_list(node.get("@type"))
        if EVI_ROCRATE_TYPE in types:
            return node
        if fallback is None and "Dataset" in types:
            fallback = node
    return fallback


def resolve_content_url(url: Any, crate_root: Path) -> Optional[Path]:
    """Turn a crate ``contentUrl`` into a local path, or None if it is remote.

    - ``file:///data/x.csv`` -> ``crate_root/data/x.csv`` (FAIRSCAPE convention:
      the path is crate-relative even though it looks absolute)
    - ``data/x.csv``         -> ``crate_root/data/x.csv``
    - ``/abs/x.csv``         -> ``/abs/x.csv``
    - ``ftp://...``, ``https://...`` -> ``None``
    """
    if isinstance(url, list):
        url = url[0] if url else None
    if isinstance(url, dict):
        url = url.get("@id")
    if not isinstance(url, str) or not url.strip():
        return None

    url = url.strip()
    if url.lower().startswith(REMOTE_SCHEMES):
        return None

    if url.startswith("file:"):
        rest = url[len("file:"):]
        # file://host/path is not something FAIRSCAPE emits; empty authority only.
        if rest.startswith("//"):
            rest = rest[2:]
        return crate_root / rest.lstrip("/")

    candidate = Path(url)
    if candidate.is_absolute():
        return candidate
    return crate_root / url


def find_subcrates(crate_root: Path) -> List[Path]:
    """Every nested ro-crate-metadata.json below the root, excluding the root's own."""
    found = []
    for dirpath, dirnames, filenames in os.walk(crate_root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".ipynb_checkpoints"}]
        if METADATA_FILENAME in filenames:
            candidate = Path(dirpath) / METADATA_FILENAME
            if candidate.parent.resolve() != crate_root.resolve():
                found.append(candidate)
    return sorted(found)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _is_excluded(relpath: str, patterns: Iterable[str]) -> bool:
    name = os.path.basename(relpath)
    for pattern in patterns:
        if fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        # Directory patterns such as ".git/*" should also match nested depths.
        if pattern.endswith("/*") and (relpath.startswith(pattern[:-1]) or f"/{pattern[:-1]}" in f"/{relpath}"):
            return True
    return False


def _content_entities(crate: Crate) -> List[Tuple[Dict[str, Any], Path]]:
    """(node, owning crate root) for every node in the root crate and its subcrates."""
    entities: List[Tuple[Dict[str, Any], Path]] = [
        (node, crate.root) for node in crate.graph if isinstance(node, dict)
    ]
    for subcrate_path in find_subcrates(crate.root):
        try:
            with subcrate_path.open("r", encoding="utf-8") as handle:
                sub = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        sub_root = subcrate_path.parent
        entities.extend((node, sub_root) for node in sub.get("@graph", []) if isinstance(node, dict))
    return entities


def index_content_urls(crate: Crate) -> Tuple[Dict[str, Dict[str, Any]], List[RemoteEntity]]:
    """Map crate-relative path -> declaring entity, and collect remote entities.

    A node whose contentUrl is a list contributes one entry per resolvable path.
    """
    by_relpath: Dict[str, Dict[str, Any]] = {}
    remote: List[RemoteEntity] = []
    seen_remote = set()

    for node, owner_root in _content_entities(crate):
        raw = node.get("contentUrl")
        if raw is None:
            continue
        for url in _as_list(raw):
            resolved = resolve_content_url(url, owner_root)
            if resolved is None:
                key = (node.get("@id"), str(url))
                if key not in seen_remote:
                    seen_remote.add(key)
                    remote.append(
                        RemoteEntity(
                            entity_id=str(node.get("@id", "")),
                            name=node.get("name"),
                            content_url=str(url),
                            content_size=node.get("contentSize"),
                        )
                    )
                continue
            try:
                relpath = resolved.resolve().relative_to(crate.root).as_posix()
            except ValueError:
                # Points outside the crate; treat as unshippable rather than escaping the tree.
                continue
            by_relpath.setdefault(relpath, node)

    return by_relpath, remote


def discover_files(
    crate: Crate,
    exclude: Sequence[str] = (),
    only_registered: bool = False,
) -> Tuple[List[LocalFile], List[RemoteEntity]]:
    """Find every local file that should travel with the crate.

    Default: walk the crate directory, so datasheets, READMEs and each nested
    ro-crate-metadata.json go along too. ``only_registered`` restricts the set to
    files a crate entity actually declares via contentUrl.
    """
    patterns = list(DEFAULT_EXCLUDES) + list(exclude)
    entity_by_relpath, remote = index_content_urls(crate)

    if only_registered:
        candidates = sorted(entity_by_relpath)
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(crate.root):
            dirnames[:] = [
                d for d in dirnames if not _is_excluded(_rel(crate.root, Path(dirpath) / d), patterns)
            ]
            for filename in filenames:
                candidates.append(_rel(crate.root, Path(dirpath) / filename))
        candidates.sort()

    files: List[LocalFile] = []
    for relpath in candidates:
        if _is_excluded(relpath, patterns):
            continue
        abspath = crate.root / relpath
        if not abspath.is_file():
            continue
        node = entity_by_relpath.get(relpath, {})
        files.append(
            LocalFile(
                relpath=relpath,
                abspath=abspath,
                size=abspath.stat().st_size,
                entity_id=node.get("@id"),
                description=node.get("description"),
                md5=node.get("md5"),
            )
        )
    return files, remote


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()

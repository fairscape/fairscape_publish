"""Normalize the messy parts of a crate root entity into a CratePayload.

Real crates carry authors in four different shapes, keywords as a string or a
list, and dates in at least three formats. Every target mapping reads the
normalized result rather than re-deriving it.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from fairscape_publish.crate import Crate
from fairscape_publish.models import Author, CratePayload, LocalFile, RemoteEntity

ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dXx])")


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def split_names(value: str) -> List[str]:
    """Split a delimited author string.

    Semicolons win when present, because "Clark, T; Ideker, T" is a list of two
    while splitting on the comma would make it four.
    """
    if ";" in value:
        parts = value.split(";")
    elif "," in value and value.count(",") > 1:
        parts = value.split(",")
    else:
        parts = [value]
    return [p.strip() for p in parts if p.strip()]


def extract_orcid(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = ORCID_RE.search(value)
    return match.group(1).upper() if match else None


def _affiliation_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    if isinstance(value, list):
        for item in value:
            text = _affiliation_text(item)
            if text:
                return text
    return None


def _author_from_node(node: Dict[str, Any]) -> Optional[Author]:
    name = node.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    orcid = extract_orcid(node.get("identifier")) or extract_orcid(node.get("orcid")) or extract_orcid(
        node.get("@id")
    )
    return Author(
        name=name.strip(),
        affiliation=_affiliation_text(node.get("affiliation")),
        orcid=orcid,
    )


def normalize_authors(
    root: Dict[str, Any],
    crate: Optional[Crate] = None,
    authors_csv: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Author]:
    """Handle every author shape seen in real FAIRSCAPE crates.

    1. ``"Clark, T; Ideker, T"``            - delimited string
    2. ``["Clark, T", "Ideker, T"]``        - list of strings
    3. ``[{"name": ..., "affiliation": ...}]``
    4. ``[{"@id": "https://orcid.org/0000-..."}]`` - references into Person nodes
       elsewhere in the graph. CM4AI releases use this; resolving it is why the
       crate is passed in.
    """
    authors: List[Author] = []
    raw = root.get("author") or root.get("creator") or []

    for item in as_list(raw):
        if isinstance(item, str):
            for name in split_names(item):
                authors.append(Author(name=name))
        elif isinstance(item, dict):
            if "name" in item:
                author = _author_from_node(item)
                if author:
                    authors.append(author)
                continue
            ref = item.get("@id")
            if not ref:
                continue
            node = crate.node(ref) if crate is not None else None
            if node:
                author = _author_from_node(node)
                if author:
                    authors.append(author)
                    continue
            orcid = extract_orcid(ref)
            if orcid:
                # A bare ORCID reference we cannot resolve is still worth keeping.
                authors.append(Author(name=orcid, orcid=orcid))

    if authors_csv:
        for author in authors:
            details = authors_csv.get(author.name.strip().lower())
            if not details:
                continue
            author.affiliation = author.affiliation or details.get("affiliation") or None
            author.orcid = author.orcid or extract_orcid(details.get("orcid"))

    # Preserve order, drop duplicates by name.
    seen = set()
    unique = []
    for author in authors:
        key = author.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(author)
    return unique


def load_authors_csv(path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    """Read a name/affiliation/orcid CSV, keyed by lowercased name.

    Ported from fairscape-cli's publish_tools._load_authors_info.
    """
    if not path:
        return {}
    info: Dict[str, Dict[str, str]] = {}
    with Path(path).open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("name") or "").strip().lower()
            if name:
                info[name] = {
                    "affiliation": (row.get("affiliation") or "").strip(),
                    "orcid": (row.get("orcid") or "").strip(),
                }
    return info


def normalize_keywords(value: Any) -> List[str]:
    keywords: List[str] = []
    for item in as_list(value):
        if isinstance(item, str):
            keywords.extend(k.strip() for k in item.split(",") if k.strip())
        elif isinstance(item, dict):
            name = item.get("name") or item.get("@id")
            if isinstance(name, str) and name.strip():
                keywords.append(name.strip())
    seen = set()
    unique = []
    for keyword in keywords:
        if keyword.lower() in seen:
            continue
        seen.add(keyword.lower())
        unique.append(keyword)
    return unique


def normalize_date(value: Any, default_today: bool = True) -> Optional[str]:
    """Coerce to YYYY-MM-DD. Accepts ISO 8601, MM/DD/YYYY, and a bare year."""
    fallback = date.today().isoformat() if default_today else None
    if not isinstance(value, str) or not value.strip():
        return fallback

    text = value.strip()
    for parser in (
        lambda t: datetime.fromisoformat(t.split("T")[0].replace("Z", "")),
        lambda t: datetime.strptime(t, "%m/%d/%Y"),
        lambda t: datetime.strptime(t, "%d-%m-%Y"),
        lambda t: datetime.strptime(t, "%Y"),
    ):
        try:
            return parser(text).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    return fallback


def publication_year(value: Any) -> int:
    normalized = normalize_date(value)
    if normalized:
        try:
            return int(normalized[:4])
        except ValueError:
            pass
    return date.today().year


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get("name"))
    if isinstance(value, list):
        for item in value:
            text = _text(item)
            if text:
                return text
    return None


def build_payload(
    crate: Crate,
    files: Sequence[LocalFile],
    remote_entities: Sequence[RemoteEntity],
    authors_csv: Optional[Dict[str, Dict[str, str]]] = None,
    contact_name: Optional[str] = None,
    contact_email: Optional[str] = None,
) -> CratePayload:
    """Flatten a crate's root entity into the shape every target mapping reads."""
    root = crate.root_entity
    authors = normalize_authors(root, crate=crate, authors_csv=authors_csv)

    resolved_contact_name = (
        contact_name
        or _text(root.get("principalInvestigator"))
        or _text(root.get("contactName"))
        or (authors[0].name if authors else None)
    )
    resolved_contact_email = (
        contact_email
        or _text(root.get("contactEmail"))
        or _text((root.get("contactPoint") or {}).get("email") if isinstance(root.get("contactPoint"), dict) else None)
    )

    return CratePayload(
        crate_root=crate.root,
        metadata_path=crate.metadata_path,
        guid=str(root.get("@id", "")),
        name=_text(root.get("name")) or "Untitled RO-Crate",
        description=_text(root.get("description")) or "",
        keywords=normalize_keywords(root.get("keywords")),
        version=_text(root.get("version")),
        license_url=root.get("license") if isinstance(root.get("license"), str) else _text(root.get("license")),
        date_published=normalize_date(root.get("datePublished") or root.get("dateCreated")),
        date_created=normalize_date(root.get("dateCreated") or root.get("datePublished")),
        authors=authors,
        contact_name=resolved_contact_name,
        contact_email=resolved_contact_email,
        publisher=_text(root.get("publisher")),
        associated_publication=_text(root.get("associatedPublication")),
        url=_text(root.get("url")),
        root=root,
        files=list(files),
        remote_entities=list(remote_entities),
    )

"""CratePayload -> Zenodo legacy deposition metadata.

Zenodo runs on InvenioRDM now, but the legacy deposit API is still supported and
is the one that hands back a stable ``prereserve_doi`` at draft time, so that is
what this targets. Files go through the newer bucket endpoint regardless.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional, Sequence

from fairscape_publish.mapping import licenses
from fairscape_publish.models import CratePayload, Issue, Severity

#: Zenodo caps a record at 100 files and 50 GB per file.
MAX_FILES = 100
MAX_FILE_BYTES = 50 * 1024**3


def _description_html(text: str) -> str:
    """Zenodo renders description as HTML; escape, then keep paragraph breaks."""
    escaped = html.escape(text or "", quote=False).strip()
    if not escaped:
        return ""
    paragraphs = [p.strip().replace("\n", "<br>") for p in escaped.split("\n\n") if p.strip()]
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def build_metadata(
    payload: CratePayload,
    communities: Optional[Sequence[str]] = None,
    upload_type: str = "dataset",
    access_right: str = "open",
    license_id: Optional[str] = None,
) -> Dict[str, Any]:
    license_info = licenses.resolve(payload.license_url)

    creators: List[Dict[str, Any]] = []
    for author in payload.authors:
        creator: Dict[str, Any] = {"name": author.name}
        if author.affiliation:
            creator["affiliation"] = author.affiliation
        if author.orcid:
            creator["orcid"] = author.orcid
        creators.append(creator)
    if not creators:
        creators = [{"name": "Unknown"}]

    metadata: Dict[str, Any] = {
        "upload_type": upload_type,
        "title": payload.name,
        "description": _description_html(payload.description),
        "creators": creators,
        "access_right": access_right,
        "publication_date": payload.date_published,
    }

    if access_right in ("open", "embargoed"):
        metadata["license"] = license_id or license_info.zenodo_id
    if payload.keywords:
        metadata["keywords"] = payload.keywords
    if payload.version:
        metadata["version"] = payload.version

    related: List[Dict[str, str]] = []
    if payload.guid.startswith("ark:"):
        related.append(
            {"identifier": payload.guid, "relation": "isIdenticalTo", "resource_type": "dataset"}
        )
    if payload.associated_publication:
        doi = _extract_doi(payload.associated_publication)
        if doi:
            related.append({"identifier": doi, "relation": "isSupplementTo"})
    if related:
        metadata["related_identifiers"] = related

    if communities:
        metadata["communities"] = [{"identifier": c} for c in communities]

    if payload.contact_name:
        metadata["contributors"] = [
            {"name": payload.contact_name, "type": "ContactPerson"}
        ]

    return {"metadata": {k: v for k, v in metadata.items() if v not in (None, "", [])}}


def _extract_doi(text: str) -> Optional[str]:
    import re

    match = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>,;]+)", text or "")
    return match.group(1).rstrip(".") if match else None


def preflight(payload: CratePayload, file_count: int = 0, largest_file: int = 0) -> List[Issue]:
    issues: List[Issue] = []
    if not payload.name:
        issues.append(Issue(severity=Severity.ERROR, field="title", message="Zenodo requires a title."))
    if not payload.description:
        issues.append(
            Issue(severity=Severity.ERROR, field="description", message="Zenodo requires a description.")
        )
    if not payload.authors:
        issues.append(
            Issue(severity=Severity.WARNING, field="creators", message="No authors found; will deposit as 'Unknown'.")
        )
    if payload.license_url and licenses.lookup(payload.license_url) is None:
        issues.append(
            Issue(
                severity=Severity.WARNING,
                field="license",
                message=f"Unrecognized license '{payload.license_url}'; defaulting to {licenses.DEFAULT_LICENSE.zenodo_id}.",
            )
        )
    if file_count > MAX_FILES:
        issues.append(
            Issue(
                severity=Severity.ERROR,
                field="files",
                message=f"{file_count} files exceeds Zenodo's limit of {MAX_FILES} per record. Use --layout zip.",
            )
        )
    if largest_file > MAX_FILE_BYTES:
        issues.append(
            Issue(
                severity=Severity.ERROR,
                field="files",
                message=f"A file exceeds Zenodo's 50 GB per-file limit ({largest_file} bytes).",
            )
        )
    return issues

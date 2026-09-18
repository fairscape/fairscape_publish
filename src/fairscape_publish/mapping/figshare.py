"""CratePayload -> Figshare article JSON.

Figshare needs six fields before an article can be published: title, authors,
categories, keywords, description and license. Categories are numeric ids that
vary per instance, so they have to be supplied explicitly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from fairscape_publish.mapping import licenses
from fairscape_publish.models import CratePayload, Issue, Severity

DEFAULT_DEFINED_TYPE = "dataset"


def build_metadata(
    payload: CratePayload,
    categories: Optional[Sequence[int]] = None,
    license_id: Optional[int] = None,
    defined_type: str = DEFAULT_DEFINED_TYPE,
) -> Dict[str, Any]:
    license_info = licenses.resolve(payload.license_url)
    resolved_license = license_id if license_id is not None else license_info.figshare_id

    authors: List[Dict[str, str]] = []
    for author in payload.authors:
        entry = {"name": author.name}
        if author.orcid:
            entry["orcid_id"] = author.orcid
        authors.append(entry)

    article: Dict[str, Any] = {
        "title": payload.name,
        "description": payload.description,
        "defined_type": defined_type,
    }
    if authors:
        article["authors"] = authors
    if payload.keywords:
        article["keywords"] = payload.keywords
    if categories:
        article["categories"] = list(categories)
    if resolved_license is not None:
        article["license"] = resolved_license
    if payload.associated_publication:
        article["references"] = [payload.associated_publication]

    return article


def preflight(
    payload: CratePayload,
    categories: Optional[Sequence[int]] = None,
    license_id: Optional[int] = None,
    will_publish: bool = False,
) -> List[Issue]:
    issues: List[Issue] = []
    severity = Severity.ERROR if will_publish else Severity.WARNING

    if not payload.name:
        issues.append(Issue(severity=Severity.ERROR, field="title", message="Figshare requires a title."))
    if not payload.description:
        issues.append(
            Issue(severity=severity, field="description", message="Figshare requires a description to publish.")
        )
    if not payload.authors:
        issues.append(
            Issue(severity=severity, field="authors", message="Figshare requires at least one author to publish.")
        )
    if not payload.keywords:
        issues.append(
            Issue(severity=severity, field="keywords", message="Figshare requires at least one keyword to publish.")
        )
    if not categories:
        issues.append(
            Issue(
                severity=severity,
                field="categories",
                message="Figshare requires at least one category id to publish. Pass --category (ids are instance-specific; see GET /v2/categories).",
            )
        )
    resolved_license = license_id if license_id is not None else licenses.resolve(payload.license_url).figshare_id
    if resolved_license is None:
        issues.append(
            Issue(
                severity=severity,
                field="license",
                message=f"No Figshare license id maps to '{payload.license_url}'. Pass --license-id.",
            )
        )
    return issues

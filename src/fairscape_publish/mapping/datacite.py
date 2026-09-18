"""CratePayload -> DataCite DOI attributes (schema kernel-4).

Ported from fairscape-cli's DataCitePublisher._transform_metadata, re-pointed at
the shared author/license normalization.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fairscape_publish.mapping import licenses
from fairscape_publish.mapping.common import publication_year
from fairscape_publish.models import CratePayload, Issue, Severity


def build_metadata(
    payload: CratePayload,
    prefix: str,
    event: Optional[str] = None,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    creators: List[Dict[str, Any]] = []
    for author in payload.authors:
        creator: Dict[str, Any] = {"name": author.name, "nameType": "Personal"}
        if author.affiliation:
            creator["affiliation"] = [{"name": author.affiliation}]
        if author.orcid:
            creator["nameIdentifiers"] = [
                {
                    "nameIdentifier": f"https://orcid.org/{author.orcid}",
                    "nameIdentifierScheme": "ORCID",
                    "schemeUri": "https://orcid.org",
                }
            ]
        creators.append(creator)
    if not creators:
        creators = [{"name": "Unknown"}]

    license_info = licenses.lookup(payload.license_url)

    attributes: Dict[str, Any] = {
        "prefix": prefix,
        "titles": [{"title": payload.name}],
        "creators": creators,
        "publisher": payload.publisher or "FAIRSCAPE",
        "publicationYear": publication_year(payload.date_published),
        "types": {"resourceTypeGeneral": "Dataset", "resourceType": "RO-Crate"},
        "url": url or payload.url or payload.guid,
        "schemaVersion": "http://datacite.org/schema/kernel-4",
        "language": "en",
    }

    if payload.description:
        attributes["descriptions"] = [
            {"description": payload.description, "descriptionType": "Abstract"}
        ]
    if payload.keywords:
        attributes["subjects"] = [{"subject": kw} for kw in payload.keywords]
    if payload.date_published:
        attributes["dates"] = [{"date": payload.date_published, "dateType": "Issued"}]
    if payload.version:
        attributes["version"] = payload.version
    if license_info:
        attributes["rightsList"] = [license_info.datacite_rights]
    if payload.guid and payload.guid.startswith("ark:"):
        attributes["alternateIdentifiers"] = [
            {"alternateIdentifier": payload.guid, "alternateIdentifierType": "ARK"}
        ]
    if event:
        attributes["event"] = event

    return {"data": {"type": "dois", "attributes": attributes}}


def preflight(payload: CratePayload, event: str = "register") -> List[Issue]:
    issues: List[Issue] = []
    if not payload.name:
        issues.append(Issue(severity=Severity.ERROR, field="titles", message="DataCite requires a title."))
    if not payload.authors:
        issues.append(
            Issue(severity=Severity.WARNING, field="creators", message="No authors found; will register as 'Unknown'.")
        )
    if not (payload.url or payload.guid):
        issues.append(
            Issue(severity=Severity.ERROR, field="url", message="DataCite requires a landing-page URL.")
        )
    if event in ("publish", "hide") and not payload.publisher:
        issues.append(
            Issue(
                severity=Severity.WARNING,
                field="publisher",
                message="No `publisher` on the crate root; will register as 'FAIRSCAPE'.",
            )
        )
    return issues

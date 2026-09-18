"""CratePayload -> Dataverse datasetVersion JSON (citation metadata block)."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from fairscape_publish.mapping import licenses
from fairscape_publish.models import CratePayload, Issue, Severity

#: Dataverse's `subject` is a controlled vocabulary; these are the standard terms.
DATAVERSE_SUBJECTS = [
    "Agricultural Sciences",
    "Arts and Humanities",
    "Astronomy and Astrophysics",
    "Business and Management",
    "Chemistry",
    "Computer and Information Science",
    "Earth and Environmental Sciences",
    "Engineering",
    "Law",
    "Mathematical Sciences",
    "Medicine, Health and Life Sciences",
    "Physics",
    "Social Sciences",
    "Other",
]

DEFAULT_SUBJECTS = ["Medicine, Health and Life Sciences"]


def _primitive(type_name: str, value: Any, multiple: bool = False) -> Dict[str, Any]:
    return {"typeName": type_name, "multiple": multiple, "typeClass": "primitive", "value": value}


def _compound(type_name: str, value: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"typeName": type_name, "multiple": True, "typeClass": "compound", "value": value}


def _vocab(type_name: str, value: List[str]) -> Dict[str, Any]:
    return {"typeName": type_name, "multiple": True, "typeClass": "controlledVocabulary", "value": value}


def build_metadata(
    payload: CratePayload,
    subjects: Optional[Sequence[str]] = None,
    license_override: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    license_info = licenses.resolve(payload.license_url)

    author_entries = []
    for author in payload.authors:
        entry = {
            "authorName": _primitive("authorName", author.name),
            "authorAffiliation": _primitive("authorAffiliation", author.affiliation or ""),
        }
        if author.orcid:
            entry["authorIdentifierScheme"] = {
                "typeName": "authorIdentifierScheme",
                "multiple": False,
                "typeClass": "controlledVocabulary",
                "value": "ORCID",
            }
            entry["authorIdentifier"] = _primitive("authorIdentifier", author.orcid)
        author_entries.append(entry)

    if not author_entries:
        author_entries.append(
            {
                "authorName": _primitive("authorName", "Unknown"),
                "authorAffiliation": _primitive("authorAffiliation", ""),
            }
        )

    fields: List[Dict[str, Any]] = [
        _primitive("title", payload.name),
        _compound("author", author_entries),
        _compound(
            "datasetContact",
            [
                {
                    "datasetContactName": _primitive("datasetContactName", payload.contact_name or "Unknown"),
                    "datasetContactEmail": _primitive("datasetContactEmail", payload.contact_email or ""),
                }
            ],
        ),
        _compound(
            "dsDescription",
            [{"dsDescriptionValue": _primitive("dsDescriptionValue", payload.description)}],
        ),
        _vocab("subject", list(subjects) if subjects else list(DEFAULT_SUBJECTS)),
    ]

    if payload.keywords:
        fields.append(
            _compound(
                "keyword",
                [{"keywordValue": _primitive("keywordValue", kw)} for kw in payload.keywords],
            )
        )

    notes = f"Published from a FAIRSCAPE RO-Crate. Crate identifier: {payload.guid}"
    fields.append(_primitive("notesText", notes))

    # "Data Creation Date" in the UI. Optional in stock Dataverse, but instances
    # such as UVA's LibraData mark it required, so always send it when we have one.
    production_date = payload.date_created or payload.date_published
    if production_date:
        fields.append(_primitive("productionDate", production_date))
    if payload.date_published:
        fields.append(_primitive("distributionDate", payload.date_published))
    fields.append(_primitive("dateOfDeposit", date.today().isoformat()))

    if payload.publisher:
        fields.append(
            _compound(
                "producer",
                [{"producerName": _primitive("producerName", payload.publisher)}],
            )
        )

    if payload.associated_publication:
        fields.append(
            _compound(
                "publication",
                [{"publicationCitation": _primitive("publicationCitation", payload.associated_publication)}],
            )
        )

    # A Dataverse instance only accepts licenses configured on it, and it matches
    # the URI string exactly (http vs https included), so an instance-supplied
    # pair always wins over our table.
    license_block = license_override or {
        "name": license_info.dataverse["name"],
        "uri": license_info.dataverse["uri"],
    }

    return {
        "datasetVersion": {
            "license": license_block,
            "metadataBlocks": {
                "citation": {"displayName": "Citation Metadata", "fields": fields},
            },
        }
    }


def preflight(payload: CratePayload, subjects: Optional[Sequence[str]] = None) -> List[Issue]:
    issues: List[Issue] = []
    if not payload.name:
        issues.append(Issue(severity=Severity.ERROR, field="name", message="Dataverse requires a title."))
    if not payload.description:
        issues.append(
            Issue(severity=Severity.ERROR, field="description", message="Dataverse requires a description.")
        )
    if not payload.contact_email:
        issues.append(
            Issue(
                severity=Severity.ERROR,
                field="contactEmail",
                message="Dataverse requires a contact email. Set `contactEmail` on the crate root or pass --contact-email.",
            )
        )
    if not payload.authors:
        issues.append(
            Issue(severity=Severity.WARNING, field="author", message="No authors found; will deposit as 'Unknown'.")
        )
    if not (payload.date_created or payload.date_published):
        issues.append(
            Issue(
                severity=Severity.WARNING,
                field="productionDate",
                message="No dateCreated or datePublished on the crate; some instances require Data Creation Date.",
            )
        )
    for subject in subjects or DEFAULT_SUBJECTS:
        if subject not in DATAVERSE_SUBJECTS:
            issues.append(
                Issue(
                    severity=Severity.ERROR,
                    field="subject",
                    message=f"'{subject}' is not in the Dataverse subject vocabulary. Valid: {', '.join(DATAVERSE_SUBJECTS)}",
                )
            )
    if payload.license_url and licenses.lookup(payload.license_url) is None:
        issues.append(
            Issue(
                severity=Severity.WARNING,
                field="license",
                message=f"Unrecognized license '{payload.license_url}'; defaulting to {licenses.DEFAULT_LICENSE.name}.",
            )
        )
    return issues

"""Canned responses so --dry-run can walk a full create -> upload -> publish path.

These mirror the documented response shapes of each API. They exist to exercise
our own parsing code offline; they are not a simulator of the remote service.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Tuple

from fairscape_publish.transport import DryRunSession

Responder = Tuple[Callable[[str, str], bool], Dict]


def _url_contains(fragment: str, method: str = "") -> Callable[[str, str], bool]:
    def predicate(request_method: str, url: str) -> bool:
        if method and request_method != method:
            return False
        return fragment in url

    return predicate


def dataverse_fixtures(base_url: str) -> List[Responder]:
    return [
        (
            _url_contains("/datasets", "POST"),
            {"status": "OK", "data": {"id": 1234, "persistentId": "doi:10.5072/FK2/DRYRUN"}},
        ),
        (
            _url_contains("/add", "POST"),
            {"status": "OK", "data": {"files": [{"dataFile": {"id": 9999, "md5": "0" * 32}}]}},
        ),
    ]


def zenodo_fixtures(base_url: str) -> List[Responder]:
    deposition_id = 1234567
    return [
        (
            _url_contains("/deposit/depositions", "POST"),
            {
                "id": deposition_id,
                "metadata": {"prereserve_doi": {"doi": f"10.5281/zenodo.{deposition_id}"}},
                "links": {
                    "bucket": f"{base_url.replace('/api', '')}/api/files/00000000-0000-0000-0000-000000000000",
                    "html": f"{base_url.replace('/api', '')}/deposit/{deposition_id}",
                    "publish": f"{base_url}/deposit/depositions/{deposition_id}/actions/publish",
                },
            },
        ),
        (
            _url_contains("/api/files/", "PUT"),
            {"key": "crate.zip", "checksum": "md5:" + "0" * 32, "file_id": "dry-run-file"},
        ),
    ]


def figshare_fixtures(base_url: str) -> List[Responder]:
    article_id = 987654
    file_id = 1
    file_location = f"{base_url}/account/articles/{article_id}/files/{file_id}"

    def is_initiate_upload(method: str, url: str) -> bool:
        return method == "POST" and url.rstrip("/").endswith("/files")

    def is_complete_upload(method: str, url: str) -> bool:
        return method == "POST" and re.search(r"/files/\d+$", url.rstrip("/")) is not None

    return [
        (_url_contains("/reserve_doi", "POST"), {"doi": f"10.6084/m9.figshare.{article_id}"}),
        (_url_contains("/publish", "POST"), {"location": f"https://figshare.com/articles/dataset/{article_id}"}),
        (is_complete_upload, {"location": file_location}),
        (is_initiate_upload, {"location": file_location}),
        (
            _url_contains("/account/articles", "POST"),
            {"entity_id": article_id, "location": f"{base_url}/account/articles/{article_id}"},
        ),
        (
            _url_contains("fup.figshare.com", "GET"),
            {"parts": [{"partNo": 1, "startOffset": 0, "endOffset": 0, "status": "PENDING"}]},
        ),
        (
            _url_contains("/files", "GET"),
            {
                "id": file_id,
                "name": "crate.zip",
                "upload_url": "https://fup.figshare.com/upload/dry-run-token",
                "status": "created",
            },
        ),
    ]


def datacite_fixtures(base_url: str) -> List[Responder]:
    return [
        (
            _url_contains("/dois", "POST"),
            {"data": {"id": "10.5072/dry-run", "type": "dois", "attributes": {"doi": "10.5072/dry-run"}}},
        ),
    ]


FIXTURES = {
    "dataverse": dataverse_fixtures,
    "zenodo": zenodo_fixtures,
    "figshare": figshare_fixtures,
    "datacite": datacite_fixtures,
}


def install(session: DryRunSession, target_name: str, base_url: str) -> None:
    builder = FIXTURES.get(target_name)
    if not builder:
        return
    for predicate, payload in builder(base_url):
        session.add_responder(predicate, payload)

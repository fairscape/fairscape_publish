"""Figshare.

Uploads are a five-step dance: declare the file, fetch its upload endpoint, read
the part layout, PUT each part, then confirm. Ported from fairscape_server's
FigsharePublisher but reading each part off disk rather than holding the whole
file in memory, so a multi-GB crate zip does not need multi-GB of RAM.

Figshare runs a stage environment at api.figsh.com with its own credentials.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from fairscape_publish.mapping import figshare as mapping
from fairscape_publish.mapping import licenses
from fairscape_publish.models import (
    CratePayload,
    Deposit,
    FileUpload,
    Issue,
    Layout,
    Severity,
    UploadedFile,
)
from fairscape_publish.targets.base import Target, TargetError
from fairscape_publish.transport import REFERENCE_READ_RETRIES, Session, TransportError

BASE_URL = "https://api.figshare.com/v2"
#: Figshare's stage environment. Credentials are separate from production and are
#: issued per institution by Figshare support; a production token will not work.
SANDBOX_URL = "https://api.figsh.com/v2"

#: Figshare asks for no more than one request per second.
MIN_REQUEST_INTERVAL = 1.0


class FigshareTarget(Target):
    name = "figshare"
    default_layout = Layout.ZIP
    supported_layouts = (Layout.ZIP, Layout.FLAT)

    def __init__(
        self,
        session: Session,
        base_url: str = BASE_URL,
        categories: Optional[Sequence[int]] = None,
        license_id: Optional[int] = None,
        defined_type: str = "dataset",
        reserve_doi: bool = True,
        will_publish: bool = False,
    ):
        super().__init__(session, base_url)
        self.categories = list(categories) if categories else None
        self.license_id = license_id
        self.defined_type = defined_type
        self.reserve_doi = reserve_doi
        self.will_publish = will_publish
        #: License id resolved from the instance's own list, when we could read it.
        self.resolved_license_id: Optional[int] = None

    def preflight(self, payload: CratePayload, uploads: Sequence[FileUpload]) -> List[Issue]:
        return mapping.preflight(
            payload,
            categories=self.categories,
            license_id=self.license_id,
            will_publish=self.will_publish,
        )

    def build_metadata(self, payload: CratePayload) -> Dict[str, Any]:
        return mapping.build_metadata(
            payload,
            categories=self.categories,
            license_id=self.resolved_license_id or self.license_id,
            defined_type=self.defined_type,
        )

    def _public_list(self, path: str) -> List[Dict[str, Any]]:
        """Read one of Figshare's unauthenticated reference lists."""
        try:
            response = self.session.get(f"{self.base_url}/{path}", expected=(200,), retries=REFERENCE_READ_RETRIES)
        except TransportError:
            return []
        data = response.json()
        return data if isinstance(data, list) else []

    def reconcile_license(self, payload: CratePayload) -> Optional[str]:
        """Resolve the license to an id this Figshare instance actually publishes.

        License ids are per-instance: production numbers CC BY 4.0 as 1, stage
        carries a second CC BY 4.0 at 50, and institutional instances differ
        again. Sending a stale integer silently deposits under the wrong license,
        so the live list at /v2/licenses wins over any built-in table.
        """
        available = self._public_list("licenses")
        if not available:
            return None

        if self.license_id is not None:
            if any(item.get("value") == self.license_id for item in available):
                return None
            raise TargetError(
                f"License id {self.license_id} is not offered by {self.base_url}. Available: "
                + ", ".join(f"{i.get('value')}={i.get('name')}" for i in available)
            )

        match = licenses.match_available(payload.license_url, available, uri_key="url")
        if match is None:
            return None  # preflight reports the gap; not fatal for a draft
        self.resolved_license_id = match.get("value")
        return (
            f"Crate license {payload.license_url!r} matched this instance's "
            f"{match.get('name')!r} (id {match.get('value')})."
        )

    def check_categories(self, payload: CratePayload) -> List[Issue]:
        """Reject a category id this instance does not have, before uploading."""
        if not self.categories:
            return []
        available = self._public_list("categories")
        if not available:
            return []
        known = {item.get("id") for item in available}
        return [
            Issue(
                severity=Severity.ERROR,
                field="categories",
                message=f"Category id {category} does not exist on {self.base_url}. See GET {self.base_url}/categories.",
            )
            for category in self.categories
            if category not in known
        ]

    def create_deposit(self, payload: CratePayload) -> Deposit:
        response = self.session.post(
            f"{self.base_url}/account/articles",
            headers={"Content-Type": "application/json"},
            json_body=self.build_metadata(payload),
            expected=(201,),
        )
        data = response.json() or {}
        article_id = self._article_id(data)

        doi = None
        if self.reserve_doi:
            doi_response = self.session.post(
                f"{self.base_url}/account/articles/{article_id}/reserve_doi",
                expected=(200, 201),
            )
            doi = (doi_response.json() or {}).get("doi")

        return Deposit(
            target=self.name,
            deposit_id=str(article_id),
            base_url=self.base_url,
            doi=doi,
            landing_url=f"https://figshare.com/account/articles/{article_id}",
            doi_registered=False,
            state="draft",
            raw=data,
        )

    def _article_id(self, data: Dict[str, Any]) -> str:
        if data.get("entity_id"):
            return str(data["entity_id"])
        location = data.get("location")
        if isinstance(location, str) and location.strip("/"):
            return location.rstrip("/").rsplit("/", 1)[-1]
        if self.session.dry_run:
            return self.dry_run_id("article")
        raise TargetError(f"Figshare did not return an article id: {data}")

    def upload_file(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        checksum = self.checksum(upload)

        # 1. Declare the file.
        initiate = self.session.post(
            f"{self.base_url}/account/articles/{deposit.deposit_id}/files",
            headers={"Content-Type": "application/json"},
            json_body={"name": upload.key, "size": upload.size, "md5": checksum},
            expected=(201,),
        )
        location = (initiate.json() or {}).get("location")
        if not location:
            if not self.session.dry_run:
                raise TargetError(f"Figshare returned no file location for {upload.key}")
            location = f"{self.base_url}/account/articles/{deposit.deposit_id}/files/0"

        # 2. Resolve the file's upload endpoint.
        detail = (self.session.get(location, expected=(200,)).json()) or {}
        file_id = str(detail.get("id") or location.rstrip("/").rsplit("/", 1)[-1])
        upload_url = detail.get("upload_url")

        # 3. Read the part layout, then 4. send each part.
        if upload_url:
            parts = ((self.session.get(upload_url, expected=(200,)).json()) or {}).get("parts") or []
            self._upload_parts(upload_url, upload, parts)
        elif not self.session.dry_run:
            raise TargetError(f"Figshare returned no upload_url for {upload.key}")

        # 5. Confirm.
        self.session.post(
            f"{self.base_url}/account/articles/{deposit.deposit_id}/files/{file_id}",
            expected=(200, 201, 202),
        )

        return UploadedFile(
            key=upload.key,
            relpath=upload.relpath,
            size=upload.size,
            md5=checksum,
            remote_id=file_id,
        )

    def _upload_parts(self, upload_url: str, upload: FileUpload, parts: Sequence[Dict[str, Any]]) -> None:
        if not parts:
            return
        with self.open_upload(upload) as handle:
            for part in parts:
                start = int(part.get("startOffset", 0))
                end = int(part.get("endOffset", 0))
                handle.seek(start)
                chunk = handle.read(end - start + 1)
                self.session.put(
                    f"{upload_url.rstrip('/')}/{part.get('partNo')}",
                    data=chunk,
                    expected=(200, 201),
                )

    def publish(self, deposit: Deposit) -> Deposit:
        response = self.session.post(
            f"{self.base_url}/account/articles/{deposit.deposit_id}/publish",
            expected=(200, 201),
        )
        data = response.json() or {}
        deposit.state = "published"
        deposit.doi = data.get("doi") or deposit.doi
        deposit.doi_registered = True
        deposit.landing_url = data.get("location") or f"https://figshare.com/articles/dataset/{deposit.deposit_id}"
        return deposit

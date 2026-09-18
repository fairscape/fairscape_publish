"""Zenodo.

Records are created through the legacy deposit API (it returns a stable
``prereserve_doi`` on the draft), and files go through the newer bucket endpoint,
which streams and lifts the old 100 MB per-file ceiling to 50 GB.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from fairscape_publish.mapping import licenses
from fairscape_publish.mapping import zenodo as mapping
from fairscape_publish.models import CratePayload, Deposit, FileUpload, Issue, Layout, UploadedFile
from fairscape_publish.targets.base import Target, TargetError
from fairscape_publish.transport import REFERENCE_READ_RETRIES, Session, TransportError

PRODUCTION_URL = "https://zenodo.org/api"
SANDBOX_URL = "https://sandbox.zenodo.org/api"


class ZenodoTarget(Target):
    name = "zenodo"
    default_layout = Layout.ZIP
    supported_layouts = (Layout.ZIP, Layout.FLAT)

    def __init__(
        self,
        session: Session,
        base_url: str = PRODUCTION_URL,
        communities: Optional[Sequence[str]] = None,
        upload_type: str = "dataset",
        access_right: str = "open",
        license: Optional[str] = None,
    ):
        super().__init__(session, base_url)
        self.communities = list(communities) if communities else None
        self.upload_type = upload_type
        self.access_right = access_right
        self.requested_license = license
        #: SPDX-lowercase id confirmed to exist in this instance's vocabulary.
        self.license_override: Optional[str] = None

    def preflight(self, payload: CratePayload, uploads: Sequence[FileUpload]) -> List[Issue]:
        largest = max((u.size for u in uploads), default=0)
        return mapping.preflight(payload, file_count=len(uploads), largest_file=largest)

    def build_metadata(self, payload: CratePayload) -> Dict[str, Any]:
        return mapping.build_metadata(
            payload,
            communities=self.communities,
            upload_type=self.upload_type,
            access_right=self.access_right,
            license_id=self.license_override,
        )

    def reconcile_license(self, payload: CratePayload) -> Optional[str]:
        """Confirm the license id exists in this instance's vocabulary.

        Since the InvenioRDM migration Zenodo validates `license` against
        /api/vocabularies/licenses (SPDX ids, lowercased). An id that is not in
        it is rejected at create time, so it is worth one cheap GET first.
        """
        wanted = self.requested_license or licenses.resolve(payload.license_url).zenodo_id
        try:
            self.session.get(f"{self.base_url}/vocabularies/licenses/{wanted}", expected=(200,), retries=REFERENCE_READ_RETRIES)
        except TransportError as exc:
            if getattr(exc, "status_code", None) != 404:
                return None  # vocabulary unreachable; let the create call decide
            raise TargetError(
                f"'{wanted}' is not a license id in {self.base_url}'s vocabulary. "
                f"Search it with GET {self.base_url}/vocabularies/licenses?q=<term>, "
                "then pass the id with --license. The crate is left as it is."
            ) from exc

        self.license_override = wanted
        if self.requested_license:
            return f"Using --license {wanted!r} instead of the crate's {payload.license_url or 'none'}."
        return None

    def create_deposit(self, payload: CratePayload) -> Deposit:
        url = f"{self.base_url}/deposit/depositions"
        response = self.session.post(
            url,
            headers={"Content-Type": "application/json"},
            json_body=self.build_metadata(payload),
            expected=(201,),
        )
        data = response.json() or {}
        links = data.get("links", {}) or {}
        prereserve = (data.get("metadata", {}) or {}).get("prereserve_doi") or {}

        return Deposit(
            target=self.name,
            deposit_id=str(data.get("id") or self.dry_run_id("deposition")),
            base_url=self.base_url,
            doi=prereserve.get("doi") or data.get("doi"),
            landing_url=links.get("html") or links.get("record_html"),
            bucket_url=links.get("bucket"),
            doi_registered=False,
            state="draft",
            raw=data,
        )

    def upload_file(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        if deposit.bucket_url:
            return self._upload_via_bucket(deposit, upload)
        return self._upload_via_legacy(deposit, upload)

    def _upload_via_bucket(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        url = f"{deposit.bucket_url.rstrip('/')}/{upload.key}"
        with self.open_upload(upload) as handle:
            response = self.session.put(
                url,
                headers={"Content-Type": "application/octet-stream"},
                data=handle,
                expected=(200, 201),
            )
        data = response.json() or {}
        return UploadedFile(
            key=upload.key,
            relpath=upload.relpath,
            size=upload.size,
            md5=(data.get("checksum") or "").replace("md5:", "") or None,
            remote_id=data.get("file_id") or data.get("version_id"),
        )

    def _upload_via_legacy(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        """Only reached on a Zenodo instance that returns no bucket link. 100 MB cap."""
        url = f"{self.base_url}/deposit/depositions/{deposit.deposit_id}/files"
        with self.open_upload(upload) as handle:
            response = self.session.post(
                url,
                data={"name": upload.key},
                files={"file": (upload.key, handle, self.guess_mimetype(upload.abspath))},
                expected=(201,),
            )
        data = response.json() or {}
        return UploadedFile(
            key=upload.key,
            relpath=upload.relpath,
            size=upload.size,
            md5=data.get("checksum"),
            remote_id=str(data.get("id")) if data.get("id") else None,
        )

    def publish(self, deposit: Deposit) -> Deposit:
        url = f"{self.base_url}/deposit/depositions/{deposit.deposit_id}/actions/publish"
        response = self.session.post(url, expected=(200, 202))
        data = response.json() or {}
        deposit.state = "published"
        deposit.doi = data.get("doi") or deposit.doi
        deposit.doi_registered = True
        deposit.landing_url = (data.get("links", {}) or {}).get("record_html") or deposit.landing_url
        return deposit

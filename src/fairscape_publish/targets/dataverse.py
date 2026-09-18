"""Dataverse native API.

Dataverse is the one target that understands directories, so `preserve` is the
default layout: each file's crate-relative directory becomes its
``directoryLabel`` and the crate tree survives the round trip.

Two ways to get bytes in. The native ``/add`` endpoint streams the file through
the Dataverse application server. Instances backed by S3 can also hand out a
presigned URL and take the bytes directly (``/uploadurls`` then ``/addFiles``),
which is what Dataverse recommends for large files -- and the only route that
works at all in front of a web application firewall, since the file body then
never passes through the WAF. It is detected rather than assumed: the instance
is asked once whether it will issue an upload URL, and the native path is used
when it will not.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from fairscape_publish.mapping import dataverse as mapping
from fairscape_publish.models import (
    CratePayload,
    Deposit,
    FileUpload,
    Issue,
    Layout,
    Severity,
    UploadedFile,
)
from fairscape_publish.mapping import licenses
from fairscape_publish.targets.base import Target, TargetError
from fairscape_publish.transport import REFERENCE_READ_RETRIES, Session, TransportError


class DataverseTarget(Target):
    name = "dataverse"
    default_layout = Layout.PRESERVE
    supported_layouts = (Layout.PRESERVE, Layout.FLAT, Layout.ZIP)

    def __init__(
        self,
        session: Session,
        base_url: str,
        collection: str,
        subjects: Optional[Sequence[str]] = None,
        tab_ingest: bool = False,
        publish_type: str = "major",
        license: Optional[str] = None,
        direct_upload: Optional[bool] = None,
    ):
        super().__init__(session, base_url)
        self.collection = collection
        self.subjects = list(subjects) if subjects else None
        self.tab_ingest = tab_ingest
        self.publish_type = publish_type
        #: Name or URI the caller wants, before reconciliation with the instance.
        self.requested_license = license
        #: The exact {name, uri} pair the instance accepts, once resolved.
        self.license_override: Optional[Dict[str, str]] = None
        #: None asks the instance; True/False is the caller overriding it.
        self.requested_direct_upload = direct_upload
        self._direct_upload: Optional[bool] = direct_upload

    def preflight(self, payload: CratePayload, uploads: Sequence[FileUpload]) -> List[Issue]:
        return mapping.preflight(payload, subjects=self.subjects)

    def build_metadata(self, payload: CratePayload) -> Dict[str, Any]:
        return mapping.build_metadata(
            payload, subjects=self.subjects, license_override=self.license_override
        )

    def instance_licenses(self) -> List[Dict[str, Any]]:
        """The licenses this instance actually accepts. Empty if it will not say."""
        try:
            response = self.session.get(f"{self.base_url}/api/licenses", expected=(200,), retries=REFERENCE_READ_RETRIES)
        except TransportError:
            return []
        data = (response.json() or {}).get("data")
        return [item for item in data if item.get("active", True)] if isinstance(data, list) else []

    def reconcile_license(self, payload: CratePayload) -> Optional[str]:
        """Pin the license to the exact pair this instance publishes.

        A Dataverse instance only accepts licenses configured on it, and it
        compares the URI string exactly - http vs https is enough to be rejected.
        Returns a note when the crate's own license had to be replaced, so the
        caller can say so out loud rather than quietly altering the metadata.
        """
        available = self.instance_licenses()
        if not available:
            return None

        wanted = self.requested_license or payload.license_url
        match = licenses.match_available(wanted, available)

        if match is None and self.requested_license:
            raise TargetError(
                f"{self.base_url} does not offer a license matching "
                f"{self.requested_license!r}. It accepts: {licenses.names_of(available)}."
            )
        if match is None:
            raise TargetError(
                f"The crate's license ({payload.license_url or 'none'}) is not offered by "
                f"{self.base_url}, which accepts: {licenses.names_of(available)}.\n"
                "Re-run with --license to choose one of those, or configure the license on the instance. "
                "The crate metadata is left as it is either way."
            )

        self.license_override = {"name": match["name"], "uri": match["uri"]}
        if self.requested_license:
            return f"Using --license {match['name']!r} instead of the crate's {payload.license_url or 'none'}."
        if licenses.normalize_license_url(match["uri"]) != licenses.normalize_license_url(payload.license_url):
            return f"Crate license {payload.license_url!r} matched this instance's {match['name']!r}."
        return None

    def required_citation_fields(self) -> List[Dict[str, Any]]:
        """Fields this instance marks required.

        Instances customize the citation block - UVA's LibraData makes
        `productionDate` required, which stock Dataverse does not - so the only
        reliable source is the instance itself.
        """
        try:
            response = self.session.get(f"{self.base_url}/api/metadatablocks/citation", expected=(200,), retries=REFERENCE_READ_RETRIES)
        except TransportError:
            return []
        fields = ((response.json() or {}).get("data") or {}).get("fields") or {}
        if not isinstance(fields, dict):
            return []
        return [
            {"name": name, "title": spec.get("title", name)}
            for name, spec in fields.items()
            if isinstance(spec, dict) and spec.get("isRequired")
        ]

    def check_required_fields(self, payload: CratePayload) -> List[Issue]:
        """Compare what the instance requires against what we are about to send."""
        required = self.required_citation_fields()
        if not required:
            return []

        document = self.build_metadata(payload)
        sending = {
            field.get("typeName")
            for field in document["datasetVersion"]["metadataBlocks"]["citation"]["fields"]
            if field.get("value") not in (None, "", [])
        }
        return [
            Issue(
                severity=Severity.ERROR,
                field=item["name"],
                message=f"{self.base_url} requires '{item['title']}' and the crate provides nothing for it.",
            )
            for item in required
            if item["name"] not in sending
        ]

    def create_deposit(self, payload: CratePayload) -> Deposit:
        url = f"{self.base_url}/api/dataverses/{self.collection}/datasets"
        response = self.session.post(
            url,
            headers={"Content-Type": "application/json"},
            json_body=self.build_metadata(payload),
            expected=(201,),
        )
        data = (response.json() or {}).get("data", {})
        persistent_id = data.get("persistentId") or self.dry_run_id("doi:10.5072/FK2")
        numeric_id = str(data.get("id") or self.dry_run_id("dataset"))

        return Deposit(
            target=self.name,
            deposit_id=numeric_id,
            base_url=self.base_url,
            persistent_id=persistent_id,
            doi=persistent_id[4:] if persistent_id.startswith("doi:") else None,
            landing_url=f"{self.base_url}/dataset.xhtml?persistentId={persistent_id}&version=DRAFT",
            doi_registered=False,
            state="draft",
            raw=data,
        )

    # -- uploads ----------------------------------------------------------

    def supports_direct_upload(self, deposit: Deposit) -> bool:
        """Will this instance hand out a presigned URL for this dataset?

        Asked once per run, with a nominal size, and cached. A dry run never
        asks -- there is nothing to upload to -- and reports the native path.
        """
        if self._direct_upload is not None:
            return self._direct_upload
        if self.session.dry_run:
            self._direct_upload = False
            return False
        try:
            response = self.session.get(
                f"{self.base_url}/api/datasets/{deposit.deposit_id}/uploadurls",
                params={"size": 1},
                expected=(200,),
                retries=REFERENCE_READ_RETRIES,
            )
            data = (response.json() or {}).get("data") or {}
            self._direct_upload = bool(data.get("url") or data.get("urls"))
        except TransportError:
            self._direct_upload = False
        return self._direct_upload

    def upload_file(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        if self.supports_direct_upload(deposit):
            try:
                return self._upload_direct(deposit, upload)
            except TransportError:
                if self.requested_direct_upload:
                    raise
                # Asked for it ourselves, so fall back rather than fail, but
                # only once: whatever went wrong will go wrong again.
                self._direct_upload = False
        return self._upload_native(deposit, upload)

    def _file_metadata(self, upload: FileUpload) -> Dict[str, Any]:
        json_data: Dict[str, Any] = {
            "restrict": "false",
            # Left off, Dataverse rewrites csv/tsv/dta into its own tabular format,
            # which would break the checksums the crate declares.
            "tabIngest": "true" if self.tab_ingest else "false",
        }
        if upload.directory_label:
            json_data["directoryLabel"] = upload.directory_label
        if upload.description:
            json_data["description"] = upload.description
        return json_data

    def _upload_native(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        persistent_id = deposit.persistent_id or deposit.deposit_id
        url = f"{self.base_url}/api/datasets/:persistentId/add"

        with self.open_upload(upload) as handle:
            response = self.session.post(
                url,
                params={"persistentId": persistent_id},
                files={
                    "file": (upload.key, handle, self.guess_mimetype(upload.abspath)),
                    "jsonData": (None, json.dumps(self._file_metadata(upload)), "application/json"),
                },
                expected=(200, 201),
            )

        payload = (response.json() or {}).get("data", {})
        return self._uploaded(upload, payload.get("files") or [])

    def _upload_direct(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        """PUT the bytes at the storage layer, then register them with Dataverse.

        The presigned URL is signed for `host` and `x-amz-tagging` only, and it
        belongs to the object store rather than to Dataverse, so the request
        carries neither the API token nor any header beyond the tag.
        """
        persistent_id = deposit.persistent_id or deposit.deposit_id

        response = self.session.get(
            f"{self.base_url}/api/datasets/{deposit.deposit_id}/uploadurls",
            params={"size": upload.size},
            expected=(200,),
        )
        ticket = (response.json() or {}).get("data") or {}
        put_url = ticket.get("url")
        storage_identifier = ticket.get("storageIdentifier")
        if not put_url or not storage_identifier:
            part_size = ticket.get("partSize")
            raise TargetError(
                f"{upload.key} is {upload.size} bytes, above this instance's "
                f"single-part upload limit of {part_size}. Multi-part direct "
                "upload is not implemented; re-run with --no-direct-upload to "
                "send it through the Dataverse application server instead."
            )

        with self.open_upload(upload) as handle:
            self.session.request(
                "PUT",
                put_url,
                headers={"x-amz-tagging": "dv-state=temp"},
                data=handle,
                expected=(200, 201),
                omit_base_headers=True,
            )

        json_data = self._file_metadata(upload)
        json_data.update({
            "storageIdentifier": storage_identifier,
            "fileName": upload.key,
            "mimeType": self.guess_mimetype(upload.abspath),
            "checksum": {"@type": "MD5", "@value": self.checksum(upload)},
        })
        response = self.session.post(
            f"{self.base_url}/api/datasets/:persistentId/addFiles",
            params={"persistentId": persistent_id},
            files={"jsonData": (None, json.dumps([json_data]), "application/json")},
            expected=(200, 201),
        )
        results = ((response.json() or {}).get("data") or {}).get("Files") or []
        failed = [r for r in results if r.get("errorMessage")]
        if failed:
            raise TargetError(
                f"{upload.key} was stored but Dataverse refused to register it: "
                f"{failed[0]['errorMessage']}"
            )
        return self._uploaded(upload, [r.get("fileDetails", {}) for r in results])

    @staticmethod
    def _uploaded(upload: FileUpload, files: Sequence[Dict[str, Any]]) -> UploadedFile:
        remote_id = None
        checksum = None
        if files:
            data_file = (files[0] or {}).get("dataFile", {})
            remote_id = str(data_file.get("id")) if data_file.get("id") is not None else None
            checksum = data_file.get("md5") or (data_file.get("checksum") or {}).get("value")

        return UploadedFile(
            key=upload.key,
            relpath=upload.relpath,
            size=upload.size,
            md5=checksum,
            remote_id=remote_id,
        )

    def publish(self, deposit: Deposit) -> Deposit:
        persistent_id = deposit.persistent_id or deposit.deposit_id
        url = f"{self.base_url}/api/datasets/:persistentId/actions/:publish"
        self.session.post(
            url,
            params={"persistentId": persistent_id, "type": self.publish_type},
            expected=(200, 201, 202),
        )
        deposit.state = "published"
        deposit.doi_registered = True
        deposit.landing_url = f"{self.base_url}/dataset.xhtml?persistentId={persistent_id}"
        return deposit

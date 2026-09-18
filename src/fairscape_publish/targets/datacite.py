"""DataCite DOI minting.

Metadata-only: DataCite registers an identifier pointing at a landing page, it
does not store files. `upload_file` is therefore a no-op, which keeps the target
interface uniform for the CLI.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from fairscape_publish.mapping import datacite as mapping
from fairscape_publish.models import CratePayload, Deposit, FileUpload, Issue, Layout, UploadedFile
from fairscape_publish.targets.base import Target
from fairscape_publish.transport import Session

PRODUCTION_URL = "https://api.datacite.org"
TEST_URL = "https://api.test.datacite.org"


class DataCiteTarget(Target):
    name = "datacite"
    default_layout = Layout.ZIP
    supported_layouts = (Layout.ZIP, Layout.FLAT, Layout.PRESERVE)
    #: DataCite stores metadata only; the CLI skips the upload phase entirely.
    uploads_files = False

    def __init__(
        self,
        session: Session,
        base_url: str = PRODUCTION_URL,
        prefix: str = "",
        event: str = "register",
        landing_url: Optional[str] = None,
    ):
        super().__init__(session, base_url)
        self.prefix = prefix
        self.event = event
        self.landing_url = landing_url

    def preflight(self, payload: CratePayload, uploads: Sequence[FileUpload]) -> List[Issue]:
        return mapping.preflight(payload, event=self.event)

    def build_metadata(self, payload: CratePayload) -> Dict[str, Any]:
        return mapping.build_metadata(
            payload, prefix=self.prefix, event=self.event, url=self.landing_url
        )

    def create_deposit(self, payload: CratePayload) -> Deposit:
        response = self.session.post(
            f"{self.base_url}/dois",
            headers={"Content-Type": "application/vnd.api+json"},
            json_body=self.build_metadata(payload),
            expected=(201,),
        )
        data = (response.json() or {}).get("data", {})
        attributes = data.get("attributes", {}) or {}
        doi = data.get("id") or attributes.get("doi") or self.dry_run_id(f"{self.prefix}/dry")

        return Deposit(
            target=self.name,
            deposit_id=str(doi),
            base_url=self.base_url,
            doi=str(doi),
            landing_url=f"https://doi.org/{doi}",
            doi_registered=True,
            state="findable" if self.event == "publish" else self.event,
            raw=data,
        )

    def upload_file(self, deposit: Deposit, upload: FileUpload) -> UploadedFile:
        raise NotImplementedError("DataCite stores metadata only; it accepts no file uploads.")

    def publish(self, deposit: Deposit) -> Deposit:
        """The `event` on creation already decides visibility; nothing more to send."""
        return deposit

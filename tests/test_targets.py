"""Integration tests for each target against mocked HTTP.

The assertions are about the requests we emit - URL, method, headers, payload
shape - since that is the part we cannot verify without a live API.
"""

import json

import pytest
import responses

from fairscape_publish.crate import discover_files
from fairscape_publish.models import Layout, Severity
from fairscape_publish.packaging import plan_uploads
from fairscape_publish.targets import (
    DataCiteTarget,
    DataverseTarget,
    FigshareTarget,
    TargetError,
    ZenodoTarget,
)
from fairscape_publish.targets import datacite as datacite_mod
from fairscape_publish.targets import figshare as figshare_mod
from fairscape_publish.targets import zenodo as zenodo_mod
from fairscape_publish.transport import Session, TransportError

DATAVERSE_URL = "https://dataverse.example.edu"


@pytest.fixture
def uploads(crate, tmp_path):
    files, _ = discover_files(crate)
    return plan_uploads(crate.root, files, Layout.PRESERVE, work_dir=tmp_path)


@pytest.fixture
def session():
    return Session(max_retries=0)


class TestDataverse:
    @responses.activate
    def test_create_deposit_posts_the_citation_block(self, payload, session):
        responses.add(
            responses.POST,
            f"{DATAVERSE_URL}/api/dataverses/demo/datasets",
            json={"status": "OK", "data": {"id": 42, "persistentId": "doi:10.5072/FK2/ABCDE"}},
            status=201,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        deposit = target.create_deposit(payload)

        assert deposit.persistent_id == "doi:10.5072/FK2/ABCDE"
        assert deposit.doi == "10.5072/FK2/ABCDE"
        assert deposit.deposit_id == "42"
        assert "version=DRAFT" in deposit.landing_url

        sent = json.loads(responses.calls[0].request.body)
        assert sent["datasetVersion"]["metadataBlocks"]["citation"]["fields"][0]["value"] == "Mini Test Crate"

    @responses.activate
    def test_upload_sends_directory_label_and_disables_tab_ingest(self, payload, session, uploads):
        responses.add(
            responses.POST,
            f"{DATAVERSE_URL}/api/datasets/:persistentId/add",
            json={"status": "OK", "data": {"files": [{"dataFile": {"id": 7, "md5": "b" * 32}}]}},
            status=200,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo", direct_upload=False)
        deposit = _deposit("dataverse", DATAVERSE_URL, persistent_id="doi:10.5072/FK2/ABCDE")
        measurements = next(u for u in uploads if u.key == "measurements.csv")

        uploaded = target.upload_file(deposit, measurements)

        assert uploaded.remote_id == "7"
        request = responses.calls[0].request
        assert "persistentId=doi%3A10.5072%2FFK2%2FABCDE" in request.url
        body = request.body.decode("utf-8", "replace") if isinstance(request.body, bytes) else str(request.body)
        assert '"directoryLabel": "data"' in body
        assert '"tabIngest": "false"' in body

    @responses.activate
    def test_upload_falls_back_to_the_native_endpoint_when_direct_is_unavailable(self, session, uploads):
        responses.add(
            responses.GET,
            f"{DATAVERSE_URL}/api/datasets/1/uploadurls",
            json={"status": "ERROR", "message": "Direct upload not supported"},
            status=400,
        )
        responses.add(
            responses.POST,
            f"{DATAVERSE_URL}/api/datasets/:persistentId/add",
            json={"status": "OK", "data": {"files": [{"dataFile": {"id": 7, "md5": "b" * 32}}]}},
            status=200,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        deposit = _deposit("dataverse", DATAVERSE_URL, persistent_id="doi:10.5072/FK2/ABCDE")
        measurements = next(u for u in uploads if u.key == "measurements.csv")

        uploaded = target.upload_file(deposit, measurements)

        assert uploaded.remote_id == "7"
        assert target.supports_direct_upload(deposit) is False
        assert any("/uploadurls" in c.request.url for c in responses.calls)
        assert responses.calls[-1].request.url.startswith(
            f"{DATAVERSE_URL}/api/datasets/:persistentId/add")

    @responses.activate
    def test_direct_upload_puts_the_bytes_at_the_store_without_the_api_token(self, session, uploads):
        put_url = "https://s3.example.com/bucket/object?X-Amz-Signature=abc"
        responses.add(
            responses.GET,
            f"{DATAVERSE_URL}/api/datasets/1/uploadurls",
            json={"status": "OK", "data": {"url": put_url, "partSize": 1 << 30,
                                           "storageIdentifier": "s3://bucket:object"}},
            status=200,
        )
        responses.add(responses.PUT, put_url, status=200)
        responses.add(
            responses.POST,
            f"{DATAVERSE_URL}/api/datasets/:persistentId/addFiles",
            json={"status": "OK", "data": {"Files": [
                {"storageIdentifier": "s3://bucket:object",
                 "fileDetails": {"dataFile": {"id": 9, "md5": "c" * 32}}}]}},
            status=200,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        deposit = _deposit("dataverse", DATAVERSE_URL, persistent_id="doi:10.5072/FK2/ABCDE")
        measurements = next(u for u in uploads if u.key == "measurements.csv")

        uploaded = target.upload_file(deposit, measurements)

        assert uploaded.remote_id == "9"
        put = next(c.request for c in responses.calls if c.request.method == "PUT")
        assert put.headers["x-amz-tagging"] == "dv-state=temp"
        assert "X-Dataverse-key" not in put.headers
        register = responses.calls[-1].request
        body = register.body.decode("utf-8", "replace") if isinstance(register.body, bytes) else str(register.body)
        assert '"storageIdentifier": "s3://bucket:object"' in body
        assert '"tabIngest": "false"' in body

    @responses.activate
    def test_direct_upload_refuses_a_file_over_the_single_part_limit(self, session, uploads):
        responses.add(
            responses.GET,
            f"{DATAVERSE_URL}/api/datasets/1/uploadurls",
            json={"status": "OK", "data": {"urls": {"1": "https://s3.example.com/part1"},
                                           "partSize": 8,
                                           "storageIdentifier": "s3://bucket:object"}},
            status=200,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo", direct_upload=True)
        deposit = _deposit("dataverse", DATAVERSE_URL, persistent_id="doi:10.5072/FK2/ABCDE")
        measurements = next(u for u in uploads if u.key == "measurements.csv")

        with pytest.raises(TargetError, match="single-part upload limit"):
            target.upload_file(deposit, measurements)

    @responses.activate
    def test_publish_requests_a_major_version(self, session):
        responses.add(
            responses.POST,
            f"{DATAVERSE_URL}/api/datasets/:persistentId/actions/:publish",
            json={"status": "OK"},
            status=200,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        deposit = target.publish(_deposit("dataverse", DATAVERSE_URL, persistent_id="doi:10.5072/FK2/ABCDE"))

        assert deposit.state == "published"
        assert "type=major" in responses.calls[0].request.url
        assert "version=DRAFT" not in deposit.landing_url


class TestZenodo:
    @responses.activate
    def test_create_deposit_captures_the_prereserved_doi_and_bucket(self, payload, session):
        responses.add(
            responses.POST,
            f"{zenodo_mod.SANDBOX_URL}/deposit/depositions",
            json={
                "id": 555,
                "metadata": {"prereserve_doi": {"doi": "10.5072/zenodo.555"}},
                "links": {"bucket": "https://sandbox.zenodo.org/api/files/abc", "html": "https://sandbox.zenodo.org/deposit/555"},
            },
            status=201,
        )
        target = ZenodoTarget(session, zenodo_mod.SANDBOX_URL)
        deposit = target.create_deposit(payload)

        assert deposit.deposit_id == "555"
        assert deposit.doi == "10.5072/zenodo.555"
        assert deposit.bucket_url == "https://sandbox.zenodo.org/api/files/abc"

        sent = json.loads(responses.calls[0].request.body)["metadata"]
        assert sent["upload_type"] == "dataset"
        assert sent["license"] == "cc-by-4.0"

    @responses.activate
    def test_upload_streams_to_the_bucket_with_a_put(self, session, uploads):
        responses.add(
            responses.PUT,
            "https://sandbox.zenodo.org/api/files/abc/measurements.csv",
            json={"checksum": "md5:" + "c" * 32, "file_id": "f-1"},
            status=201,
        )
        target = ZenodoTarget(session, zenodo_mod.SANDBOX_URL)
        deposit = _deposit("zenodo", zenodo_mod.SANDBOX_URL, bucket_url="https://sandbox.zenodo.org/api/files/abc")
        measurements = next(u for u in uploads if u.key == "measurements.csv")

        uploaded = target.upload_file(deposit, measurements)

        assert uploaded.md5 == "c" * 32
        assert responses.calls[0].request.method == "PUT"

    @responses.activate
    def test_upload_falls_back_to_the_legacy_endpoint_without_a_bucket(self, session, uploads):
        responses.add(
            responses.POST,
            f"{zenodo_mod.SANDBOX_URL}/deposit/depositions/555/files",
            json={"id": 9, "checksum": "d" * 32},
            status=201,
        )
        target = ZenodoTarget(session, zenodo_mod.SANDBOX_URL)
        deposit = _deposit("zenodo", zenodo_mod.SANDBOX_URL, deposit_id="555")
        uploaded = target.upload_file(deposit, next(u for u in uploads if u.key == "analyze.py"))
        assert uploaded.remote_id == "9"

    @responses.activate
    def test_publish_returns_the_minted_doi(self, session):
        responses.add(
            responses.POST,
            f"{zenodo_mod.SANDBOX_URL}/deposit/depositions/555/actions/publish",
            json={"doi": "10.5072/zenodo.555", "links": {"record_html": "https://sandbox.zenodo.org/record/555"}},
            status=202,
        )
        target = ZenodoTarget(session, zenodo_mod.SANDBOX_URL)
        deposit = target.publish(_deposit("zenodo", zenodo_mod.SANDBOX_URL, deposit_id="555"))
        assert deposit.state == "published"
        assert deposit.landing_url == "https://sandbox.zenodo.org/record/555"


class TestFigshare:
    BASE = figshare_mod.BASE_URL

    @responses.activate
    def test_create_deposit_then_reserves_a_doi(self, payload, session):
        responses.add(
            responses.POST, f"{self.BASE}/account/articles",
            json={"entity_id": 987, "location": f"{self.BASE}/account/articles/987"}, status=201,
        )
        responses.add(
            responses.POST, f"{self.BASE}/account/articles/987/reserve_doi",
            json={"doi": "10.6084/m9.figshare.987"}, status=200,
        )
        target = FigshareTarget(session, self.BASE, categories=[29872])
        deposit = target.create_deposit(payload)

        assert deposit.deposit_id == "987"
        assert deposit.doi == "10.6084/m9.figshare.987"
        assert json.loads(responses.calls[0].request.body)["categories"] == [29872]

    @responses.activate
    def test_article_id_falls_back_to_the_location_header_value(self, payload, session):
        responses.add(
            responses.POST, f"{self.BASE}/account/articles",
            json={"location": f"{self.BASE}/account/articles/321"}, status=201,
        )
        target = FigshareTarget(session, self.BASE, reserve_doi=False)
        assert target.create_deposit(payload).deposit_id == "321"

    @responses.activate
    def test_upload_walks_the_five_step_part_flow(self, session, uploads):
        file_location = f"{self.BASE}/account/articles/987/files/5"
        responses.add(responses.POST, f"{self.BASE}/account/articles/987/files", json={"location": file_location}, status=201)
        responses.add(responses.GET, file_location, json={"id": 5, "upload_url": "https://fup.figshare.com/upload/tok"}, status=200)
        responses.add(
            responses.GET, "https://fup.figshare.com/upload/tok",
            json={"parts": [{"partNo": 1, "startOffset": 0, "endOffset": 9}, {"partNo": 2, "startOffset": 10, "endOffset": 29}]},
            status=200,
        )
        responses.add(responses.PUT, "https://fup.figshare.com/upload/tok/1", status=200)
        responses.add(responses.PUT, "https://fup.figshare.com/upload/tok/2", status=200)
        responses.add(responses.POST, file_location, json={}, status=202)

        target = FigshareTarget(session, self.BASE)
        target.session.min_interval = 0  # do not sleep through the test
        measurements = next(u for u in uploads if u.key == "measurements.csv")
        uploaded = target.upload_file(_deposit("figshare", self.BASE, deposit_id="987"), measurements)

        assert uploaded.remote_id == "5"
        methods = [(c.request.method, c.request.url) for c in responses.calls]
        assert methods[0][0] == "POST" and methods[0][1].endswith("/files")
        assert [m[0] for m in methods] == ["POST", "GET", "GET", "PUT", "PUT", "POST"]

        # Part 1 is the first ten bytes of the file, part 2 the next twenty.
        content = measurements.abspath.read_bytes()
        assert responses.calls[3].request.body == content[0:10]
        assert responses.calls[4].request.body == content[10:30]

    @responses.activate
    def test_declared_size_and_md5_are_sent(self, session, uploads):
        file_location = f"{self.BASE}/account/articles/987/files/5"
        responses.add(responses.POST, f"{self.BASE}/account/articles/987/files", json={"location": file_location}, status=201)
        responses.add(responses.GET, file_location, json={"id": 5, "upload_url": "https://fup.figshare.com/upload/tok"}, status=200)
        responses.add(responses.GET, "https://fup.figshare.com/upload/tok", json={"parts": []}, status=200)
        responses.add(responses.POST, file_location, json={}, status=202)

        target = FigshareTarget(session, self.BASE)
        analyze = next(u for u in uploads if u.key == "analyze.py")
        target.upload_file(_deposit("figshare", self.BASE, deposit_id="987"), analyze)

        declared = json.loads(responses.calls[0].request.body)
        assert declared["name"] == "analyze.py"
        assert declared["size"] == analyze.abspath.stat().st_size
        assert len(declared["md5"]) == 32


class TestDataCite:
    @responses.activate
    def test_mint_sends_vnd_api_json(self, payload, session):
        responses.add(
            responses.POST, f"{datacite_mod.TEST_URL}/dois",
            json={"data": {"id": "10.5072/abc-123", "attributes": {"doi": "10.5072/abc-123"}}}, status=201,
        )
        target = DataCiteTarget(session, datacite_mod.TEST_URL, prefix="10.5072", event="register")
        deposit = target.create_deposit(payload)

        assert deposit.doi == "10.5072/abc-123"
        assert deposit.landing_url == "https://doi.org/10.5072/abc-123"
        assert responses.calls[0].request.headers["Content-Type"] == "application/vnd.api+json"

    def test_datacite_accepts_no_files(self, session):
        target = DataCiteTarget(session, datacite_mod.TEST_URL, prefix="10.5072")
        assert target.uploads_files is False


class TestErrorHandling:
    @responses.activate
    def test_a_4xx_surfaces_the_response_body(self, payload, session):
        responses.add(
            responses.POST, f"{DATAVERSE_URL}/api/dataverses/demo/datasets",
            json={"status": "ERROR", "message": "Bad subject"}, status=400,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        with pytest.raises(TransportError) as exc:
            target.create_deposit(payload)
        assert exc.value.status_code == 400
        assert "Bad subject" in exc.value.body

    @responses.activate
    def test_a_token_is_never_echoed_in_the_error(self, payload):
        responses.add(responses.POST, f"{DATAVERSE_URL}/api/dataverses/demo/datasets", json={}, status=403)
        session = Session(headers={"X-Dataverse-key": "super-secret"}, max_retries=0)
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        with pytest.raises(TransportError) as exc:
            target.create_deposit(payload)
        assert "super-secret" not in str(exc.value)
        assert session.calls[0].headers["X-Dataverse-key"] == "<redacted>"

    @responses.activate
    def test_a_5xx_is_retried_then_succeeds(self, payload):
        responses.add(responses.POST, f"{DATAVERSE_URL}/api/dataverses/demo/datasets", json={}, status=503)
        responses.add(
            responses.POST, f"{DATAVERSE_URL}/api/dataverses/demo/datasets",
            json={"data": {"id": 1, "persistentId": "doi:10.5072/FK2/OK"}}, status=201,
        )
        session = Session(max_retries=1)
        session._backoff = lambda *args: None  # no sleeping in tests
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        assert target.create_deposit(payload).persistent_id == "doi:10.5072/FK2/OK"
        assert len(responses.calls) == 2


def _deposit(target, base_url, deposit_id="1", **kwargs):
    from fairscape_publish.models import Deposit

    return Deposit(target=target, deposit_id=deposit_id, base_url=base_url, **kwargs)


class TestDataverseInstanceReconciliation:
    """An instance is the only authority on which licenses and fields it accepts.

    Both of these were found by a real deposit to UVA's LibraData dev instance,
    which offers only CC0/CC BY and marks productionDate required.
    """

    LICENSES = {
        "status": "OK",
        "data": [
            {"name": "CC0 1.0", "uri": "http://creativecommons.org/publicdomain/zero/1.0", "active": True},
            {"name": "CC BY 4.0", "uri": "http://creativecommons.org/licenses/by/4.0", "active": True},
        ],
    }

    def _mock_licenses(self):
        responses.add(responses.GET, f"{DATAVERSE_URL}/api/licenses", json=self.LICENSES, status=200)

    @responses.activate
    def test_the_instance_uri_wins_over_our_table(self, payload, session):
        # Our table says https://...by/4.0; this instance says http://. Dataverse
        # compares the string exactly, so the instance's spelling must be sent.
        self._mock_licenses()
        payload.license_url = "https://creativecommons.org/licenses/by/4.0/"
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        target.reconcile_license(payload)

        block = target.build_metadata(payload)["datasetVersion"]["license"]
        assert block == {"name": "CC BY 4.0", "uri": "http://creativecommons.org/licenses/by/4.0"}

    @responses.activate
    def test_an_unsupported_license_fails_before_anything_is_created(self, payload, session):
        self._mock_licenses()
        payload.license_url = "https://opensource.org/licenses/MIT"
        target = DataverseTarget(session, DATAVERSE_URL, "demo")

        with pytest.raises(TargetError) as exc:
            target.reconcile_license(payload)
        assert "CC BY 4.0, CC0 1.0" in str(exc.value)
        assert len(responses.calls) == 1  # only the licenses read; no dataset created

    @responses.activate
    def test_an_explicit_override_is_honoured_and_reported(self, payload, session):
        self._mock_licenses()
        payload.license_url = "https://opensource.org/licenses/MIT"
        target = DataverseTarget(session, DATAVERSE_URL, "demo", license="CC BY 4.0")
        note = target.reconcile_license(payload)

        assert "CC BY 4.0" in note and "MIT" in note
        assert target.build_metadata(payload)["datasetVersion"]["license"]["name"] == "CC BY 4.0"

    @responses.activate
    def test_an_override_the_instance_lacks_is_rejected(self, payload, session):
        self._mock_licenses()
        target = DataverseTarget(session, DATAVERSE_URL, "demo", license="Apache 2.0")
        with pytest.raises(TargetError, match="does not offer a license"):
            target.reconcile_license(payload)

    @responses.activate
    def test_an_unreachable_licenses_endpoint_does_not_block(self, payload, session):
        responses.add(responses.GET, f"{DATAVERSE_URL}/api/licenses", status=404)
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        assert target.reconcile_license(payload) is None
        assert target.license_override is None

    @responses.activate
    def test_a_required_field_we_do_not_send_is_reported(self, payload, session):
        responses.add(
            responses.GET,
            f"{DATAVERSE_URL}/api/metadatablocks/citation",
            json={"data": {"fields": {
                "title": {"title": "Title", "isRequired": True},
                "productionDate": {"title": "Data Creation Date", "isRequired": True},
                "timePeriodCovered": {"title": "Time Period", "isRequired": True},
                "notesText": {"title": "Notes", "isRequired": False},
            }}},
            status=200,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        issues = target.check_required_fields(payload)

        # productionDate is sent (the crate has a date); timePeriodCovered is not.
        assert [i.field for i in issues] == ["timePeriodCovered"]
        assert "Time Period" in issues[0].message

    @responses.activate
    def test_production_date_is_sent_from_the_crate(self, payload, session):
        responses.add(
            responses.GET,
            f"{DATAVERSE_URL}/api/metadatablocks/citation",
            json={"data": {"fields": {"productionDate": {"title": "Data Creation Date", "isRequired": True}}}},
            status=200,
        )
        target = DataverseTarget(session, DATAVERSE_URL, "demo")
        assert target.check_required_fields(payload) == []

        fields = {
            f["typeName"]: f["value"]
            for f in target.build_metadata(payload)["datasetVersion"]["metadataBlocks"]["citation"]["fields"]
        }
        assert fields["productionDate"] == "2026-01-15"


class TestFigshareInstanceReconciliation:
    """Figshare license ids are per-instance, so the live list has to win.

    Production numbers CC BY 4.0 as 1; stage carries a second CC BY 4.0 at 50.
    A hardcoded integer would silently deposit under the wrong license.
    """

    BASE = figshare_mod.BASE_URL
    LICENSES = [
        {"value": 1, "name": "CC BY", "url": "https://creativecommons.org/licenses/by/4.0/"},
        {"value": 3, "name": "MIT", "url": "https://opensource.org/licenses/MIT"},
        {"value": 50, "name": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"},
    ]

    def _mock(self, licenses_payload=None, categories=None):
        responses.add(responses.GET, f"{self.BASE}/licenses", json=licenses_payload or self.LICENSES, status=200)
        if categories is not None:
            responses.add(responses.GET, f"{self.BASE}/categories", json=categories, status=200)

    @responses.activate
    def test_the_crate_license_resolves_to_the_instance_id(self, payload, session):
        self._mock()
        payload.license_url = "https://opensource.org/licenses/MIT"
        target = FigshareTarget(session, self.BASE)
        note = target.reconcile_license(payload)

        assert "id 3" in note
        assert target.build_metadata(payload)["license"] == 3

    @responses.activate
    def test_an_explicit_id_the_instance_lacks_is_rejected(self, payload, session):
        self._mock()
        target = FigshareTarget(session, self.BASE, license_id=999)
        with pytest.raises(TargetError, match="not offered by"):
            target.reconcile_license(payload)

    @responses.activate
    def test_an_explicit_id_the_instance_has_is_accepted(self, payload, session):
        self._mock()
        target = FigshareTarget(session, self.BASE, license_id=50)
        assert target.reconcile_license(payload) is None
        assert target.build_metadata(payload)["license"] == 50

    @responses.activate
    def test_an_unmatched_license_is_left_to_preflight(self, payload, session):
        self._mock()
        payload.license_url = "https://example.org/unknown"
        target = FigshareTarget(session, self.BASE)
        assert target.reconcile_license(payload) is None
        assert target.resolved_license_id is None

    @responses.activate
    def test_a_bad_category_id_is_caught_before_any_write(self, payload, session):
        self._mock(categories=[{"id": 29872, "title": "Bioinformatics"}])
        target = FigshareTarget(session, self.BASE, categories=[29872, 99999999])
        issues = target.check_categories(payload)

        assert len(issues) == 1
        assert "99999999" in issues[0].message
        assert issues[0].severity is Severity.ERROR

    @responses.activate
    def test_valid_categories_raise_nothing(self, payload, session):
        self._mock(categories=[{"id": 29872, "title": "Bioinformatics"}])
        target = FigshareTarget(session, self.BASE, categories=[29872])
        assert target.check_categories(payload) == []

    @responses.activate
    def test_an_unreachable_reference_list_does_not_block(self, payload, session):
        responses.add(responses.GET, f"{self.BASE}/licenses", status=503)
        responses.add(responses.GET, f"{self.BASE}/categories", status=503)
        target = FigshareTarget(session, self.BASE, categories=[1])
        assert target.reconcile_license(payload) is None
        assert target.check_categories(payload) == []

    def test_the_sandbox_url_is_the_stage_environment(self):
        assert figshare_mod.SANDBOX_URL == "https://api.figsh.com/v2"


class TestReservedVersusRegisteredDoi:
    """A DOI on a draft is reserved, not registered, and can still change.

    Zenodo sandbox makes this visible: it reserves under the production prefix
    10.5281 but assigns 10.5072 when the record is actually published.
    """

    @responses.activate
    def test_zenodo_draft_doi_is_only_reserved(self, payload, session):
        responses.add(
            responses.POST,
            f"{zenodo_mod.SANDBOX_URL}/deposit/depositions",
            json={"id": 597016, "metadata": {"prereserve_doi": {"doi": "10.5281/zenodo.597016"}}, "links": {}},
            status=201,
        )
        deposit = ZenodoTarget(session, zenodo_mod.SANDBOX_URL).create_deposit(payload)
        assert deposit.doi == "10.5281/zenodo.597016"
        assert deposit.doi_registered is False

    @responses.activate
    def test_publishing_registers_it_and_can_change_the_prefix(self, session):
        responses.add(
            responses.POST,
            f"{zenodo_mod.SANDBOX_URL}/deposit/depositions/597016/actions/publish",
            json={"doi": "10.5072/zenodo.597016", "links": {}},
            status=202,
        )
        target = ZenodoTarget(session, zenodo_mod.SANDBOX_URL)
        deposit = target.publish(
            _deposit("zenodo", zenodo_mod.SANDBOX_URL, deposit_id="597016", doi="10.5281/zenodo.597016")
        )
        assert deposit.doi == "10.5072/zenodo.597016"
        assert deposit.doi_registered is True

    @responses.activate
    def test_dataverse_draft_doi_is_only_reserved(self, payload, session):
        responses.add(
            responses.POST,
            f"{DATAVERSE_URL}/api/dataverses/demo/datasets",
            json={"data": {"id": 1, "persistentId": "doi:10.5072/FK2/ABCDE"}},
            status=201,
        )
        deposit = DataverseTarget(session, DATAVERSE_URL, "demo").create_deposit(payload)
        assert deposit.doi_registered is False

    @responses.activate
    def test_datacite_registers_on_creation(self, payload, session):
        responses.add(
            responses.POST, f"{datacite_mod.TEST_URL}/dois",
            json={"data": {"id": "10.5072/abc"}}, status=201,
        )
        deposit = DataCiteTarget(session, datacite_mod.TEST_URL, prefix="10.5072").create_deposit(payload)
        assert deposit.doi_registered is True


class TestZenodoLicenseVocabulary:
    @responses.activate
    def test_a_license_missing_from_the_vocabulary_stops_the_run(self, payload, session):
        payload.license_url = "https://example.org/weird"
        responses.add(
            responses.GET, f"{zenodo_mod.PRODUCTION_URL}/vocabularies/licenses/cc-by-4.0", status=404
        )
        target = ZenodoTarget(session, zenodo_mod.PRODUCTION_URL, license="nonexistent-id")
        responses.add(
            responses.GET, f"{zenodo_mod.PRODUCTION_URL}/vocabularies/licenses/nonexistent-id", status=404
        )
        with pytest.raises(TargetError, match="not a license id"):
            target.reconcile_license(payload)

    @responses.activate
    def test_a_confirmed_id_is_used(self, payload, session):
        responses.add(
            responses.GET,
            f"{zenodo_mod.PRODUCTION_URL}/vocabularies/licenses/cc-by-4.0",
            json={"id": "cc-by-4.0"},
            status=200,
        )
        target = ZenodoTarget(session, zenodo_mod.PRODUCTION_URL)
        target.reconcile_license(payload)
        assert target.build_metadata(payload)["metadata"]["license"] == "cc-by-4.0"

    @responses.activate
    def test_an_unreachable_vocabulary_does_not_block(self, payload, session):
        responses.add(
            responses.GET, f"{zenodo_mod.PRODUCTION_URL}/vocabularies/licenses/cc-by-4.0", status=503
        )
        target = ZenodoTarget(session, zenodo_mod.PRODUCTION_URL)
        assert target.reconcile_license(payload) is None

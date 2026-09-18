"""CLI tests. Every one of these runs offline; --dry-run opens no sockets."""

import json
import re

import pytest
from click.testing import CliRunner

from fairscape_publish.cli import cli
from fairscape_publish.receipts import RECEIPT_FILENAME


@pytest.fixture
def run(crate_dir):
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(cli, [str(a) for a in args], catch_exceptions=False)

    invoke.crate = crate_dir
    return invoke


class TestCheck:
    def test_reports_every_target_by_default(self, run):
        result = run("check", run.crate)
        assert result.exit_code == 0, result.output
        for name in ("dataverse", "zenodo", "figshare", "datacite"):
            assert name in result.output

    def test_counts_local_files_and_flags_remote_ones(self, run):
        result = run("check", run.crate, "--target", "zenodo")
        assert "5 local file(s)" in result.output
        assert "1 remote entit" in result.output

    def test_a_blocking_issue_exits_nonzero(self, run):
        result = run("check", run.crate, "--target", "dataverse", "--subject", "Nonsense")
        assert result.exit_code != 0
        assert "not in the Dataverse subject vocabulary" in result.output

    def test_show_metadata_prints_the_payload(self, run):
        result = run("check", run.crate, "--target", "zenodo", "--show-metadata")
        assert '"upload_type": "dataset"' in result.output


class TestDryRun:
    def test_zenodo_walks_create_upload_and_leaves_a_draft(self, run):
        result = run("zenodo", run.crate, "--token", "x", "--sandbox", "--dry-run", "--verbose")
        assert result.exit_code == 0, result.output
        assert "POST https://sandbox.zenodo.org/api/deposit/depositions" in result.output
        assert "PUT https://sandbox.zenodo.org/api/files/" in result.output
        assert "Left as a draft" in result.output
        assert "Nothing was sent" in result.output

    def test_dry_run_writes_nothing_into_the_crate(self, run):
        run("zenodo", run.crate, "--token", "x", "--sandbox", "--dry-run", "--write-back")
        assert not (run.crate / RECEIPT_FILENAME).exists()
        metadata = json.loads((run.crate / "ro-crate-metadata.json").read_text())
        root = next(n for n in metadata["@graph"] if n["@id"] == "ark:99999/rocrate-mini-test-crate")
        assert "identifier" not in root

    def test_dry_run_does_not_build_the_zip(self, run, tmp_path):
        work = tmp_path / "work"
        run("zenodo", run.crate, "--token", "x", "--dry-run", "--work-dir", work)
        assert not work.exists() or not list(work.glob("*.zip"))

    def test_publish_flag_reaches_the_publish_endpoint(self, run):
        result = run("zenodo", run.crate, "--token", "x", "--sandbox", "--dry-run", "--publish", "--verbose")
        assert "actions/publish" in result.output
        assert "Left as a draft" not in result.output

    def test_dataverse_preserves_the_directory_tree(self, run):
        result = run(
            "dataverse", run.crate, "--url", "https://dv.example.edu",
            "--collection", "demo", "--token", "x", "--dry-run", "--verbose",
        )
        assert result.exit_code == 0, result.output
        assert '"directoryLabel": "data"' in result.output
        assert '"tabIngest": "false"' in result.output

    def test_figshare_runs_the_full_part_upload_flow(self, run):
        result = run(
            "figshare", run.crate, "--token", "x", "--category", "29872", "--dry-run", "--verbose"
        )
        assert result.exit_code == 0, result.output
        assert "reserve_doi" in result.output
        assert "fup.figshare.com" in result.output

    def test_datacite_uploads_no_files(self, run):
        result = run(
            "datacite", run.crate, "--prefix", "10.5072", "--username", "U",
            "--password", "P", "--test", "--dry-run", "--verbose",
        )
        assert result.exit_code == 0, result.output
        assert "Uploading" not in result.output
        assert "api.test.datacite.org/dois" in result.output

    def test_tokens_are_redacted_in_the_transcript(self, run):
        result = run("zenodo", run.crate, "--token", "hunter2", "--sandbox", "--dry-run", "--verbose")
        assert "hunter2" not in result.output
        assert "<redacted>" in result.output


class TestGuards:
    def test_a_blocking_preflight_stops_before_any_request(self, run):
        result = run(
            "dataverse", run.crate, "--url", "https://dv.example.edu", "--collection", "demo",
            "--token", "x", "--subject", "Nonsense", "--dry-run", "--verbose",
        )
        assert result.exit_code != 0
        assert "Creating deposit" not in result.output

    def test_an_unsupported_layout_is_rejected(self, run):
        result = run("zenodo", run.crate, "--token", "x", "--dry-run", "--layout", "preserve")
        assert result.exit_code != 0
        assert "does not support --layout preserve" in result.output

    def test_env_vars_supply_tokens(self, crate_dir, monkeypatch):
        monkeypatch.setenv("ZENODO_TOKEN", "from-env")
        result = CliRunner().invoke(cli, ["zenodo", str(crate_dir), "--sandbox", "--dry-run"])
        assert result.exit_code == 0, result.output

    def test_a_non_crate_path_fails_cleanly(self, tmp_path):
        stray = tmp_path / "notes.txt"
        stray.write_text("hi")
        result = CliRunner().invoke(cli, ["check", str(stray)])
        assert result.exit_code != 0
        assert "Expected a crate directory" in result.output


class TestStatus:
    def test_reports_nothing_for_a_fresh_crate(self, run):
        assert "No deposits recorded" in run("status", run.crate).output

    def test_reports_a_recorded_deposit(self, run, crate_dir):
        from fairscape_publish.models import Deposit
        from fairscape_publish.receipts import Receipt, record_from_deposit, save_receipt

        receipt = Receipt()
        receipt.upsert(
            record_from_deposit(
                Deposit(
                    target="zenodo", deposit_id="555", base_url="https://zenodo.org/api",
                    doi="10.5281/zenodo.555",
                ),
                layout="zip",
            )
        )
        save_receipt(crate_dir, receipt)

        output = run("status", crate_dir).output
        assert "zenodo  555  [draft]" in output
        assert "10.5281/zenodo.555" in output


class TestRealRunAgainstMockedHttp:
    """Covers what --dry-run necessarily skips: zip building and receipt writing."""

    ZENODO = "https://sandbox.zenodo.org/api"
    BUCKET = "https://sandbox.zenodo.org/api/files/abc"

    def _mock_zenodo(self, responses_mod):
        # Preflight confirms the license id exists before anything is created.
        responses_mod.add(
            responses_mod.GET,
            f"{self.ZENODO}/vocabularies/licenses/cc-by-4.0",
            json={"id": "cc-by-4.0", "title": {"en": "Creative Commons Attribution 4.0 International"}},
            status=200,
        )
        responses_mod.add(
            responses_mod.POST,
            f"{self.ZENODO}/deposit/depositions",
            json={
                "id": 777,
                "metadata": {"prereserve_doi": {"doi": "10.5072/zenodo.777"}},
                "links": {"bucket": self.BUCKET, "html": "https://sandbox.zenodo.org/deposit/777"},
            },
            status=201,
        )
        responses_mod.add(
            responses_mod.PUT,
            re.compile(rf"{re.escape(self.BUCKET)}/.*"),
            json={"checksum": "md5:" + "0" * 32, "file_id": "f-1"},
            status=201,
        )

    def test_a_real_run_builds_the_zip_and_writes_a_receipt(self, run, tmp_path):
        import responses as responses_mod

        with responses_mod.RequestsMock() as mocked:
            self._mock_zenodo(mocked)
            work = tmp_path / "work"
            result = run(
                "zenodo", run.crate, "--token", "x", "--sandbox", "--yes", "--work-dir", work
            )

        assert result.exit_code == 0, result.output
        assert list(work.glob("*.zip")), "the crate archive should have been built"

        receipt = json.loads((run.crate / RECEIPT_FILENAME).read_text())
        deposit = receipt["deposits"][0]
        assert deposit["deposit_id"] == "777"
        assert deposit["doi"] == "10.5072/zenodo.777"
        assert deposit["bucket_url"] == self.BUCKET
        assert sorted(f["key"] for f in deposit["files"]) == ["mini-crate.zip", "ro-crate-metadata.json"]

    def test_resume_skips_files_already_uploaded(self, run, tmp_path):
        import responses as responses_mod

        with responses_mod.RequestsMock() as mocked:
            self._mock_zenodo(mocked)
            run("zenodo", run.crate, "--token", "x", "--sandbox", "--yes", "--work-dir", tmp_path / "a")

        with responses_mod.RequestsMock() as mocked:
            # Only the publish call is registered; any upload attempt would fail.
            mocked.add(
                mocked.POST,
                f"{self.ZENODO}/deposit/depositions/777/actions/publish",
                json={"doi": "10.5072/zenodo.777", "links": {"record_html": "https://sandbox.zenodo.org/record/777"}},
                status=202,
            )
            result = run(
                "zenodo", run.crate, "--token", "x", "--sandbox", "--yes",
                "--resume", "--publish", "--write-back", "--work-dir", tmp_path / "b",
            )

        assert result.exit_code == 0, result.output
        assert "already uploaded, skipping" in result.output
        assert "Resuming draft 777" in result.output

        receipt = json.loads((run.crate / RECEIPT_FILENAME).read_text())
        assert receipt["deposits"][0]["state"] == "published"

        metadata = json.loads((run.crate / "ro-crate-metadata.json").read_text())
        root = next(n for n in metadata["@graph"] if n["@id"] == "ark:99999/rocrate-mini-test-crate")
        assert root["identifier"] == "10.5072/zenodo.777"

    def test_the_confirmation_prompt_can_abort(self, run):
        from click.testing import CliRunner

        result = CliRunner().invoke(
            cli,
            ["zenodo", str(run.crate), "--token", "x", "--sandbox"],
            input="n\n",
        )
        assert result.exit_code != 0
        assert "Aborted" in result.output
        assert not (run.crate / RECEIPT_FILENAME).exists()

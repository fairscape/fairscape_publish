import json

from fairscape_publish.models import Deposit, UploadedFile
from fairscape_publish.receipts import (
    RECEIPT_FILENAME,
    Receipt,
    load_receipt,
    record_from_deposit,
    save_receipt,
    write_back,
)


def _deposit(**kwargs):
    base = dict(
        target="zenodo",
        deposit_id="555",
        base_url="https://sandbox.zenodo.org/api",
        doi="10.5072/zenodo.555",
        landing_url="https://sandbox.zenodo.org/record/555",
    )
    base.update(kwargs)
    return Deposit(**base)


def test_missing_receipt_reads_as_empty(crate_dir):
    assert load_receipt(crate_dir).deposits == []


def test_receipt_round_trips(crate_dir):
    receipt = Receipt()
    receipt.upsert(record_from_deposit(_deposit(), layout="zip"))
    save_receipt(crate_dir, receipt)

    assert (crate_dir / RECEIPT_FILENAME).exists()
    reloaded = load_receipt(crate_dir)
    assert reloaded.deposits[0].doi == "10.5072/zenodo.555"
    assert reloaded.deposits[0].layout == "zip"


def test_upsert_replaces_the_same_deposit_rather_than_appending(crate_dir):
    receipt = Receipt()
    receipt.upsert(record_from_deposit(_deposit(), layout="zip"))
    receipt.upsert(record_from_deposit(_deposit(state="published"), layout="zip"))
    assert len(receipt.deposits) == 1
    assert receipt.deposits[0].state == "published"


def test_a_corrupt_receipt_does_not_block_publishing(crate_dir):
    (crate_dir / RECEIPT_FILENAME).write_text("{ not json")
    assert load_receipt(crate_dir).deposits == []


class TestResume:
    def _record(self):
        record = record_from_deposit(_deposit(), layout="flat")
        record.record_upload(UploadedFile(key="a.csv", size=100, md5="a" * 32))
        return record

    def test_matching_key_and_size_counts_as_uploaded(self):
        assert self._record().has_uploaded("a.csv", 100) is True

    def test_a_changed_size_forces_a_re_upload(self):
        assert self._record().has_uploaded("a.csv", 200) is False

    def test_a_changed_checksum_forces_a_re_upload(self):
        assert self._record().has_uploaded("a.csv", 100, md5="b" * 32) is False

    def test_an_unknown_key_is_not_uploaded(self):
        assert self._record().has_uploaded("b.csv", 100) is False

    def test_recording_the_same_key_twice_does_not_duplicate(self):
        record = self._record()
        record.record_upload(UploadedFile(key="a.csv", size=150, md5="c" * 32))
        assert len(record.files) == 1
        assert record.files[0].size == 150

    def test_same_filename_in_two_directories_is_two_files(self):
        """`preserve` layout repeats filenames -- three MLflow runs each publish
        a model.pkl. Keying the receipt on the bare name recorded one of them
        and let --resume skip the other two."""
        record = record_from_deposit(_deposit(), layout="preserve")
        record.record_upload(
            UploadedFile(key="model.pkl", relpath="run-a/model/model.pkl", size=100, md5="a" * 32))
        record.record_upload(
            UploadedFile(key="model.pkl", relpath="run-b/model/model.pkl", size=200, md5="b" * 32))

        assert len(record.files) == 2
        assert record.has_uploaded("run-a/model/model.pkl", 100) is True
        assert record.has_uploaded("run-b/model/model.pkl", 200) is True
        assert record.has_uploaded("run-c/model/model.pkl", 100) is False


class TestWriteBack:
    def test_identifier_and_url_land_on_the_root_entity(self, crate_dir):
        write_back(crate_dir, _deposit())
        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        root = next(n for n in metadata["@graph"] if n["@id"] == "ark:99999/rocrate-mini-test-crate")
        assert root["identifier"] == "10.5072/zenodo.555"
        assert root["url"] == "https://sandbox.zenodo.org/record/555"

    def test_a_second_deposit_accumulates_identifiers(self, crate_dir):
        write_back(crate_dir, _deposit())
        write_back(crate_dir, _deposit(target="dataverse", doi=None, persistent_id="doi:10.5072/FK2/X"))
        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        root = next(n for n in metadata["@graph"] if n["@id"] == "ark:99999/rocrate-mini-test-crate")
        assert root["identifier"] == ["10.5072/zenodo.555", "doi:10.5072/FK2/X"]

    def test_the_same_identifier_is_not_written_twice(self, crate_dir):
        write_back(crate_dir, _deposit())
        write_back(crate_dir, _deposit())
        metadata = json.loads((crate_dir / "ro-crate-metadata.json").read_text())
        root = next(n for n in metadata["@graph"] if n["@id"] == "ark:99999/rocrate-mini-test-crate")
        assert root["identifier"] == "10.5072/zenodo.555"

    def test_an_existing_url_is_left_alone(self, crate_dir):
        metadata_path = crate_dir / "ro-crate-metadata.json"
        metadata = json.loads(metadata_path.read_text())
        for node in metadata["@graph"]:
            if node["@id"] == "ark:99999/rocrate-mini-test-crate":
                node["url"] = "https://cm4ai.org/original"
        metadata_path.write_text(json.dumps(metadata))

        write_back(crate_dir, _deposit())
        root = next(
            n for n in json.loads(metadata_path.read_text())["@graph"]
            if n["@id"] == "ark:99999/rocrate-mini-test-crate"
        )
        assert root["url"] == "https://cm4ai.org/original"

    def test_subcrates_are_never_touched(self, crate_dir):
        subcrate = crate_dir / "subcrate" / "ro-crate-metadata.json"
        before = subcrate.read_text()
        write_back(crate_dir, _deposit())
        assert subcrate.read_text() == before

    def test_the_crate_stays_valid_json_after_write_back(self, crate_dir):
        write_back(crate_dir, _deposit())
        from fairscape_publish.crate import load_crate

        assert load_crate(crate_dir, validate=False).root_entity["name"] == "Mini Test Crate"

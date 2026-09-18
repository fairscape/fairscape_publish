from pathlib import Path

import pytest

from fairscape_publish.crate import (
    CrateError,
    discover_files,
    find_subcrates,
    index_content_urls,
    load_crate,
    resolve_content_url,
    root_entity,
)


def test_load_crate_accepts_directory_or_metadata_file(crate_dir):
    from_dir = load_crate(crate_dir, validate=False)
    from_file = load_crate(crate_dir / "ro-crate-metadata.json", validate=False)
    assert from_dir.root == from_file.root
    assert from_dir.root_entity["@id"] == "ark:99999/rocrate-mini-test-crate"


def test_load_crate_rejects_a_non_crate_path(tmp_path):
    stray = tmp_path / "notes.txt"
    stray.write_text("hello")
    with pytest.raises(CrateError):
        load_crate(stray)


def test_load_crate_reports_missing_metadata(tmp_path):
    with pytest.raises(CrateError, match="No ro-crate-metadata.json"):
        load_crate(tmp_path)


def test_root_entity_follows_the_about_link(crate):
    assert root_entity(crate.graph)["name"] == "Mini Test Crate"


def test_root_entity_falls_back_to_the_evi_typed_node():
    graph = [
        {"@id": "other", "@type": "Dataset", "name": "not the root"},
        {"@id": "ark:1/x", "@type": ["Dataset", "https://w3id.org/EVI#ROCrate"], "name": "root"},
    ]
    assert root_entity(graph)["name"] == "root"


def test_root_entity_returns_none_for_an_empty_graph():
    assert root_entity([]) is None


class TestResolveContentUrl:
    def test_file_scheme_is_crate_relative_not_filesystem_absolute(self, tmp_path):
        # The FAIRSCAPE convention: the path after file:// hangs off the crate root.
        assert resolve_content_url("file:///data/x.csv", tmp_path) == tmp_path / "data/x.csv"

    def test_bare_relative_path(self, tmp_path):
        assert resolve_content_url("data/x.csv", tmp_path) == tmp_path / "data/x.csv"

    def test_absolute_path_is_left_alone(self, tmp_path):
        assert resolve_content_url("/srv/data/x.csv", tmp_path) == Path("/srv/data/x.csv")

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://massive-ftp.ucsd.edu/v09/MSV000097606",
            "https://example.org/x.csv",
            "s3://bucket/key",
        ],
    )
    def test_remote_urls_are_not_local(self, url, tmp_path):
        assert resolve_content_url(url, tmp_path) is None

    def test_a_list_uses_the_first_entry(self, tmp_path):
        assert resolve_content_url(["file:///a.csv", "file:///b.csv"], tmp_path) == tmp_path / "a.csv"

    @pytest.mark.parametrize("url", [None, "", "   ", []])
    def test_empty_values_resolve_to_nothing(self, url, tmp_path):
        assert resolve_content_url(url, tmp_path) is None


def test_find_subcrates_excludes_the_root_crate(crate_dir):
    found = find_subcrates(crate_dir)
    assert [p.parent.name for p in found] == ["subcrate"]


def test_subcrate_content_urls_resolve_against_the_subcrate_root(crate):
    by_relpath, _ = index_content_urls(crate)
    # nested.txt is declared as file:///nested.txt inside subcrate/, so it must
    # land at subcrate/nested.txt, not at the parent crate's root.
    assert "subcrate/nested.txt" in by_relpath
    assert "data/measurements.csv" in by_relpath


def test_discover_files_walks_the_tree_and_skips_junk(crate):
    files, remote = discover_files(crate)
    relpaths = sorted(f.relpath for f in files)
    assert relpaths == [
        "analyze.py",
        "data/measurements.csv",
        "ro-crate-metadata.json",
        "subcrate/nested.txt",
        "subcrate/ro-crate-metadata.json",
    ]
    assert not any(".DS_Store" in p for p in relpaths)


def test_discover_files_attaches_declaring_entity_metadata(crate):
    files, _ = discover_files(crate)
    measurements = next(f for f in files if f.relpath == "data/measurements.csv")
    assert measurements.entity_id == "ark:99999/dataset-measurements-csv"
    assert measurements.description == "Measurements used by the analysis."
    assert measurements.md5 == "a" * 32
    assert measurements.directory == "data"


def test_remote_entities_are_reported_never_fetched(crate):
    _, remote = discover_files(crate)
    assert len(remote) == 1
    assert remote[0].content_url.startswith("ftp://")
    assert remote[0].content_size == "1.49 TB"


def test_only_registered_limits_to_declared_content(crate):
    files, _ = discover_files(crate, only_registered=True)
    assert sorted(f.relpath for f in files) == [
        "analyze.py",
        "data/measurements.csv",
        "subcrate/nested.txt",
    ]


def test_exclude_globs_are_honoured(crate):
    files, _ = discover_files(crate, exclude=["subcrate/*", "*.py"])
    assert sorted(f.relpath for f in files) == ["data/measurements.csv", "ro-crate-metadata.json"]


class TestValidation:
    def _break(self, crate_dir):
        import json

        path = crate_dir / "ro-crate-metadata.json"
        metadata = json.loads(path.read_text())
        for node in metadata["@graph"]:
            if node.get("@id") == "ark:99999/dataset-measurements-csv":
                del node["keywords"]
        path.write_text(json.dumps(metadata))
        return path

    def test_a_valid_crate_passes(self, crate_dir):
        assert load_crate(crate_dir).root_entity["name"] == "Mini Test Crate"

    def test_an_invalid_crate_is_rejected_by_default(self, crate_dir):
        self._break(crate_dir)
        with pytest.raises(CrateError, match="failed RO-Crate validation"):
            load_crate(crate_dir)

    def test_no_validate_lets_it_through(self, crate_dir):
        self._break(crate_dir)
        assert load_crate(crate_dir, validate=False).root_entity["name"] == "Mini Test Crate"

    def test_validation_does_not_mutate_the_caller_s_graph(self, crate_dir):
        # ROCrateV1_2's before-validator swaps @graph dicts for pydantic models in
        # place; we validate a copy so the caller keeps plain dicts to read and write.
        loaded = load_crate(crate_dir)
        assert all(isinstance(node, dict) for node in loaded.graph)
        assert loaded.graph[1].get("name") == "Mini Test Crate"


def test_junk_is_excluded_at_any_depth_but_dotted_content_survives(crate_dir):
    (crate_dir / "subcrate" / ".git").mkdir()
    (crate_dir / "subcrate" / ".git" / "config").write_text("[core]\n")
    (crate_dir / "subcrate" / "__pycache__").mkdir()
    (crate_dir / "subcrate" / "__pycache__" / "x.cpython-311.pyc").write_text("junk")
    (crate_dir / "subcrate" / ".DS_Store").write_text("junk")
    (crate_dir / ".fairscape-publish.json").write_text("{}")
    # A dotted directory that holds real crate content must still be shipped.
    (crate_dir / ".cache" / "samples").mkdir(parents=True)
    (crate_dir / ".cache" / "samples" / "sample.json").write_text("{}")

    from fairscape_publish.crate import load_crate as _load

    files, _ = discover_files(_load(crate_dir, validate=False))
    relpaths = sorted(f.relpath for f in files)

    assert ".cache/samples/sample.json" in relpaths
    assert not any(p.startswith("subcrate/.git") for p in relpaths)
    assert not any("__pycache__" in p for p in relpaths)
    assert not any(".DS_Store" in p for p in relpaths)
    assert ".fairscape-publish.json" not in relpaths

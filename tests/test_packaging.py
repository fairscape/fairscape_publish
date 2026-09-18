import zipfile

import pytest

from fairscape_publish.crate import discover_files
from fairscape_publish.models import Layout
from fairscape_publish.packaging import (
    build_crate_zip,
    flat_name,
    human_bytes,
    md5_of,
    plan_uploads,
)


def test_flat_name_encodes_the_path(self=None):
    assert flat_name("data/raw.csv") == "data__raw.csv"
    assert flat_name("top.csv") == "top.csv"


@pytest.mark.parametrize(
    "size,expected", [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024**2, "5.0 MB")]
)
def test_human_bytes(size, expected):
    assert human_bytes(size) == expected


def test_zip_contains_exactly_the_discovered_files(crate, tmp_path):
    files, _ = discover_files(crate)
    archive = build_crate_zip(crate.root, files, out_dir=tmp_path)
    with zipfile.ZipFile(archive) as zf:
        names = sorted(zf.namelist())
    assert names == sorted(f.relpath for f in files)
    assert ".DS_Store" not in " ".join(names)


def test_zip_layout_ships_the_archive_plus_a_metadata_sidecar(crate, tmp_path):
    files, _ = discover_files(crate)
    uploads = plan_uploads(crate.root, files, Layout.ZIP, work_dir=tmp_path)
    assert [u.key for u in uploads] == ["mini-crate.zip", "ro-crate-metadata.json"]
    assert uploads[0].is_generated is True


def test_zip_layout_can_be_planned_without_building(crate, tmp_path):
    files, _ = discover_files(crate)
    uploads = plan_uploads(crate.root, files, Layout.ZIP, work_dir=tmp_path, build=False)
    # Nothing written; the reported size is the uncompressed upper bound.
    assert not uploads[0].abspath.exists()
    assert uploads[0].size == sum(f.size for f in files)


def test_flat_layout_path_encodes_every_file(crate, tmp_path):
    files, _ = discover_files(crate)
    uploads = plan_uploads(crate.root, files, Layout.FLAT, work_dir=tmp_path)
    keys = sorted(u.key for u in uploads)
    assert "data__measurements.csv" in keys
    assert "subcrate__ro-crate-metadata.json" in keys
    assert all(u.directory_label == "" for u in uploads)


def test_preserve_layout_carries_the_directory(crate, tmp_path):
    files, _ = discover_files(crate)
    uploads = {u.key: u for u in plan_uploads(crate.root, files, Layout.PRESERVE, work_dir=tmp_path)}
    assert uploads["measurements.csv"].directory_label == "data"
    assert uploads["analyze.py"].directory_label == ""


def test_preserve_layout_keeps_same_named_files_apart(crate, tmp_path):
    files, _ = discover_files(crate)
    uploads = plan_uploads(crate.root, files, Layout.PRESERVE, work_dir=tmp_path)
    # Both crates declare a ro-crate-metadata.json; the directory keeps them distinct.
    metadata = [(u.directory_label, u.key) for u in uploads if u.key.startswith("ro-crate-metadata")]
    assert sorted(metadata) == [("", "ro-crate-metadata.json"), ("subcrate", "ro-crate-metadata.json")]


def test_flat_layout_renames_a_colliding_basename(crate, tmp_path):
    files, _ = discover_files(crate)
    for item in files:
        item.relpath = item.relpath.rsplit("/", 1)[-1]
    uploads = plan_uploads(crate.root, files, Layout.PRESERVE, work_dir=tmp_path)
    keys = [u.key for u in uploads]
    assert len(keys) == len(set(keys))
    assert "ro-crate-metadata-1.json" in keys


def test_md5_matches_hashlib(crate):
    import hashlib

    path = crate.root / "analyze.py"
    assert md5_of(path) == hashlib.md5(path.read_bytes()).hexdigest()

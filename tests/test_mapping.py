import pytest

from fairscape_publish.mapping import datacite, dataverse, figshare, licenses, zenodo
from fairscape_publish.mapping.common import (
    normalize_authors,
    normalize_date,
    normalize_keywords,
    publication_year,
    split_names,
)
from fairscape_publish.models import Severity


class TestAuthors:
    def test_orcid_id_references_resolve_against_person_nodes(self, crate):
        # CM4AI-style crates list authors as bare @id refs into the graph.
        authors = normalize_authors(crate.root_entity, crate=crate)
        lovelace = authors[0]
        assert lovelace.name == "Lovelace, A"
        assert lovelace.affiliation == "Example University"
        assert lovelace.orcid == "0000-0002-1825-0097"

    def test_inline_dicts_are_kept(self, crate):
        authors = normalize_authors(crate.root_entity, crate=crate)
        assert authors[1].name == "Hopper, G"
        assert authors[1].affiliation == "Example University"

    def test_semicolons_win_over_commas(self):
        assert split_names("Clark, T; Ideker, T") == ["Clark, T", "Ideker, T"]

    def test_single_comma_pair_is_one_name(self):
        assert split_names("Lovelace, Ada") == ["Lovelace, Ada"]

    def test_list_of_strings(self):
        authors = normalize_authors({"author": ["Alice", "Bob"]})
        assert [a.name for a in authors] == ["Alice", "Bob"]

    def test_duplicates_are_dropped_preserving_order(self):
        authors = normalize_authors({"author": ["Alice", "alice", "Bob"]})
        assert [a.name for a in authors] == ["Alice", "Bob"]

    def test_unresolvable_orcid_ref_still_yields_an_author(self):
        authors = normalize_authors({"author": [{"@id": "https://orcid.org/0000-0002-1825-0097"}]})
        assert authors[0].orcid == "0000-0002-1825-0097"

    def test_authors_csv_fills_gaps_without_overwriting(self):
        csv_data = {"alice": {"affiliation": "Somewhere", "orcid": "0000-0002-1825-0097"}}
        authors = normalize_authors({"author": ["Alice"]}, authors_csv=csv_data)
        assert authors[0].affiliation == "Somewhere"
        assert authors[0].orcid == "0000-0002-1825-0097"


class TestScalars:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2026-01-15", "2026-01-15"),
            ("2026-01-15T10:30:00Z", "2026-01-15"),
            ("01/15/2026", "2026-01-15"),
            ("2026", "2026-01-01"),
        ],
    )
    def test_dates_normalize_to_iso(self, value, expected):
        assert normalize_date(value) == expected

    def test_unparseable_date_falls_back_to_today(self):
        assert normalize_date("not a date") is not None

    def test_publication_year(self):
        assert publication_year("2026-01-15") == 2026

    def test_keywords_from_a_comma_string(self):
        assert normalize_keywords("a, b, c") == ["a", "b", "c"]

    def test_keywords_dedupe_case_insensitively(self):
        assert normalize_keywords(["AI", "ai", "ML"]) == ["AI", "ML"]


class TestLicenses:
    def test_deed_suffix_and_trailing_slash_are_folded(self):
        found = licenses.lookup("https://creativecommons.org/licenses/by-nc-sa/4.0/deed.en")
        assert found is not None and found.spdx == "CC-BY-NC-SA-4.0"

    def test_http_and_https_are_the_same_license(self):
        assert licenses.lookup("http://creativecommons.org/licenses/by/4.0/") is licenses.lookup(
            "https://creativecommons.org/licenses/by/4.0"
        )

    def test_lookup_by_spdx_and_by_name(self):
        assert licenses.lookup("MIT").zenodo_id == "mit"
        assert licenses.lookup("CC BY 4.0").figshare_id == 1

    def test_unknown_license_resolves_to_the_default(self):
        assert licenses.lookup("https://example.org/nope") is None
        assert licenses.resolve("https://example.org/nope").spdx == "CC-BY-4.0"


class TestDataverseMapping:
    def test_citation_block_shape(self, payload):
        doc = dataverse.build_metadata(payload)
        fields = {f["typeName"]: f for f in doc["datasetVersion"]["metadataBlocks"]["citation"]["fields"]}
        assert fields["title"]["value"] == "Mini Test Crate"
        assert doc["datasetVersion"]["license"]["name"] == "CC BY 4.0"
        assert fields["subject"]["typeClass"] == "controlledVocabulary"
        assert fields["datasetContact"]["value"][0]["datasetContactEmail"]["value"] == "curator@example.edu"

    def test_orcid_becomes_an_author_identifier(self, payload):
        doc = dataverse.build_metadata(payload)
        fields = {f["typeName"]: f for f in doc["datasetVersion"]["metadataBlocks"]["citation"]["fields"]}
        first = fields["author"]["value"][0]
        assert first["authorIdentifierScheme"]["value"] == "ORCID"
        assert first["authorIdentifier"]["value"] == "0000-0002-1825-0097"

    def test_missing_contact_email_is_blocking(self, payload):
        payload.contact_email = None
        issues = dataverse.preflight(payload)
        assert any(i.severity is Severity.ERROR and i.field == "contactEmail" for i in issues)

    def test_subject_outside_the_vocabulary_is_blocking(self, payload):
        issues = dataverse.preflight(payload, subjects=["Underwater Basket Weaving"])
        assert any(i.severity is Severity.ERROR and i.field == "subject" for i in issues)

    def test_a_valid_crate_has_no_blocking_issues(self, payload):
        assert not [i for i in dataverse.preflight(payload) if i.severity is Severity.ERROR]


class TestZenodoMapping:
    def test_metadata_shape(self, payload):
        doc = zenodo.build_metadata(payload)["metadata"]
        assert doc["upload_type"] == "dataset"
        assert doc["license"] == "cc-by-4.0"
        assert doc["access_right"] == "open"
        assert doc["publication_date"] == "2026-01-15"
        assert doc["creators"][0]["orcid"] == "0000-0002-1825-0097"

    def test_description_is_wrapped_in_html_and_escaped(self, payload):
        payload.description = "5 < 6 & always"
        doc = zenodo.build_metadata(payload)["metadata"]
        assert doc["description"] == "<p>5 &lt; 6 &amp; always</p>"

    def test_ark_becomes_a_related_identifier(self, payload):
        doc = zenodo.build_metadata(payload)["metadata"]
        assert doc["related_identifiers"][0]["identifier"].startswith("ark:99999/")

    def test_closed_access_omits_the_license(self, payload):
        doc = zenodo.build_metadata(payload, access_right="closed")["metadata"]
        assert "license" not in doc

    def test_communities_are_passed_through(self, payload):
        doc = zenodo.build_metadata(payload, communities=["bridge2ai"])["metadata"]
        assert doc["communities"] == [{"identifier": "bridge2ai"}]

    def test_too_many_files_is_blocking(self, payload):
        issues = zenodo.preflight(payload, file_count=101)
        assert any(i.severity is Severity.ERROR and i.field == "files" for i in issues)

    def test_oversized_file_is_blocking(self, payload):
        issues = zenodo.preflight(payload, file_count=1, largest_file=zenodo.MAX_FILE_BYTES + 1)
        assert any(i.severity is Severity.ERROR for i in issues)


class TestFigshareMapping:
    def test_metadata_shape(self, payload):
        doc = figshare.build_metadata(payload, categories=[29872])
        assert doc["defined_type"] == "dataset"
        assert doc["categories"] == [29872]
        assert doc["license"] == 1  # derived from the crate's CC BY 4.0
        assert doc["authors"][0]["orcid_id"] == "0000-0002-1825-0097"

    def test_explicit_license_id_wins(self, payload):
        assert figshare.build_metadata(payload, license_id=42)["license"] == 42

    def test_missing_category_is_a_warning_for_a_draft(self, payload):
        issues = figshare.preflight(payload, will_publish=False)
        category = next(i for i in issues if i.field == "categories")
        assert category.severity is Severity.WARNING

    def test_missing_category_blocks_a_publish(self, payload):
        issues = figshare.preflight(payload, will_publish=True)
        category = next(i for i in issues if i.field == "categories")
        assert category.severity is Severity.ERROR


class TestDataCiteMapping:
    def test_attributes_shape(self, payload):
        doc = datacite.build_metadata(payload, prefix="10.5072", event="register")
        attrs = doc["data"]["attributes"]
        assert doc["data"]["type"] == "dois"
        assert attrs["prefix"] == "10.5072"
        assert attrs["publicationYear"] == 2026
        assert attrs["types"]["resourceTypeGeneral"] == "Dataset"
        assert attrs["event"] == "register"
        assert attrs["publisher"] == "Example University"

    def test_orcid_becomes_a_name_identifier(self, payload):
        attrs = datacite.build_metadata(payload, prefix="10.5072")["data"]["attributes"]
        identifiers = attrs["creators"][0]["nameIdentifiers"]
        assert identifiers[0]["nameIdentifier"] == "https://orcid.org/0000-0002-1825-0097"

    def test_ark_is_recorded_as_an_alternate_identifier(self, payload):
        attrs = datacite.build_metadata(payload, prefix="10.5072")["data"]["attributes"]
        assert attrs["alternateIdentifiers"][0]["alternateIdentifierType"] == "ARK"

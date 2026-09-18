# Metadata crosswalk

How an RO-Crate's root entity becomes each repository's metadata document.
Source of truth is `src/fairscape_publish/mapping/`; this file is the readable
version of it.

## Normalization applied before any target sees the crate

`mapping/common.py` flattens the root entity into a `CratePayload`. Four things
get normalized because real crates disagree about them:

**Authors.** Four shapes appear in the wild and all four are handled:

| Shape | Example | Handling |
|---|---|---|
| Delimited string | `"Clark, T; Ideker, T"` | Split on `;`, or on `,` when there are more than two commas |
| List of strings | `["Clark, T", "Ideker, T"]` | Taken as-is |
| List of objects | `[{"name": ..., "affiliation": ...}]` | Name, affiliation, ORCID read directly |
| List of `@id` refs | `[{"@id": "https://orcid.org/0000-..."}]` | Resolved against `Person` nodes in the same graph |

The last one is what CM4AI releases use. An unresolvable ORCID reference still
produces an author carrying the ORCID. `--authors-csv` fills in affiliation and
ORCID for authors that have neither, matched on lowercased name.

**Keywords.** A comma-separated string, a list of strings, or a list of
`DefinedTerm` objects all become a de-duplicated list of strings.

**Dates.** ISO 8601 (with or without a time), `MM/DD/YYYY`, `DD-MM-YYYY` and a
bare year all become `YYYY-MM-DD`. Unparseable values fall back to today.

**Licenses.** `mapping/licenses.py` holds one table. Lookup folds
`http`/`https`, `www.`, a trailing slash, and the `/deed.en` and `/legalcode`
suffixes, so `https://creativecommons.org/licenses/by-nc-sa/4.0/deed.en` and
`http://creativecommons.org/licenses/by-nc-sa/4.0/` resolve to the same entry.
Each entry carries the SPDX id, display name, Zenodo string id, Figshare integer
id, and Dataverse name/URI pair. An unrecognized license falls back to CC BY 4.0
with a warning.

**Contact.** `contactEmail` on the root, then `contactPoint.email`. Contact name
is `principalInvestigator`, then `contactName`, then the first author. Both are
overridable with `--contact-name` / `--contact-email`.

## Field-by-field

| Crate (root entity) | Dataverse (citation block) | Zenodo (deposition) | Figshare (article) | DataCite (attributes) |
|---|---|---|---|---|
| `name` | `title` | `title` | `title` | `titles[0].title` |
| `description` | `dsDescription.dsDescriptionValue` | `description` (HTML-escaped, paragraphs wrapped in `<p>`) | `description` | `descriptions[0]`, type `Abstract` |
| `author` | `author` compound, with `authorIdentifierScheme: ORCID` | `creators[]` with `affiliation`, `orcid` | `authors[]` with `orcid_id` | `creators[]` with `nameIdentifiers` |
| `keywords` | `keyword` compound | `keywords[]` | `keywords[]` | `subjects[]` |
| `license` | `datasetVersion.license` name + uri | `license` (Zenodo string id) | `license` (Figshare integer id) | `rightsList[]` with `rightsIdentifier` |
| `version` | — | `version` | — | `version` |
| `dateCreated` | `productionDate` ("Data Creation Date") |  |  |  |
| `datePublished` | `distributionDate` | `publication_date` | — | `dates[].Issued`, `publicationYear` |
| `publisher` | `producer.producerName` | — | — | `publisher` (defaults to `FAIRSCAPE`) |
| `associatedPublication` | `publication.publicationCitation` | `related_identifiers` `isSupplementTo` (DOI extracted) | `references[]` | — |
| `contactEmail` / `principalInvestigator` | `datasetContact` compound | `contributors[]` type `ContactPerson` | — | — |
| `@id` (ARK) | `notesText` | `related_identifiers` `isIdenticalTo` | — | `alternateIdentifiers[]` type `ARK` |
| `url` | — | — | — | `url` (falls back to `@id`) |
| — | `subject` (`--subject`, default *Medicine, Health and Life Sciences*) | `upload_type: dataset`, `access_right: open` | `defined_type: dataset`, `categories` (`--category`) | `types.resourceTypeGeneral: Dataset` |
| — | `dateOfDeposit` = today | `communities` (`--community`) | — | `schemaVersion` kernel-4, `language: en` |

Empty values are dropped rather than sent as `""` or `[]`.

## Files

`contentUrl` resolution follows the FAIRSCAPE convention, not RFC 8089:

| Value | Resolves to |
|---|---|
| `file:///data/x.csv` | `<crate root>/data/x.csv` — crate-relative despite the leading slash |
| `data/x.csv` | `<crate root>/data/x.csv` |
| `/srv/data/x.csv` | `/srv/data/x.csv` |
| `ftp://`, `http(s)://`, `s3://`, `gs://`, `sftp://` | Nothing. Reported as a remote entity and skipped. |

A subcrate's `file:///` paths resolve against *that subcrate's* root, so
`subcrate/ro-crate-metadata.json` declaring `file:///nested.txt` maps to
`subcrate/nested.txt` in the parent crate.

A file discovered on disk is matched against the resolved `contentUrl` index; if
an entity declares it, that entity's `description` and `md5` ride along —
Dataverse receives the description as the file's description, and the checksum is
recorded in the receipt for `--resume` comparisons.

## Preflight rules

`check` and every deposit command run these before any request. Errors block;
warnings are printed and the run continues.

| Target | Error | Warning |
|---|---|---|
| Dataverse | missing title, description, or contact email; a `subject` outside the controlled vocabulary; a license the instance does not offer; an instance-required field the crate cannot fill | no authors; unrecognized license; no `dateCreated`/`datePublished` |
| Zenodo | missing title or description; more than 100 files; a file over 50 GB | no authors; unrecognized license |
| Figshare | missing title. With `--publish`: missing description, authors, keywords, categories, or license id | the same five when only creating a draft |
| DataCite | missing title; no URL and no `@id` | no authors; no publisher when publishing |


## Instances are not interchangeable

Two of the rules above are read from the live instance rather than assumed,
because a Dataverse deployment can change both:

| Read | Why |
|---|---|
| `GET /api/licenses` | An instance accepts only its configured licenses and matches the URI string exactly. UVA LibraData offers just CC0 1.0 and CC BY 4.0, and spells the CC BY URI `http://`. A crate declaring MIT is refused with a 400 at create time unless caught first. |
| `GET /api/metadatablocks/citation` | Instances add required fields. LibraData marks `productionDate` required; stock Dataverse does not. Missing one produces a 403 mid-run. |
| `GET /api/datasets/{id}/uploadurls` | Whether the instance will issue a presigned URL for its object store. Where it will, file bytes bypass the application server — required for large files, and the only way past a WAF that inspects upload bodies (LibraData dev rejects any body containing `../`). |

Zenodo needs it for a third reason:

| Read | Why |
|---|---|
| `GET /api/vocabularies/licenses/{id}` | Since the InvenioRDM migration, `license` is validated against an SPDX-id vocabulary. An id outside it is rejected at create time. |

Figshare needs the same treatment for a different reason — its license and
category ids are integers assigned per instance:

| Read | Why |
|---|---|
| `GET /v2/licenses` | Production numbers CC BY 4.0 as `1`; stage carries a second CC BY 4.0 at `50`; institutional instances differ again. A hardcoded id does not fail loudly — it deposits under the wrong license. The crate's license URL is matched against the live list instead. |
| `GET /v2/categories` | A category id that does not exist on the instance is rejected at publish time, long after the files are uploaded. |

All four reads are unauthenticated. They are turned into preflight errors, so a
run that cannot succeed stops before it creates a half-populated draft. None of
them ever edits the crate.


## Reserved DOIs are not registered DOIs

Every file-hosting target hands back a DOI when the draft is created, and none of
them are registered yet:

| Target | At draft | At publish |
|---|---|---|
| Zenodo | `prereserve_doi` — **sandbox reserves under the production prefix `10.5281`** | the real DOI, `10.5072/...` on sandbox |
| Dataverse | `persistentId` reserved against the draft | registered |
| Figshare | `reserve_doi` | registered |
| DataCite | n/a — creating the DOI *is* the registration | `--event publish` makes it findable |

`Deposit.doi_registered` carries the distinction, the CLI prints `(reserved)`
until publish, and `status` keeps showing it that way. A reserved DOI does not
resolve and can change; it should not go into a citation.

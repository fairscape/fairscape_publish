# fairscape-publish

Push a local FAIRSCAPE RO-Crate — with all of its local files — to Dataverse,
Zenodo, or Figshare, or mint a DOI for it on DataCite.

Point it at a crate directory. It walks the tree (subcrates included), maps the
crate's root entity onto the repository's metadata schema, creates a draft
record, uploads every local file, and writes a receipt so the run can be resumed.

```bash
pip install -e .

# See whether a crate is ready, without touching the network
fairscape-publish check ./my-crate

# Rehearse the whole deposit: every request printed, nothing sent
fairscape-publish zenodo ./my-crate --token $ZENODO_TOKEN --sandbox --dry-run --verbose

# Create the draft for real
fairscape-publish zenodo ./my-crate --token $ZENODO_TOKEN --sandbox
```

## Safety defaults

- **Deposits are drafts.** Nothing becomes public without `--publish`.
- `--dry-run` opens no sockets, builds no archive, and writes nothing into the crate.
- A confirmation prompt shows the target, file count and total bytes before the
  first network write. `--yes` skips it.
- Remote `contentUrl` entities (`ftp://`, `https://`, `s3://`) are reported and
  skipped, never downloaded. A CM4AI release that points at 1.5 TB of FTP data
  deposits its metadata and local files without fetching a byte of it.
- API tokens are redacted from every log line and error message.

## Commands

| Command | What it does |
|---|---|
| `check CRATE` | Offline readiness report for each target. Exits non-zero on a blocking issue. |
| `dataverse CRATE` | Create a Dataverse dataset and upload files, directory tree preserved. |
| `zenodo CRATE` | Create a Zenodo deposition and upload files through the bucket API. |
| `figshare CRATE` | Create a Figshare article and upload files through its part-upload flow. |
| `datacite CRATE` | Mint a DOI from crate metadata. Metadata only; no files. |
| `status CRATE` | Show what has already been deposited from this crate. |

### Credentials

Every token can come from the environment instead of the command line:

| Target | Option | Environment variable |
|---|---|---|
| Dataverse | `--token` | `DATAVERSE_API_TOKEN` |
| Zenodo | `--token` | `ZENODO_TOKEN` |
| Figshare | `--token` | `FIGSHARE_TOKEN` |
| DataCite | `--username` / `--password` | `DATACITE_USERNAME` / `DATACITE_PASSWORD` |

### File layout

Dataverse understands directories; Zenodo and Figshare present a flat file list.
`--layout` decides how the crate tree is projected:

| Layout | Result | Default for |
|---|---|---|
| `zip` | One crate archive plus `ro-crate-metadata.json` alongside it, so the landing page shows readable metadata | Zenodo, Figshare |
| `preserve` | Every file uploaded separately, its directory carried in Dataverse's `directoryLabel` | Dataverse |
| `flat` | Every file uploaded separately, path encoded into the name (`data__raw.csv`) | — |

By default the whole crate directory is walked, so datasheets, READMEs and each
nested `ro-crate-metadata.json` travel with the data. `--only-registered`
restricts the upload to files a crate entity declares via `contentUrl`.
`--exclude GLOB` (repeatable) drops anything else; `.git/`, `.DS_Store`,
`__pycache__` and the receipt file are always excluded.

### Resuming

Each run writes `.fairscape-publish.json` next to `ro-crate-metadata.json`,
recording the deposit id, DOI, landing page and every file uploaded. If a run is
interrupted:

```bash
fairscape-publish zenodo ./my-crate --token $ZENODO_TOKEN --resume
```

reuses the existing draft and skips files already uploaded at the same size.
Files are tracked by their crate-relative path, not their name: under `preserve`
layout three MLflow runs each publish a `model.pkl`, and they are three files.
Once you have reviewed the draft in the repository:

```bash
fairscape-publish zenodo ./my-crate --token $ZENODO_TOKEN --resume --publish --write-back
```

`--write-back` records the assigned DOI as `identifier` on the crate's root
entity. Without it, `ro-crate-metadata.json` is never modified.

## Per-target notes

**Dataverse** needs a contact email (`contactEmail` on the crate root, or
`--contact-email`) and a `subject` from its controlled vocabulary
(`--subject`, repeatable; `check` lists the valid terms). Tabular ingest is
disabled by default so uploaded csv/tsv files stay byte-identical to the crate's
declared checksums; `--tab-ingest` re-enables it.

Every Dataverse instance is configured differently, so before creating anything
the client makes two read-only calls to the instance and turns what it learns
into preflight errors:

- `GET /api/licenses` — an instance only accepts licenses configured on it, and
  compares the URI string exactly. The instance's spelling of a matched license
  wins over the built-in table (UVA's LibraData publishes CC BY 4.0 as `http://`,
  not `https://`). If the crate's license is not offered at all, the run stops
  and lists what is; `--license "CC BY 4.0"` picks one of them. The crate's own
  metadata is never rewritten to match.
- `GET /api/metadatablocks/citation` — instances mark extra fields required.
  LibraData requires *Data Creation Date* (`productionDate`), which is optional
  in stock Dataverse. Anything required that the crate cannot fill is reported
  before the upload starts rather than as a 403 halfway in.
- `GET /api/datasets/{id}/uploadurls` — asked once, after the draft exists, to
  find out whether the instance will hand out a presigned URL for its object
  store. If it will, file bytes go straight there and only the registration
  metadata goes to Dataverse; if it will not, the native `/add` endpoint is
  used. `--direct-upload` / `--no-direct-upload` forces the choice.

Direct upload is not only about size. A Dataverse behind a web application
firewall can reject perfectly ordinary files: UVA's dev instance answers any
multipart body containing the literal `../` with a bare nginx 403, which a
README documenting `-profile <docker/singularity/.../institute>` is enough to
trigger. Sending the bytes to the object store takes the file body out of the
firewall's path entirely. Multi-part direct upload (for files above the
instance's `partSize`, commonly 1–5 GB) is not implemented; such a file is
reported with a message pointing at `--no-direct-upload`.

**Zenodo** caps a record at 100 files and 50 GB per file, which is why `zip` is
the default layout. `--sandbox` targets sandbox.zenodo.org — use it first; it
requires its own account and token. Since the InvenioRDM migration Zenodo
validates `license` against `/api/vocabularies/licenses` (SPDX ids, lowercased),
so preflight confirms the id exists before creating anything; `--license` picks a
different one.

**A DOI on a draft is reserved, not registered.** It is not resolvable and it can
still change, so the CLI labels it `(reserved)` until the record is published.
Zenodo sandbox makes this concrete: it reserves under the *production* prefix
`10.5281` and only assigns the sandbox prefix `10.5072` at publish time. Do not
put a reserved DOI in a paper.

**Figshare** has a stage environment at `api.figsh.com` (`--sandbox`), but its
credentials are separate from production and are issued per institution by
Figshare support — a production token will not work there. Publishing requires
title, authors, categories, keywords, description and license. Both category ids
and license ids are per-instance, so the client reads `/v2/licenses` and
`/v2/categories` (both unauthenticated) during preflight: the crate's license is
matched to the id this instance actually uses, and a category id it does not have
is reported before anything is uploaded. The client honours Figshare's
one-request-per-second limit.

**DataCite** stores metadata only. `--event register` (the default) creates a
registered DOI without making it findable; `--event publish` makes it findable.
`--test` targets api.test.datacite.org.

## Development

```bash
pip install -e ".[test]"
pytest
```

The whole suite is offline — targets run against mocked HTTP, and the CLI tests
run under `--dry-run`. A `live` pytest marker exists for future smoke tests
against real sandboxes; nothing is marked with it.

### Test environments

| Repository | Endpoint | Getting in |
|---|---|---|
| Zenodo | `sandbox.zenodo.org` (`--sandbox`) | Self-service: register an account, create a token. Mints real test DOIs under `10.5072`. |
| Dataverse | `demo.dataverse.org`, or an institutional dev instance | Self-service on demo. |
| DataCite | `api.test.datacite.org` (`--test`), Fabrica at `doi.test.datacite.org` | Members and repositories get test accounts from their Direct Member; otherwise ask DataCite support. Test credentials are separate from production and carry a different prefix. |
| Figshare | `api.figsh.com` (`--sandbox`) | Stage credentials are issued per institution by Figshare support. `/v2/licenses` and `/v2/categories` are readable without a token, so preflight works against it unauthenticated. |

Zenodo sandbox is the only one of the four you can set up entirely on your own
in a few minutes, so it is the right place to start.

Both the Dataverse and the Zenodo paths have been exercised end to end against
live servers: UVA LibraData dev deposits (10 files, then 44 and 90 files via S3
direct upload, `preserve` layout, every checksum byte-identical after upload)
and a Zenodo sandbox record published at `10.5072/zenodo.597016` (`zip` layout,
archive downloaded back and verified byte-identical to the source crate). The
two larger Dataverse deposits are the crates in
[`../fairscape_complete_examples`](../fairscape_complete_examples).

See [PUBLISH_MAPPING.md](PUBLISH_MAPPING.md) for the metadata crosswalk.
# fairscape_publish

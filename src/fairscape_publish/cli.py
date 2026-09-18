"""fairscape-publish command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import click

from fairscape_publish import __version__
from fairscape_publish.crate import Crate, CrateError, load_crate
from fairscape_publish.mapping.common import build_payload, load_authors_csv
from fairscape_publish.models import (
    CratePayload,
    Deposit,
    FileUpload,
    Issue,
    Layout,
    Severity,
)
from fairscape_publish.packaging import human_bytes, plan_uploads
from fairscape_publish.receipts import (
    load_receipt,
    record_from_deposit,
    save_receipt,
    write_back,
)
from fairscape_publish.targets import (
    DataCiteTarget,
    DataverseTarget,
    FigshareTarget,
    Target,
    TargetError,
    ZenodoTarget,
)
from fairscape_publish.targets import datacite as datacite_target
from fairscape_publish.targets import fixtures as dry_run_fixtures
from fairscape_publish.targets import figshare as figshare_target
from fairscape_publish.targets import zenodo as zenodo_target
from fairscape_publish.transport import DryRunSession, Session, TransportError


# --------------------------------------------------------------------------
# shared options
# --------------------------------------------------------------------------


def crate_options(func: Callable) -> Callable:
    """Options every deposit command shares."""
    decorators = [
        click.argument("crate", type=click.Path(exists=True, path_type=Path)),
        click.option(
            "--layout",
            type=click.Choice([layout.value for layout in Layout]),
            default=None,
            help="How the crate's files are laid out in the repository. Defaults to the target's own default.",
        ),
        click.option(
            "--exclude",
            multiple=True,
            help="Glob of crate-relative paths to leave out. Repeatable.",
        ),
        click.option(
            "--only-registered",
            is_flag=True,
            help="Upload only files declared by a crate entity's contentUrl, instead of walking the directory.",
        ),
        click.option(
            "--authors-csv",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="CSV with name,affiliation,orcid columns used to enrich crate authors.",
        ),
        click.option("--contact-name", help="Override the contact name."),
        click.option("--contact-email", help="Override the contact email."),
        click.option(
            "--publish",
            "do_publish",
            is_flag=True,
            help="Make the record public. Without this the deposit is left as a draft.",
        ),
        click.option(
            "--write-back",
            is_flag=True,
            help="Record the assigned DOI/persistent id on the crate's root entity.",
        ),
        click.option(
            "--resume",
            is_flag=True,
            help="Reuse the draft in .fairscape-publish.json and skip files already uploaded.",
        ),
        click.option("--dry-run", is_flag=True, help="Print every request that would be made. No network."),
        click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt."),
        click.option("--no-validate", is_flag=True, help="Skip RO-Crate schema validation."),
        click.option(
            "--work-dir",
            type=click.Path(file_okay=False, path_type=Path),
            help="Where to build the crate zip. Defaults to a temporary directory.",
        ),
        click.option("--verbose", "-v", is_flag=True, help="Log each HTTP request."),
    ]
    for decorator in reversed(decorators):
        func = decorator(func)
    return func


class Options:
    """The shared options, unpacked from click's kwargs."""

    def __init__(self, kwargs: Dict[str, Any]):
        self.crate: Path = kwargs.pop("crate")
        self.layout: Optional[str] = kwargs.pop("layout")
        self.exclude: Tuple[str, ...] = kwargs.pop("exclude")
        self.only_registered: bool = kwargs.pop("only_registered")
        self.authors_csv: Optional[Path] = kwargs.pop("authors_csv")
        self.contact_name: Optional[str] = kwargs.pop("contact_name")
        self.contact_email: Optional[str] = kwargs.pop("contact_email")
        self.do_publish: bool = kwargs.pop("do_publish")
        self.write_back: bool = kwargs.pop("write_back")
        self.resume: bool = kwargs.pop("resume")
        self.dry_run: bool = kwargs.pop("dry_run")
        self.yes: bool = kwargs.pop("yes")
        self.no_validate: bool = kwargs.pop("no_validate")
        self.work_dir: Optional[Path] = kwargs.pop("work_dir")
        self.verbose: bool = kwargs.pop("verbose")


# --------------------------------------------------------------------------
# shared machinery
# --------------------------------------------------------------------------


def make_session(options: Options, headers: Dict[str, str], min_interval: float = 0.0) -> Session:
    logger = (lambda message: click.echo(f"  {message}", err=True)) if options.verbose else None
    if options.dry_run:
        return DryRunSession(headers=headers, logger=logger)
    return Session(headers=headers, min_interval=min_interval, logger=logger)


def prepare(options: Options) -> Tuple[Crate, CratePayload]:
    """Load and validate the crate, then flatten it into a payload."""
    crate = load_crate(options.crate, validate=not options.no_validate)
    from fairscape_publish.crate import discover_files

    files, remote = discover_files(
        crate, exclude=options.exclude, only_registered=options.only_registered
    )
    payload = build_payload(
        crate,
        files=files,
        remote_entities=remote,
        authors_csv=load_authors_csv(options.authors_csv),
        contact_name=options.contact_name,
        contact_email=options.contact_email,
    )
    return crate, payload


def resolve_layout(options: Options, target: Target) -> Layout:
    if options.layout is None:
        return target.default_layout
    layout = Layout(options.layout)
    if layout not in target.supported_layouts:
        supported = ", ".join(item.value for item in target.supported_layouts)
        raise click.ClickException(
            f"{target.name} does not support --layout {layout.value}. Supported: {supported}."
        )
    return layout


def report_issues(issues: Sequence[Issue]) -> int:
    errors = 0
    for issue in issues:
        colour = "red" if issue.severity is Severity.ERROR else "yellow"
        click.secho(f"  {issue}", fg=colour, err=True)
        if issue.severity is Severity.ERROR:
            errors += 1
    return errors


def print_summary(payload: CratePayload, target: Target, layout: Layout, uploads: Sequence[FileUpload]) -> None:
    click.echo("")
    click.secho(payload.name, bold=True)
    click.echo(f"  crate      {payload.crate_root}")
    click.echo(f"  identifier {payload.guid}")
    click.echo(f"  target     {target.name} ({target.base_url})")
    click.echo(f"  layout     {layout.value}")
    click.echo(
        f"  files      {len(payload.files)} local, {human_bytes(payload.total_bytes)}"
        f"  ->  {len(uploads)} upload(s), {human_bytes(sum(u.size for u in uploads))}"
    )
    if payload.remote_entities:
        total = len(payload.remote_entities)
        click.secho(
            f"  skipped    {total} entit{'y' if total == 1 else 'ies'} whose contentUrl is remote:", fg="yellow"
        )
        for entity in payload.remote_entities[:5]:
            size = f" ({entity.content_size})" if entity.content_size else ""
            click.secho(f"               {entity.content_url}{size}", fg="yellow")
        if total > 5:
            click.secho(f"               ... and {total - 5} more", fg="yellow")
    click.echo("")


def run_deposit(options: Options, target: Target) -> None:
    """The full create -> upload -> publish flow, shared by every target."""
    try:
        crate, payload = prepare(options)
    except CrateError as exc:
        raise click.ClickException(str(exc)) from exc

    layout = resolve_layout(options, target)
    uploads_files = getattr(target, "uploads_files", True)

    uploads: List[FileUpload] = []
    if uploads_files:
        uploads = plan_uploads(
            crate_root=payload.crate_root,
            files=payload.files,
            layout=layout,
            guid=payload.guid,
            work_dir=options.work_dir,
            build=not options.dry_run,
            progress=None,
        )

    print_summary(payload, target, layout, uploads)

    click.secho("Preflight", bold=True)
    reconcile = getattr(target, "reconcile_license", None)
    if reconcile is not None and not options.dry_run:
        # Dataverse only accepts licenses configured on the instance, so this is
        # one live read that turns a create-time 400 into an actionable message.
        note = _call(reconcile, payload)
        if note:
            click.secho(f"  {note}", fg="yellow")
    issues = list(target.preflight(payload, uploads))
    if not options.dry_run:
        for hook_name in ("check_required_fields", "check_categories"):
            hook = getattr(target, hook_name, None)
            if hook is not None:
                issues.extend(_call(hook, payload))
    if issues:
        errors = report_issues(issues)
        if errors:
            raise click.ClickException(
                f"{errors} blocking issue(s). Fix the crate metadata or pass the matching option, then retry."
            )
    else:
        click.secho("  no issues", fg="green")
    click.echo("")

    if options.dry_run:
        click.secho("Metadata that would be sent", bold=True)
        click.echo(json.dumps(target.build_metadata(payload), indent=2)[:8000])
        click.echo("")

    if not options.yes and not options.dry_run:
        action = "publish publicly" if options.do_publish else "create a draft"
        click.confirm(
            f"This will {action} on {target.name} ({target.base_url}) and upload "
            f"{len(uploads)} file(s), {human_bytes(sum(u.size for u in uploads))}. Continue?",
            abort=True,
        )

    receipt = load_receipt(payload.crate_root)
    record = receipt.find(target.name, target.base_url) if options.resume else None

    if record and record.state == "draft":
        click.secho(f"Resuming draft {record.deposit_id}", bold=True)
        if record.layout != layout.value:
            click.secho(
                f"  warning: the draft was created with --layout {record.layout}; "
                f"uploading {layout.value} files into it will mix the two.",
                fg="yellow",
            )
        deposit = Deposit(
            target=record.target,
            deposit_id=record.deposit_id,
            base_url=record.base_url,
            doi=record.doi,
            landing_url=record.landing_url,
            persistent_id=record.persistent_id,
            doi_registered=record.doi_registered,
            bucket_url=record.bucket_url,
            state=record.state,
        )
    else:
        click.secho("Creating deposit", bold=True)
        deposit = _call(target.create_deposit, payload)
        record = record_from_deposit(deposit, layout.value)
        receipt.upsert(record)
        _save(options, payload.crate_root, receipt)
        click.echo(f"  id  {deposit.deposit_id}")
        if deposit.doi:
            suffix = "" if deposit.doi_registered else "  (reserved)"
            click.echo(f"  doi {deposit.doi}{suffix}")

    if uploads_files and uploads:
        click.secho(f"Uploading {len(uploads)} file(s)", bold=True)
        for index, upload in enumerate(uploads, start=1):
            label = f"[{index}/{len(uploads)}] {upload.key} ({human_bytes(upload.size)})"
            if options.resume and record.has_uploaded(upload.identity, upload.size):
                click.secho(f"  {label} - already uploaded, skipping", fg="cyan")
                continue
            click.echo(f"  {label}")
            uploaded = _call(target.upload_file, deposit, upload)
            record.record_upload(uploaded)
            _save(options, payload.crate_root, receipt)
        _call(target.finalize, deposit)

    if options.do_publish:
        click.secho("Publishing", bold=True)
        deposit = _call(target.publish, deposit)
        record.state = deposit.state
        record.doi = deposit.doi or record.doi
        record.doi_registered = deposit.doi_registered
        record.landing_url = deposit.landing_url or record.landing_url
        _save(options, payload.crate_root, receipt)

    if options.write_back:
        if options.dry_run:
            identifier = deposit.doi or deposit.persistent_id
            click.echo(f"Would set identifier={identifier!r} on {payload.metadata_path}")
        else:
            written = write_back(payload.crate_root, deposit)
            if written:
                click.echo(f"Updated {written}")

    _print_result(deposit, options)

    if options.dry_run and options.verbose:
        click.echo("")
        click.secho("Request transcript", bold=True)
        for call in target.session.calls:
            click.echo(call.describe())
            click.echo("")


def _save(options: Options, crate_root: Path, receipt: Any) -> None:
    """Persist the receipt, unless this is a dry run - which must not touch the crate."""
    if options.dry_run:
        return
    save_receipt(crate_root, receipt)


def _call(func: Callable, *args: Any) -> Any:
    try:
        return func(*args)
    except (TransportError, TargetError) as exc:
        message = str(exc)
        body = getattr(exc, "body", "")
        if body:
            message = f"{message}\n{body}"
        raise click.ClickException(message) from exc


def _print_result(deposit: Deposit, options: Options) -> None:
    click.echo("")
    prefix = "[dry run] " if options.dry_run else ""
    click.secho(f"{prefix}{deposit.state.title()} on {deposit.target}", fg="green", bold=True)
    if deposit.persistent_id:
        click.echo(f"  persistent id {deposit.persistent_id}")
    if deposit.doi:
        if deposit.doi_registered:
            click.echo(f"  doi           {deposit.doi}")
        else:
            click.echo(f"  doi           {deposit.doi}  (reserved, not registered until published)")
    if deposit.landing_url:
        click.echo(f"  url           {deposit.landing_url}")
    if not options.do_publish and deposit.target != "datacite":
        click.echo("")
        click.secho(
            "  Left as a draft. Review it in the repository, then re-run with --publish "
            "--resume to make it public.",
            fg="cyan",
        )
    if options.dry_run:
        click.echo("")
        click.secho("  Nothing was sent. Remove --dry-run to make these requests.", fg="cyan")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


@click.group()
@click.version_option(__version__, prog_name="fairscape-publish")
def cli() -> None:
    """Push a local RO-Crate, with all of its local files, to a data repository.

    Deposits are created as drafts. Nothing becomes public without --publish.
    """


@cli.command("dataverse")
@click.option("--url", required=True, help="Base URL of the Dataverse instance, e.g. https://dataverse.harvard.edu.")
@click.option("--collection", required=True, help="Alias of the target Dataverse collection.")
@click.option("--token", required=True, envvar="DATAVERSE_API_TOKEN", help="Dataverse API token (env: DATAVERSE_API_TOKEN).")
@click.option("--subject", "subjects", multiple=True, help="Dataverse subject term. Repeatable. See `check` for valid values.")
@click.option("--tab-ingest", is_flag=True, help="Let Dataverse ingest csv/tsv into its tabular format. Off by default so files stay byte-identical.")
@click.option("--publish-type", type=click.Choice(["major", "minor"]), default="major", help="Version bump when publishing.")
@click.option("--license", "license_choice", default=None, help="License name or URI to deposit under, when the crate's own license is not configured on the instance.")
@click.option(
    "--direct-upload/--no-direct-upload",
    "direct_upload",
    default=None,
    help=(
        "Send file bytes straight to the instance's object store with a presigned URL, "
        "instead of through the Dataverse application server. Detected from the instance "
        "by default; force it on when a firewall in front of Dataverse rejects file bodies."
    ),
)
@crate_options
def dataverse_command(url: str, collection: str, token: str, subjects: Tuple[str, ...], tab_ingest: bool, publish_type: str, license_choice: Optional[str], direct_upload: Optional[bool], **kwargs: Any) -> None:
    """Deposit into a Dataverse collection, preserving the crate's directory tree."""
    options = Options(kwargs)
    session = make_session(options, headers={"X-Dataverse-key": token})
    target = DataverseTarget(
        session=session,
        base_url=url,
        collection=collection,
        subjects=list(subjects) or None,
        tab_ingest=tab_ingest,
        publish_type=publish_type,
        license=license_choice,
        direct_upload=direct_upload,
    )
    if isinstance(session, DryRunSession):
        dry_run_fixtures.install(session, target.name, target.base_url)
    run_deposit(options, target)


@cli.command("zenodo")
@click.option("--token", required=True, envvar="ZENODO_TOKEN", help="Zenodo personal access token (env: ZENODO_TOKEN).")
@click.option("--sandbox", is_flag=True, help="Use sandbox.zenodo.org instead of zenodo.org.")
@click.option("--api-url", default=None, help="Override the Zenodo API base URL.")
@click.option("--community", "communities", multiple=True, help="Zenodo community identifier. Repeatable.")
@click.option("--upload-type", default="dataset", help="Zenodo upload_type. Defaults to dataset.")
@click.option("--access-right", type=click.Choice(["open", "embargoed", "restricted", "closed"]), default="open")
@click.option("--license", "license_choice", default=None, help="Zenodo license id (SPDX, lowercased) to deposit under, overriding the crate's.")
@crate_options
def zenodo_command(token: str, sandbox: bool, api_url: Optional[str], communities: Tuple[str, ...], upload_type: str, access_right: str, license_choice: Optional[str], **kwargs: Any) -> None:
    """Deposit into Zenodo. Defaults to a single crate zip plus its metadata."""
    options = Options(kwargs)
    base_url = api_url or (zenodo_target.SANDBOX_URL if sandbox else zenodo_target.PRODUCTION_URL)
    session = make_session(options, headers={"Authorization": f"Bearer {token}"})
    target = ZenodoTarget(
        session=session,
        base_url=base_url,
        communities=list(communities) or None,
        upload_type=upload_type,
        access_right=access_right,
        license=license_choice,
    )
    if isinstance(session, DryRunSession):
        dry_run_fixtures.install(session, target.name, target.base_url)
    run_deposit(options, target)


@cli.command("figshare")
@click.option("--token", required=True, envvar="FIGSHARE_TOKEN", help="Figshare personal token (env: FIGSHARE_TOKEN).")
@click.option("--category", "categories", multiple=True, type=int, help="Figshare category id. Repeatable. Required to publish.")
@click.option("--license-id", type=int, default=None, help="Figshare numeric license id. Derived from the crate license when omitted.")
@click.option("--defined-type", default="dataset", help="Figshare item type. Defaults to dataset.")
@click.option("--sandbox", is_flag=True, help="Use Figshare's stage environment (api.figsh.com). Needs separate stage credentials.")
@click.option("--api-url", default=None, help="Override the Figshare API base URL.")
@click.option("--no-reserve-doi", is_flag=True, help="Skip DOI reservation on the draft.")
@crate_options
def figshare_command(token: str, categories: Tuple[int, ...], license_id: Optional[int], defined_type: str, sandbox: bool, api_url: Optional[str], no_reserve_doi: bool, **kwargs: Any) -> None:
    """Deposit into Figshare. --sandbox targets the stage environment."""
    options = Options(kwargs)
    base_url = api_url or (figshare_target.SANDBOX_URL if sandbox else figshare_target.BASE_URL)
    session = make_session(
        options,
        headers={"Authorization": f"token {token}"},
        min_interval=figshare_target.MIN_REQUEST_INTERVAL,
    )
    target = FigshareTarget(
        session=session,
        base_url=base_url,
        categories=list(categories) or None,
        license_id=license_id,
        defined_type=defined_type,
        reserve_doi=not no_reserve_doi,
        will_publish=options.do_publish,
    )
    if isinstance(session, DryRunSession):
        dry_run_fixtures.install(session, target.name, target.base_url)
    run_deposit(options, target)


@cli.command("datacite")
@click.option("--prefix", required=True, help="Your DataCite DOI prefix, e.g. 10.1234.")
@click.option("--username", required=True, envvar="DATACITE_USERNAME", help="Repository id, e.g. MEMBER.REPO (env: DATACITE_USERNAME).")
@click.option("--password", required=True, envvar="DATACITE_PASSWORD", help="DataCite password (env: DATACITE_PASSWORD).")
@click.option("--test", "use_test", is_flag=True, help="Use api.test.datacite.org.")
@click.option("--api-url", default=None, help="Override the DataCite API base URL.")
@click.option("--event", type=click.Choice(["register", "publish", "hide"]), default="register", help="DOI state. 'register' keeps it a draft-visible DOI.")
@click.option("--landing-url", default=None, help="Landing page the DOI resolves to. Defaults to the crate's url, then its @id.")
@crate_options
def datacite_command(prefix: str, username: str, password: str, use_test: bool, api_url: Optional[str], event: str, landing_url: Optional[str], **kwargs: Any) -> None:
    """Mint a DOI from crate metadata. Metadata only; no files are uploaded."""
    import base64

    options = Options(kwargs)
    base_url = api_url or (datacite_target.TEST_URL if use_test else datacite_target.PRODUCTION_URL)
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    session = make_session(options, headers={"Authorization": f"Basic {credentials}"})
    target = DataCiteTarget(
        session=session, base_url=base_url, prefix=prefix, event=event, landing_url=landing_url
    )
    if isinstance(session, DryRunSession):
        dry_run_fixtures.install(session, target.name, target.base_url)
    run_deposit(options, target)


@cli.command("check")
@click.argument("crate", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--target",
    "target_names",
    multiple=True,
    type=click.Choice(["dataverse", "zenodo", "figshare", "datacite"]),
    help="Target to check. Repeatable. Defaults to all four.",
)
@click.option("--layout", type=click.Choice([layout.value for layout in Layout]), default=None)
@click.option("--exclude", multiple=True)
@click.option("--only-registered", is_flag=True)
@click.option("--authors-csv", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--contact-email", default=None)
@click.option("--category", "categories", multiple=True, type=int, help="Figshare category ids, for the Figshare check.")
@click.option("--subject", "subjects", multiple=True, help="Dataverse subject terms, for the Dataverse check.")
@click.option("--no-validate", is_flag=True)
@click.option("--show-metadata", is_flag=True, help="Also print the metadata document each target would receive.")
def check_command(
    crate: Path,
    target_names: Tuple[str, ...],
    layout: Optional[str],
    exclude: Tuple[str, ...],
    only_registered: bool,
    authors_csv: Optional[Path],
    contact_email: Optional[str],
    categories: Tuple[int, ...],
    subjects: Tuple[str, ...],
    no_validate: bool,
    show_metadata: bool,
) -> None:
    """Report, entirely offline, whether a crate is ready for each repository."""
    options = Options(
        {
            "crate": crate,
            "layout": layout,
            "exclude": exclude,
            "only_registered": only_registered,
            "authors_csv": authors_csv,
            "contact_name": None,
            "contact_email": contact_email,
            "do_publish": False,
            "write_back": False,
            "resume": False,
            "dry_run": True,
            "yes": True,
            "no_validate": no_validate,
            "work_dir": None,
            "verbose": False,
        }
    )
    try:
        crate_obj, payload = prepare(options)
    except CrateError as exc:
        raise click.ClickException(str(exc)) from exc

    session = DryRunSession()
    builders: Dict[str, Callable[[], Target]] = {
        "dataverse": lambda: DataverseTarget(session, "https://dataverse.example.edu", "example", subjects=list(subjects) or None),
        "zenodo": lambda: ZenodoTarget(session, zenodo_target.PRODUCTION_URL),
        "figshare": lambda: FigshareTarget(session, figshare_target.BASE_URL, categories=list(categories) or None),
        "datacite": lambda: DataCiteTarget(session, datacite_target.PRODUCTION_URL, prefix="10.0000"),
    }
    selected = list(target_names) or list(builders)

    click.secho(payload.name, bold=True)
    click.echo(f"  {len(payload.files)} local file(s), {human_bytes(payload.total_bytes)}")
    click.echo(f"  {len(payload.authors)} author(s), {len(payload.keywords)} keyword(s)")
    if payload.remote_entities:
        click.secho(f"  {len(payload.remote_entities)} remote entit(y/ies) will not be uploaded", fg="yellow")
    click.echo("")

    blocking = 0
    for name in selected:
        target = builders[name]()
        target_layout = Layout(layout) if layout else target.default_layout
        if target_layout not in target.supported_layouts:
            target_layout = target.default_layout
        uploads = (
            plan_uploads(
                crate_root=payload.crate_root,
                files=payload.files,
                layout=target_layout,
                guid=payload.guid,
                build=False,
            )
            if getattr(target, "uploads_files", True)
            else []
        )
        click.secho(f"{name} (layout: {target_layout.value})", bold=True)
        issues = target.preflight(payload, uploads)
        if issues:
            blocking += report_issues(issues)
        else:
            click.secho("  ready", fg="green")
        if show_metadata:
            click.echo(json.dumps(target.build_metadata(payload), indent=2))
        click.echo("")

    if blocking:
        raise click.ClickException(f"{blocking} blocking issue(s) across the selected targets.")


@cli.command("status")
@click.argument("crate", type=click.Path(exists=True, path_type=Path))
def status_command(crate: Path) -> None:
    """Show what has already been deposited from this crate."""
    root = crate if crate.is_dir() else crate.parent
    receipt = load_receipt(root)
    if not receipt.deposits:
        click.echo(f"No deposits recorded for {root}")
        return

    for record in receipt.deposits:
        click.secho(f"{record.target}  {record.deposit_id}  [{record.state}]", bold=True)
        click.echo(f"  base url   {record.base_url}")
        if record.persistent_id:
            click.echo(f"  persistent {record.persistent_id}")
        if record.doi:
            suffix = "" if record.doi_registered else "  (reserved)"
            click.echo(f"  doi        {record.doi}{suffix}")
        if record.landing_url:
            click.echo(f"  url        {record.landing_url}")
        click.echo(f"  layout     {record.layout}")
        click.echo(f"  files      {len(record.files)} uploaded, {human_bytes(sum(f.size for f in record.files))}")
        click.echo(f"  created    {record.created_at}")
        click.echo(f"  updated    {record.updated_at}")
        click.echo("")

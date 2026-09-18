"""One license table, four repositories.

Existing FAIRSCAPE code carried three divergent partial license maps (Dataverse
name/uri pairs in fairscape-cli, Zenodo string ids and Figshare integer ids in
fairscape_server). This consolidates them so a crate's ``license`` URL resolves
consistently everywhere.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from pydantic import BaseModel


class License(BaseModel):
    spdx: str
    name: str
    url: str
    zenodo_id: str
    figshare_id: Optional[int] = None
    dataverse_name: str = ""
    dataverse_uri: str = ""

    @property
    def dataverse(self) -> Dict[str, str]:
        return {"name": self.dataverse_name or self.name, "uri": self.dataverse_uri or self.url}

    @property
    def datacite_rights(self) -> Dict[str, str]:
        return {"rights": self.name, "rightsUri": self.url, "rightsIdentifier": self.spdx}


#: Figshare license ids are per-instance; these are the defaults on figshare.com.
LICENSES = [
    License(
        spdx="CC-BY-4.0", name="CC BY 4.0", url="https://creativecommons.org/licenses/by/4.0",
        zenodo_id="cc-by-4.0", figshare_id=1,
        dataverse_name="CC BY 4.0", dataverse_uri="https://creativecommons.org/licenses/by/4.0",
    ),
    License(
        spdx="CC0-1.0", name="CC0 1.0", url="https://creativecommons.org/publicdomain/zero/1.0",
        zenodo_id="cc-zero", figshare_id=2,
        dataverse_name="CC0 1.0", dataverse_uri="http://creativecommons.org/publicdomain/zero/1.0",
    ),
    License(
        spdx="CC-BY-SA-4.0", name="CC BY-SA 4.0", url="https://creativecommons.org/licenses/by-sa/4.0",
        zenodo_id="cc-by-sa-4.0",
        dataverse_name="CC BY-SA 4.0", dataverse_uri="https://creativecommons.org/licenses/by-sa/4.0",
    ),
    License(
        spdx="CC-BY-NC-4.0", name="CC BY-NC 4.0", url="https://creativecommons.org/licenses/by-nc/4.0",
        zenodo_id="cc-by-nc-4.0",
        dataverse_name="CC BY-NC 4.0", dataverse_uri="https://creativecommons.org/licenses/by-nc/4.0",
    ),
    License(
        spdx="CC-BY-NC-SA-4.0", name="CC BY-NC-SA 4.0",
        url="https://creativecommons.org/licenses/by-nc-sa/4.0",
        zenodo_id="cc-by-nc-sa-4.0",
        dataverse_name="CC BY-NC-SA 4.0", dataverse_uri="https://creativecommons.org/licenses/by-nc-sa/4.0",
    ),
    License(
        spdx="CC-BY-ND-4.0", name="CC BY-ND 4.0", url="https://creativecommons.org/licenses/by-nd/4.0",
        zenodo_id="cc-by-nd-4.0",
        dataverse_name="CC BY-ND 4.0", dataverse_uri="https://creativecommons.org/licenses/by-nd/4.0",
    ),
    License(
        spdx="CC-BY-NC-ND-4.0", name="CC BY-NC-ND 4.0",
        url="https://creativecommons.org/licenses/by-nc-nd/4.0",
        zenodo_id="cc-by-nc-nd-4.0",
        dataverse_name="CC BY-NC-ND 4.0", dataverse_uri="https://creativecommons.org/licenses/by-nc-nd/4.0",
    ),
    License(
        spdx="MIT", name="MIT License", url="https://opensource.org/licenses/MIT",
        zenodo_id="mit", figshare_id=3,
    ),
    License(
        spdx="Apache-2.0", name="Apache License 2.0", url="https://www.apache.org/licenses/LICENSE-2.0",
        zenodo_id="apache-2.0", figshare_id=7,
    ),
    License(
        spdx="GPL-3.0-or-later", name="GNU General Public License v3.0 or later",
        url="https://www.gnu.org/licenses/gpl-3.0", zenodo_id="gpl-3.0-or-later", figshare_id=6,
    ),
    License(
        spdx="BSD-3-Clause", name="BSD 3-Clause License",
        url="https://opensource.org/licenses/BSD-3-Clause", zenodo_id="bsd-3-clause",
    ),
]

DEFAULT_LICENSE = LICENSES[0]  # CC BY 4.0


def normalize_license_url(url: Any) -> str:
    """Fold the cosmetic variation crates carry: scheme, www, trailing slash, /deed.*, /legalcode."""
    if isinstance(url, dict):
        url = url.get("@id") or url.get("url")
    if isinstance(url, list):
        url = url[0] if url else None
    if not isinstance(url, str):
        return ""

    value = url.strip().rstrip("/")
    lowered = value.lower()
    for suffix in ("/legalcode", "/deed"):
        idx = lowered.rfind(suffix)
        if idx != -1:
            value = value[:idx]
            lowered = value.lower()
    if lowered.startswith("http://"):
        value = "https://" + value[len("http://"):]
    value = value.replace("https://www.", "https://")
    return value.rstrip("/")


_BY_URL = {normalize_license_url(lic.url): lic for lic in LICENSES}
_BY_SPDX = {lic.spdx.lower(): lic for lic in LICENSES}
_BY_NAME = {lic.name.lower(): lic for lic in LICENSES}


def lookup(url: Any) -> Optional[License]:
    """Resolve a crate license value by URL, SPDX id, or human name. None if unknown."""
    if not url:
        return None
    normalized = normalize_license_url(url)
    if normalized in _BY_URL:
        return _BY_URL[normalized]

    raw = url if isinstance(url, str) else normalized
    key = raw.strip().lower()
    return _BY_SPDX.get(key) or _BY_NAME.get(key)


def resolve(url: Any, default: License = DEFAULT_LICENSE) -> License:
    """Like `lookup` but always returns something usable."""
    return lookup(url) or default


def match_available(
    wanted: Any,
    available: Sequence[Dict[str, Any]],
    name_key: str = "name",
    uri_key: str = "uri",
) -> Optional[Dict[str, Any]]:
    """Find a repository's own entry for a license, given a name, URI, or SPDX id.

    Repositories publish their configured licenses on a public endpoint and only
    accept what is on that list - Dataverse at /api/licenses, Figshare at
    /v2/licenses. Their ids and URI spellings are per-instance, so the live list
    always wins over the table above.
    """
    if not wanted:
        return None

    text = str(wanted).strip()
    lowered = text.lower()
    normalized = normalize_license_url(text)

    for item in available:
        if str(item.get(name_key, "")).strip().lower() == lowered:
            return item
    if normalized:
        for item in available:
            if normalize_license_url(item.get(uri_key)) == normalized:
                return item

    known = lookup(text)
    if known:
        target = normalize_license_url(known.url)
        for item in available:
            if normalize_license_url(item.get(uri_key)) == target:
                return item
            if str(item.get(name_key, "")).strip().lower() == known.name.lower():
                return item
    return None


def names_of(available: Sequence[Dict[str, Any]], name_key: str = "name") -> str:
    return ", ".join(sorted(str(item.get(name_key)) for item in available))

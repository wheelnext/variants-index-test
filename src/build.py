from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import urljoin
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import jinja2
import jsonschema_rs
import requests
from bs4 import BeautifulSoup
from packaging.version import Version

BUILD_DIR = Path("build")
BUILD_DATE = dt.datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
VARIANT_SCHEMA_PATH = Path(__file__).parent / "schema.json"
VARIANT_VALIDATOR = jsonschema_rs.Draft202012Validator(
    json.loads(VARIANT_SCHEMA_PATH.read_text(encoding="utf-8"))
)

logger = logging.getLogger(__name__)

VARIANT_JSON_FILE_REGEX = re.compile(r"\S*-(.*)-variants\.json")
VARIANT_WHL_FILE_REGEX = re.compile(
    r"(?P<base_wheel_name>                "  # <base_wheel_name> group (without variant)
    r"  (?P<namever>                      "  # "namever" group contains <name>-<ver>
    r"    (?P<name>[^\s-]+?)              "  # <name>
    r"    - (?P<ver>[^\s-]*?)             "  # "-" <ver>
    r"  )                                 "  # close "namever" group
    r"  (?: - (?P<build>\d[^-]*?) )?      "  # optional "-" <build>
    r"  - (?P<pyver>[^\s-]+?)             "  # "-" <pyver> tag
    r"  - (?P<abi>[^\s-]+?)               "  # "-" <abi> tag
    r"  - (?P<plat>[^\s-]+?)              "  # "-" <plat> tag
    r")                                   "  # end of <base_wheel_name> group
    r"(?: - (?P<variant_label>            "  # optional <variant_label>
    r"     [0-9a-z._]{1,16}              "
    r"    )                               "
    r")?                                  "
    r"\.whl                               "  # ".whl" suffix
    r"                                    ",
    re.VERBOSE,
)


def sha256sum(path: Path, chunk_size: int = 8192) -> str:
    """Compute the SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class PkgConfig:
    name: str
    registry: str
    is_provider: bool = False


@dataclass(frozen=True)
class Artifact:
    name: str
    link: str
    checksum: str

    def re_match(self, regex: re.Pattern[str]) -> re.Match[str]:
        match = regex.match(self.name)
        if match is None:
            raise ValueError(f"Impossible to match the regex with `{self.name}`")

        return match


@dataclass(frozen=True)
class VariantJson(Artifact):
    @property
    def version(self) -> str:
        return self.re_match(VARIANT_JSON_FILE_REGEX).group(1)

    @classmethod
    def from_file(cls, fp: Path) -> VariantJson:
        return VariantJson(name=fp.name, link=fp.name, checksum=sha256sum(fp))


@dataclass(frozen=True)
class VariantWheel(Artifact):
    vprops: list[str] | None = None

    @property
    def version(self) -> str:
        return self.re_match(VARIANT_WHL_FILE_REGEX).group("ver")

    @property
    def variant_alias(self) -> str | None:
        return self.re_match(VARIANT_WHL_FILE_REGEX).group("variant_label")


def safe_urljoin(base: str, path: str) -> str:
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, path)


def generate_main_index(packages: list[str]) -> None:
    # Load template
    current_dir = Path(__file__).parent
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(current_dir / "templates"),
        autoescape=True,
    )
    template = jinja_env.get_template("main_page.j2")

    filtered_packages = [
        pkg for pkg in packages if (BUILD_DIR / pkg / "index.html").is_file()
    ]

    # Render template
    output = template.render(
        directories=sorted(filtered_packages),
        build_date=BUILD_DATE,
    )

    with (BUILD_DIR / "index.html").open(mode="w") as f:
        f.write(output)


def fetch_links(url: str) -> list[VariantWheel | VariantJson]:
    # Fetch the content of the URL
    response = requests.get(url, timeout=10)
    response.raise_for_status()  # Ensure we notice bad responses

    # Parse the HTML content
    soup = BeautifulSoup(response.text, "html.parser")

    # Use URL filenames because directory listings may abbreviate link text.
    artifacts: list[VariantWheel | VariantJson] = []
    for a_tag in soup.find_all("a", href=True):
        href: str = a_tag["href"]  # pyright: ignore[reportUnknownVariableType, reportAssignmentType, reportArgumentType, reportIndexIssue]
        parsed = urlparse(href)
        filename = unquote(parsed.path.rsplit("/", maxsplit=1)[-1])

        if filename.endswith((".json", ".whl")):
            checksum = ""

            # Case 1: checksum inside the href fragment
            if parsed.fragment.startswith("sha256="):
                checksum: str = parsed.fragment.split("=", 1)[1]

            # Case 2: checksum inside integrity attribute
            elif integrity := a_tag.get("integrity"):  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
                if integrity.startswith("sha256-"):  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
                    checksum: str = integrity.split("sha256-", 1)[1]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportAttributeAccessIssue]

            absolute_link = safe_urljoin(url, href.split("#", maxsplit=1)[0])

            if filename.endswith(".json"):
                artifacts.append(
                    VariantJson(name=filename, link=absolute_link, checksum=checksum)  # pyright: ignore[reportUnknownArgumentType]
                )

            else:
                artifacts.append(
                    VariantWheel(name=filename, link=absolute_link, checksum=checksum)  # pyright: ignore[reportUnknownArgumentType]
                )

    return artifacts


def pkg_name_to_version(pkg_name: str) -> Version:
    return Version(pkg_name.split("-", maxsplit=2)[1])


def filter_provider_wheels(
    artifacts: list[VariantWheel | VariantJson],
) -> list[VariantWheel]:
    wheels: list[VariantWheel] = []
    for artifact in artifacts:
        if not isinstance(artifact, VariantWheel):
            continue
        try:
            variant_alias = artifact.variant_alias
        except ValueError:
            logger.warning(
                "Ignoring `%s`: invalid provider wheel filename.", artifact.name
            )
            continue
        if variant_alias:
            logger.warning(
                "Ignoring `%s`: provider wheels must not have a variant label.",
                artifact.name,
            )
            continue
        wheels.append(artifact)
    return wheels


def collect_all_links(pkgconfig: PkgConfig) -> list[VariantWheel | VariantJson]:
    artifacts = fetch_links(safe_urljoin(pkgconfig.registry, pkgconfig.name + "/"))
    if pkgconfig.is_provider:
        artifacts = list(filter_provider_wheels(artifacts))

    registry_versions: set[Version] = {
        pkg_name_to_version(artifact.name)
        for artifact in artifacts
        if isinstance(artifact, VariantWheel)
    }

    # Also add the packages published on PyPI
    with contextlib.suppress(requests.exceptions.HTTPError):
        pypi_artifacts = fetch_links(
            safe_urljoin("https://pypi.org/simple", pkgconfig.name + "/")
        )
        if pkgconfig.is_provider:
            pypi_artifacts = list(filter_provider_wheels(pypi_artifacts))
        for artifact in pypi_artifacts:
            if isinstance(artifact, VariantJson):
                raise TypeError(
                    f"Unexpected Object found on pypi.org `{artifact.name}`"
                )

            if pkg_name_to_version(artifact.name) in registry_versions:
                continue

            artifacts.append(artifact)

    return artifacts


def download_json(url: str) -> dict[str, Any]:
    # Fetch the JSON content from the URL
    response = requests.get(url, timeout=10)
    response.raise_for_status()  # Ensure we notice bad responses

    data = response.json()

    VARIANT_VALIDATOR.validate(data)
    return data


def load_variant_json(url: str, pkg_cfg: PkgConfig) -> dict[str, Any]:
    parsed_url = urlparse(url)

    variant_f = BUILD_DIR / pkg_cfg.name / unquote(Path(parsed_url.path).name)
    if not variant_f.exists():
        data = download_json(url)
        variant_f.parent.mkdir(exist_ok=True, parents=True)
        with variant_f.open(mode="w") as f:
            json.dump(data, f, sort_keys=True, indent=4)
        return data

    try:
        with variant_f.open(mode="r") as f:
            data = json.load(f)
        VARIANT_VALIDATOR.validate(data)
    except (jsonschema_rs.ValidationError, json.JSONDecodeError):
        variant_f.unlink()
        raise

    return data


def generate_project_index(pkg_config: PkgConfig) -> bool:
    """Build a package page, returning False when no eligible wheels remain."""
    logger.info("Processing `%s` ...", pkg_config.name)
    project_dir = BUILD_DIR / pkg_config.name
    # A previously generated page must not keep an ignored package visible.
    (project_dir / "index.html").unlink(missing_ok=True)
    # Load template
    current_dir = Path(__file__).parent
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(current_dir / "templates"),
        autoescape=True,
    )
    template = jinja_env.get_template("project_page.j2")

    artifacts = collect_all_links(pkg_config)

    variants_json_files = sorted(
        [artifact for artifact in artifacts if isinstance(artifact, VariantJson)],
        key=lambda x: x.name,
    )

    variant_configs: dict[str, dict[str, list[str]]] = {}
    accepted_json_files: list[VariantJson] = []
    rejected_versions: set[str] = set()

    for vjson_f in variants_json_files:
        if vjson_f.version in variant_configs:
            raise ValueError(
                f"Variant JSON file for version `{vjson_f.version}` and package "
                f"{pkg_config.name} already exists."
            )

        try:
            data = load_variant_json(vjson_f.link, pkg_cfg=pkg_config)
        except (
            jsonschema_rs.ValidationError,
            json.JSONDecodeError,
            requests.exceptions.JSONDecodeError,
        ) as exc:
            logger.warning(
                "Ignoring `%s`: incompatible with v0.2.0 (%s).",
                vjson_f.name,
                str(exc).splitlines()[0],
            )
            rejected_versions.add(vjson_f.version)
            continue

        variant_configs[vjson_f.version] = {
            variant_alias: [
                f"{ns} :: {vfeat_name} :: {vfeat_val}"
                for ns, vfeat_data in variant_data.items()
                for vfeat_name, vfeat_values in vfeat_data.items()
                for vfeat_val in vfeat_values
            ]
            for variant_alias, variant_data in data["variants"].items()
        }
        accepted_json_files.append(VariantJson.from_file(project_dir / vjson_f.name))

    def augment_wheel_variant(artifact: VariantWheel) -> VariantWheel:
        return VariantWheel(
            name=artifact.name,
            link=artifact.link,
            checksum=artifact.checksum,
            vprops=(
                variant_configs[artifact.version][artifact.variant_alias]
                if artifact.variant_alias
                else []
            ),
        )

    wheel_files: list[VariantWheel] = []
    for artifact in artifacts:
        if not isinstance(artifact, VariantWheel):
            continue
        if artifact.version in rejected_versions:
            continue
        if artifact.variant_alias:
            if artifact.version not in variant_configs:
                continue
            if artifact.variant_alias not in variant_configs[artifact.version]:
                logger.warning(
                    "Ignoring `%s`: variant label is missing from metadata.",
                    artifact.name,
                )
                continue
            wheel_files.append(augment_wheel_variant(artifact))
        else:
            wheel_files.append(artifact)

    wheel_files.sort(
        key=lambda x: (Version(x.name.split("-", maxsplit=2)[1]), x.name),
        reverse=True,
    )

    if pkg_config.is_provider and not wheel_files:
        logger.warning(
            "Ignoring `%s`: no ordinary provider wheels found.", pkg_config.name
        )
        return False

    if not pkg_config.is_provider and not any(
        artifact.variant_alias for artifact in wheel_files
    ):
        logger.warning(
            "Ignoring `%s`: no compatible variant releases for v0.2.0.",
            pkg_config.name,
        )
        return False

    # Render template
    output = template.render(
        project_name=pkg_config.name,
        variants_json_files=sorted(
            accepted_json_files,
            key=lambda vf: (Version(vf.name.split("-", maxsplit=2)[1]), vf.name),
            reverse=True,
        ),
        wheel_variant_files=wheel_files,
        build_date=BUILD_DATE,
    )

    project_dir.mkdir(exist_ok=True, parents=True)

    with (project_dir / "index.html").open(mode="w") as f:
        f.write(output)

    return True

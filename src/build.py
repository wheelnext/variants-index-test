from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import jinja2
import requests
from bs4 import BeautifulSoup

BUILD_DIR = Path("build")
BUILD_DATE = datetime.datetime.now(tz=ZoneInfo("America/New_York")).strftime(
    "%Y-%m-%d %H:%M"
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


@dataclass(frozen=True)
class PkgConfig:
    name: str
    registry: str


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


@dataclass(frozen=True)
class VariantWheel(Artifact):
    vprops: list[str] | None = None

    @property
    def version(self) -> str:
        return self.re_match(VARIANT_WHL_FILE_REGEX).group("ver")

    @property
    def variant_alias(self) -> str:
        return self.re_match(VARIANT_WHL_FILE_REGEX).group("variant_label")


def generate_main_index(packages: list[str]) -> None:
    # Load template
    current_dir = Path(__file__).parent
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(current_dir / "templates"),
        autoescape=True,
    )
    template = jinja_env.get_template("main_page.j2")

    # Render template
    output = template.render(
        directories=sorted(packages),
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

    # Find all <a> tags with href attribute ending with .json or .whl
    artifacts: list[VariantWheel | VariantJson] = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.endswith(".json") or href.endswith(".whl"):
            absolute_link = urljoin(url, href)
            checksum = (
                a_tag.get("data-dist-info-metadata")
                or a_tag.get("data-core-metadata")
                or ""
            )
            filename: str = a_tag.text

            if filename.endswith(".json"):
                artifacts.append(
                    VariantJson(name=filename, link=absolute_link, checksum=checksum)
                )

            elif filename.endswith(".whl"):
                artifacts.append(
                    VariantWheel(name=filename, link=absolute_link, checksum=checksum)
                )

            else:
                raise ValueError(f"Unknown file extension: `{filename}` ...")

    return artifacts


def download_json(url: str) -> dict[str, Any]:
    # Fetch the JSON content from the URL
    response = requests.get(url, timeout=10)
    response.raise_for_status()  # Ensure we notice bad responses

    return response.json()


def generate_project_index(pkg_config: PkgConfig) -> None:
    logger.info("Processing `%s` ...", pkg_config.name)
    # Load template
    current_dir = Path(__file__).parent
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(current_dir / "templates"),
        autoescape=True,
    )
    template = jinja_env.get_template("project_page.j2")

    artifacts = fetch_links(urljoin(pkg_config.registry, pkg_config.name))

    variants_json_files = sorted(
        [artifact for artifact in artifacts if isinstance(artifact, VariantJson)],
        key=lambda x: x.name,
    )

    variant_configs: dict[str, dict[str, list[str]]] = {}

    for vjson_f in variants_json_files:
        if vjson_f.version in variant_configs:
            raise ValueError(
                f"Variant JSON file for version `{vjson_f.version}` and package "
                f"{pkg_config.name} already exists."
            )
        data = download_json(vjson_f.link)
        if (variant_info := data.get("variants", None)) is None:
            raise ValueError("Invalid Variant JSON file format ...")

        variant_configs[vjson_f.version] = {
            variant_alias: [
                f"{ns} :: {vfeat_name} :: {vfeat_val}"
                for ns, vfeat_data in variant_data.items()
                for vfeat_name, vfeat_values in vfeat_data.items()
                for vfeat_val in vfeat_values
            ]
            for variant_alias, variant_data in variant_info.items()
        }

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

    wheel_variant_files = sorted(
        [
            augment_wheel_variant(artifact)
            for artifact in artifacts
            if isinstance(artifact, VariantWheel)
        ],
        key=lambda x: x.name,
    )

    # Render template
    output = template.render(
        project_name=pkg_config.name,
        variants_json_files=variants_json_files,
        wheel_variant_files=wheel_variant_files,
        build_date=BUILD_DATE,
    )

    project_dir = BUILD_DIR / pkg_config.name
    project_dir.mkdir(exist_ok=True, parents=True)

    with (project_dir / "index.html").open(mode="w") as f:
        f.write(output)

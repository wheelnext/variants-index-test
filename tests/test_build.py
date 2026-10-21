from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from src.build import VariantJson
from src.build import VariantWheel
from src.build import fetch_links

INDEX_URL = "https://example.org/simple/vllm-ascend/"
WHEEL_NAME = "vllm_ascend-0.1.dev1+g79d26b7c1-cp312-cp312-manylinux_2_24_x86_64-a5.whl"


def fetch_html(html: str) -> list[VariantWheel | VariantJson]:
    with patch("src.build.requests.get") as get:
        get.return_value.text = html
        return fetch_links(INDEX_URL)


class FetchLinksTests(TestCase):
    def test_abbreviated_wheel_label(self) -> None:
        artifacts = fetch_html(
            f'<a href="{WHEEL_NAME}">'
            "vllm_ascend-0.1.dev1+g79d26b7c1-cp312-cp312-man..&gt;</a>"
        )

        assert len(artifacts) == 1
        wheel = artifacts[0]
        assert isinstance(wheel, VariantWheel)
        assert wheel.name == WHEEL_NAME
        assert wheel.link == INDEX_URL + WHEEL_NAME
        assert wheel.version == "0.1.dev1+g79d26b7c1"
        assert wheel.variant_alias == "a5"
        assert wheel.checksum == ""

    def test_encoded_filename_preserves_download_url_and_checksum(self) -> None:
        download_url = (
            "https://downloads.example.org/"
            + WHEEL_NAME.replace("+", "%2B")
            + "?download=1"
        )
        artifacts = fetch_html(
            f'<a href="{download_url}#sha256=abc123" '
            'integrity="sha256-ignored">Download wheel</a>'
        )

        assert artifacts == [
            VariantWheel(name=WHEEL_NAME, link=download_url, checksum="abc123")
        ]

    def test_json_filename_and_integrity_come_from_link_attributes(self) -> None:
        filename = "vllm_ascend-0.1.dev1+g79d26b7c1-variants.json"
        artifacts = fetch_html(
            f'<a href="/metadata/{filename}" integrity="sha256-def456">'
            "misleading-label.whl</a>"
        )

        assert artifacts == [
            VariantJson(
                name=filename,
                link=f"https://example.org/metadata/{filename}",
                checksum="def456",
            )
        ]

    def test_unsupported_urls_are_ignored_even_with_wheel_labels(self) -> None:
        artifacts = fetch_html(
            '<a href="../">Parent directory</a>'
            '<a href="archive.tar.gz">pretend.whl</a>'
            '<a href="directory.whl/">directory.whl</a>'
            '<a href="download?filename=pretend.whl">pretend.whl</a>'
            '<a href="notes.txt#pretend.whl">pretend.whl</a>'
            "<a>missing-href.whl</a>"
        )

        assert artifacts == []

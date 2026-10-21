from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from unittest.mock import Mock
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup

import main
from src.build import VariantJson
from src.build import VariantWheel
from src.build import collect_all_links
from src.build import generate_main_index
from src.build import generate_project_index
from tests.helpers import JSON_NAME
from tests.helpers import PKG_CONFIG
from tests.helpers import BuildTestCase
from tests.helpers import variant_metadata


def metadata_artifact(version: str = "1.0") -> VariantJson:
    name = f"demo-{version}-variants.json"
    return VariantJson(name=name, link=f"{PKG_CONFIG.registry}demo/{name}", checksum="")


def wheel_artifact(version: str = "1.0", label: str | None = "cu128") -> VariantWheel:
    suffix = f"-{label}" if label else ""
    name = f"demo-{version}-py3-none-any{suffix}.whl"
    return VariantWheel(
        name=name, link=f"{PKG_CONFIG.registry}demo/{name}", checksum="abc123"
    )


def artifact_names(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [anchor.get_text() for anchor in soup.select("li a")]


class ProjectIndexTests(BuildTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.collect = self.enterContext(patch("src.build.collect_all_links"))
        self.collect.return_value = [metadata_artifact(), wheel_artifact()]
        self.mock_metadata(variant_metadata())

    def project_html(self) -> str:
        return (self.build_dir / PKG_CONFIG.name / "index.html").read_text()

    def test_valid_release_is_published_with_properties_and_checksums(self) -> None:
        assert generate_project_index(PKG_CONFIG) is True
        html = self.project_html()
        assert artifact_names(html) == [JSON_NAME, wheel_artifact().name]
        assert "nvidia :: cuda_version_lower_bound :: 12.8" in html
        assert f"{wheel_artifact().link}#sha256=abc123" in html
        cached = self.build_dir / PKG_CONFIG.name / JSON_NAME
        checksum = hashlib.sha256(cached.read_bytes()).hexdigest()
        assert f"{JSON_NAME}#sha256={checksum}" in html

    def test_mixed_releases_keep_valid_versions_and_ignore_invalid_ones(self) -> None:
        self.collect.return_value = [
            metadata_artifact("1.0"),
            wheel_artifact("1.0"),
            wheel_artifact("1.0", None),
            metadata_artifact("2.0"),
            wheel_artifact("2.0"),
            metadata_artifact("10.0"),
            wheel_artifact("10.0"),
            wheel_artifact("3.0", None),
            wheel_artifact("4.0"),  # No metadata for this variant release.
        ]
        legacy = variant_metadata() | {"static-properties": {}}
        self.get.return_value.json.side_effect = [
            legacy,
            variant_metadata(),
            variant_metadata(),
        ]

        with self.assertLogs("src.build", level="WARNING") as logs:
            assert generate_project_index(PKG_CONFIG) is True

        assert "Ignoring `demo-1.0-variants.json`" in "\n".join(logs.output)
        assert artifact_names(self.project_html()) == [
            "demo-10.0-variants.json",
            "demo-2.0-variants.json",
            wheel_artifact("10.0").name,
            wheel_artifact("3.0", None).name,
            wheel_artifact("2.0").name,
        ]
        assert not (self.build_dir / PKG_CONFIG.name / JSON_NAME).exists()

    def test_legacy_only_package_is_ignored_even_with_pypi_wheels(self) -> None:
        self.collect.return_value.append(wheel_artifact("2.0", None))
        self.mock_metadata(variant_metadata() | {"static-properties": {}})
        with self.assertLogs("src.build", level="WARNING") as logs:
            assert generate_project_index(PKG_CONFIG) is False
        assert "Ignoring `demo`: no compatible variant releases" in "\n".join(
            logs.output
        )
        assert not (self.build_dir / PKG_CONFIG.name / "index.html").exists()
        generate_main_index([PKG_CONFIG.name])
        assert PKG_CONFIG.name not in artifact_names(
            (self.build_dir / "index.html").read_text()
        )

    def test_packages_without_usable_variant_wheels_are_ignored(self) -> None:
        cases = [
            [],
            [wheel_artifact("1.0", None)],
            [wheel_artifact()],
            [metadata_artifact()],
            [metadata_artifact(), wheel_artifact("2.0", None)],
            [metadata_artifact(), wheel_artifact(label="missing")],
        ]
        for artifacts in cases:
            with self.subTest(artifacts=artifacts):
                self.collect.return_value = artifacts
                with self.assertLogs("src.build", level="WARNING"):
                    assert generate_project_index(PKG_CONFIG) is False
                generate_main_index([PKG_CONFIG.name])
                assert artifact_names((self.build_dir / "index.html").read_text()) == []

    def test_malformed_download_is_skipped_and_other_releases_continue(self) -> None:
        self.collect.return_value.extend(
            [metadata_artifact("2.0"), wheel_artifact("2.0")]
        )
        self.get.return_value.json.side_effect = [
            requests.exceptions.JSONDecodeError("Expecting value", "bad", 0),
            variant_metadata(),
        ]
        with self.assertLogs("src.build", level="WARNING"):
            assert generate_project_index(PKG_CONFIG) is True
        assert artifact_names(self.project_html()) == [
            metadata_artifact("2.0").name,
            wheel_artifact("2.0").name,
        ]

    def test_invalid_cached_release_cannot_preserve_stale_project_page(self) -> None:
        cached = self.cache_metadata(json.dumps({}))
        (cached.parent / "index.html").write_text("stale page")
        with self.assertLogs("src.build", level="WARNING"):
            assert generate_project_index(PKG_CONFIG) is False
        assert not cached.exists()
        assert not (cached.parent / "index.html").exists()
        self.get.assert_not_called()
        generate_main_index([PKG_CONFIG.name])
        assert artifact_names((self.build_dir / "index.html").read_text()) == []

    def test_cached_invalid_json_is_ignored_without_aborting_build(self) -> None:
        self.cache_metadata("not json")
        with self.assertLogs("src.build", level="WARNING"):
            assert generate_project_index(PKG_CONFIG) is False

    def test_unlisted_cached_metadata_is_not_published(self) -> None:
        for version in ("0.5", "0.9"):
            self.cache_metadata(
                json.dumps(variant_metadata()), metadata_artifact(version).name
            )
        assert generate_project_index(PKG_CONFIG) is True
        assert artifact_names(self.project_html()) == [JSON_NAME, wheel_artifact().name]

    def test_unknown_wheel_label_is_skipped_without_losing_valid_wheel(self) -> None:
        self.collect.return_value.append(wheel_artifact(label="missing"))
        with self.assertLogs("src.build", level="WARNING"):
            assert generate_project_index(PKG_CONFIG) is True
        assert artifact_names(self.project_html()) == [JSON_NAME, wheel_artifact().name]

    def test_null_variant_properties_are_preserved_without_conversion(self) -> None:
        for label in ("null", "00000000"):
            with self.subTest(label=label):
                data = variant_metadata()
                data["variants"] = {label: {}}
                self.cache_metadata(json.dumps(data))
                self.collect.return_value = [
                    metadata_artifact(),
                    wheel_artifact(label=label),
                ]
                assert generate_project_index(PKG_CONFIG) is True
                assert artifact_names(self.project_html()) == [
                    JSON_NAME,
                    wheel_artifact(label=label).name,
                ]
                cached = self.build_dir / PKG_CONFIG.name / JSON_NAME
                assert json.loads(cached.read_text()) == data

    def test_duplicate_metadata_for_a_release_remains_an_error(self) -> None:
        self.collect.return_value.append(metadata_artifact())
        with self.assertRaisesRegex(ValueError, "already exists"):
            generate_project_index(PKG_CONFIG)

    def test_download_failure_is_not_silently_ignored(self) -> None:
        self.get.side_effect = requests.HTTPError("server error")
        with self.assertRaises(requests.HTTPError):
            generate_project_index(PKG_CONFIG)


class MainIndexTests(BuildTestCase):
    def test_main_index_requires_a_page_and_sorts_only_requested_packages(self) -> None:
        for package in ("zulu", "alpha", "unrequested"):
            directory = self.build_dir / package
            directory.mkdir()
            (directory / "index.html").write_text("page")
        (self.build_dir / "cached-only").mkdir()
        generate_main_index(["zulu", "cached-only", "missing", "alpha"])
        html = (self.build_dir / "index.html").read_text()
        assert artifact_names(html) == ["alpha", "zulu"]
        assert "v0.2.0" in html
        for unsupported in ("v0.0.1", "v0.0.2", "v0.0.3"):
            assert unsupported not in html

    def test_empty_index_has_no_packages(self) -> None:
        generate_main_index([])
        assert artifact_names((self.build_dir / "index.html").read_text()) == []

    def test_entrypoint_builds_only_compatible_packages_from_registry_html(
        self,
    ) -> None:
        config = self.build_dir.parent / "index.toml"
        config.write_text(
            '[[index]]\nregistry = "https://example.org/simple/"\n'
            'packages = ["accepted", "vllm", "ordinary"]\n'
            '[[provider-index]]\nregistry = "https://example.org/simple/"\n'
            'packages = ["provider"]\n'
        )
        responses = {}
        for package in ("accepted", "vllm", "ordinary", "provider"):
            label = "" if package == "provider" else "-cu128"
            wheel = f"{package}-1.0-py3-none-any{label}.whl"
            metadata = f"{package}-1.0-variants.json"
            registry = f"https://example.org/simple/{package}/"
            html = (
                f'<a href="{wheel}">wheel</a><a href="{metadata}">metadata</a>'
                if package != "ordinary"
                else ""
            )
            responses[registry] = Mock(text=html)
            data = variant_metadata()
            if package == "vllm":
                data["providers"]["nvidia"]["install-time"] = True
            if package != "provider":
                responses[registry + metadata] = Mock(**{"json.return_value": data})
            responses[f"https://pypi.org/simple/{package}/"] = Mock(
                text=f'<a href="{package}-2.0-py3-none-any.whl">ordinary wheel</a>'
            )
        self.get.side_effect = lambda url, timeout: responses[url]

        with (
            patch("main.CONFIG_FILEPATH", config),
            patch("main.BUILD_DIR", self.build_dir),
            self.assertLogs("src.build", level="WARNING") as logs,
        ):
            main.main()

        html = (self.build_dir / "index.html").read_text()
        assert artifact_names(html) == ["accepted", "provider"]
        assert "vllm" not in html
        assert "Ignoring `vllm`" in "\n".join(logs.output)
        accepted = (self.build_dir / "accepted" / "index.html").read_text()
        assert artifact_names(accepted) == [
            "accepted-1.0-variants.json",
            "accepted-2.0-py3-none-any.whl",
            "accepted-1.0-py3-none-any-cu128.whl",
        ]
        assert not (self.build_dir / "vllm" / "index.html").exists()
        assert not (self.build_dir / "ordinary" / "index.html").exists()
        provider = (self.build_dir / "provider" / "index.html").read_text()
        assert artifact_names(provider) == [
            "provider-2.0-py3-none-any.whl",
            "provider-1.0-py3-none-any.whl",
        ]
        assert list((self.build_dir / "provider").glob("*.json")) == []

    def test_provider_only_configuration_builds_without_variant_metadata(self) -> None:
        config = self.build_dir.parent / "index.toml"
        config.write_text(
            '[[provider-index]]\nname = "quansight"\n'
            'maintainers = [{ name = "Maintainer", email = "dev@example.org" }]\n'
            'registry = "https://example.org/simple/"\npackages = ["demo"]\n'
        )
        self.get.side_effect = None
        self.get.return_value.text = (
            '<a href="demo-1.0-py3-none-any.whl">wheel</a>'
            '<a href="demo-1.0-variants.json">metadata</a>'
        )
        with (
            patch("main.CONFIG_FILEPATH", config),
            patch("main.BUILD_DIR", self.build_dir),
            patch("src.build.load_variant_json") as load,
        ):
            main.main()
        load.assert_not_called()
        assert artifact_names((self.build_dir / "index.html").read_text()) == ["demo"]
        assert artifact_names((self.build_dir / "demo" / "index.html").read_text()) == [
            "demo-1.0-py3-none-any.whl"
        ]
        assert [call.args[0] for call in self.get.call_args_list] == [
            "https://example.org/simple/demo/",
            "https://pypi.org/simple/demo/",
        ]

    def test_duplicate_packages_are_rejected_within_and_across_index_types(
        self,
    ) -> None:
        config = self.build_dir.parent / "index.toml"
        sentinel = self.build_dir / "index.html"
        sentinel.write_text("previous build")
        for sections in (
            ("index", "index"),
            ("provider-index", "provider-index"),
            ("index", "provider-index"),
        ):
            with self.subTest(sections=sections):
                config.write_text(
                    "\n".join(
                        f'[[{section}]]\nregistry = "https://example.org/{i}/"\n'
                        'packages = ["demo"]\n'
                        for i, section in enumerate(sections)
                    )
                )
                with (
                    patch("main.CONFIG_FILEPATH", config),
                    patch("main.BUILD_DIR", self.build_dir),
                    self.assertRaisesRegex(ValueError, "declared for two indexes"),
                ):
                    main.main()
                assert sentinel.read_text() == "previous build"
        self.get.assert_not_called()


class ProviderIndexTests(BuildTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = replace(PKG_CONFIG, is_provider=True)
        self.fetch = self.enterContext(patch("src.build.fetch_links"))
        self.load = self.enterContext(
            patch(
                "src.build.load_variant_json",
                side_effect=AssertionError("Metadata read"),
            )
        )
        self.validator = self.enterContext(patch("src.build.VARIANT_VALIDATOR"))

    def test_provider_page_lists_only_ordinary_wheels_without_metadata_access(
        self,
    ) -> None:
        build_wheel = replace(
            wheel_artifact(label=None), name="demo-1.0-1-py3-none-any.whl"
        )
        self.fetch.side_effect = [
            [
                metadata_artifact(),
                replace(metadata_artifact(), name="unrelated.json"),
                wheel_artifact("2.0", None),
                wheel_artifact("10.0", None),
                build_wheel,
            ],
            [],
        ]
        cached = self.cache_metadata("not valid JSON")

        assert generate_project_index(self.config) is True
        self.load.assert_not_called()
        self.validator.validate.assert_not_called()
        self.get.assert_not_called()
        assert cached.read_text() == "not valid JSON"
        html = (cached.parent / "index.html").read_text()
        assert artifact_names(html) == [
            wheel_artifact("10.0", None).name,
            wheel_artifact("2.0", None).name,
            build_wheel.name,
        ]
        assert f"{wheel_artifact('10.0', None).link}#sha256=abc123" in html
        generate_main_index([self.config.name])
        assert artifact_names((self.build_dir / "index.html").read_text()) == ["demo"]

    def test_labeled_provider_wheels_are_warned_about_and_skipped(self) -> None:
        rejected = [
            wheel_artifact(label=label) for label in ("cu128", "null", "00000000")
        ]
        rejected.append(
            replace(wheel_artifact(), name="demo-1.0-1-py3-none-any-cu128.whl")
        )
        ordinary = wheel_artifact(label=None)
        self.fetch.side_effect = [[*rejected, ordinary], []]

        with self.assertLogs("src.build", level="WARNING") as logs:
            assert generate_project_index(self.config) is True

        assert len(logs.output) == len(rejected)
        for wheel in rejected:
            assert any(
                wheel.name in message and "variant label" in message
                for message in logs.output
            )
        assert artifact_names((self.build_dir / "demo" / "index.html").read_text()) == [
            ordinary.name
        ]

    def test_invalid_provider_wheel_filename_is_warned_about_and_skipped(self) -> None:
        invalid = replace(wheel_artifact(), name="invalid.whl")
        self.fetch.side_effect = [[invalid, wheel_artifact(label=None)], []]
        with self.assertLogs("src.build", level="WARNING") as logs:
            assert generate_project_index(self.config) is True
        assert "invalid provider wheel filename" in logs.output[0]
        assert artifact_names((self.build_dir / "demo" / "index.html").read_text()) == [
            wheel_artifact(label=None).name
        ]

    def test_provider_pypi_fallback_is_filtered_before_selecting_versions(self) -> None:
        registry_wheel = wheel_artifact(label=None)
        same_version = replace(registry_wheel, link="https://pypi.org/same.whl")
        fallback = replace(
            wheel_artifact("2.0", None), link="https://pypi.org/extra.whl"
        )
        self.fetch.side_effect = [
            [registry_wheel, wheel_artifact("2.0"), metadata_artifact()],
            [same_version, fallback, wheel_artifact("3.0"), metadata_artifact()],
        ]
        with self.assertLogs("src.build", level="WARNING") as logs:
            assert collect_all_links(self.config) == [registry_wheel, fallback]
        assert len(logs.output) == 2
        self.get.assert_not_called()
        self.load.assert_not_called()

    def test_provider_with_no_ordinary_wheels_is_ignored_and_stale_page_removed(
        self,
    ) -> None:
        for artifacts in ([], [metadata_artifact()], [wheel_artifact()]):
            with self.subTest(artifacts=artifacts):
                project_dir = self.build_dir / "demo"
                project_dir.mkdir(exist_ok=True)
                (project_dir / "index.html").write_text("stale page")
                self.fetch.side_effect = [artifacts, []]
                with self.assertLogs("src.build", level="WARNING") as logs:
                    assert generate_project_index(self.config) is False
                assert any(
                    "no ordinary provider wheels" in line for line in logs.output
                )
                assert not (project_dir / "index.html").exists()
                generate_main_index([self.config.name])
                assert artifact_names((self.build_dir / "index.html").read_text()) == []
        self.load.assert_not_called()
        self.validator.validate.assert_not_called()

    def test_missing_pypi_project_does_not_hide_provider_wheels(self) -> None:
        self.fetch.side_effect = [
            [wheel_artifact(label=None)],
            requests.HTTPError("404"),
        ]
        assert generate_project_index(self.config) is True


class CollectLinksTests(BuildTestCase):
    def test_collection_has_no_legacy_null_variant_filter(self) -> None:
        wheel = wheel_artifact(label="00000000")
        with patch("src.build.fetch_links", side_effect=[[wheel], []]):
            assert collect_all_links(PKG_CONFIG) == [wheel]

    def test_pypi_wheels_only_supplement_versions_not_in_variant_registry(self) -> None:
        variant_wheel = wheel_artifact()
        fallback = wheel_artifact("2.0", None)
        with patch(
            "src.build.fetch_links",
            side_effect=[
                [metadata_artifact(), variant_wheel],
                [wheel_artifact(label=None), fallback],
            ],
        ):
            assert collect_all_links(PKG_CONFIG) == [
                metadata_artifact(),
                variant_wheel,
                fallback,
            ]

    def test_missing_pypi_project_does_not_hide_variant_releases(self) -> None:
        with patch(
            "src.build.fetch_links",
            side_effect=[
                [metadata_artifact(), wheel_artifact()],
                requests.HTTPError("404"),
            ],
        ):
            assert collect_all_links(PKG_CONFIG) == [
                metadata_artifact(),
                wheel_artifact(),
            ]

from __future__ import annotations

import json
from copy import deepcopy

import jsonschema_rs
import requests

from src.build import download_json
from src.build import load_variant_json
from tests.helpers import JSON_NAME
from tests.helpers import JSON_URL
from tests.helpers import PKG_CONFIG
from tests.helpers import BuildTestCase
from tests.helpers import variant_metadata


class MetadataValidationTests(BuildTestCase):
    def test_current_metadata_is_returned_unchanged_without_fetching_schema(
        self,
    ) -> None:
        data = variant_metadata()
        original = deepcopy(data)
        self.mock_metadata(data)

        assert download_json(JSON_URL) == original
        assert data == original
        self.get.assert_called_once_with(JSON_URL, timeout=10)

    def test_all_current_provider_types_are_accepted(self) -> None:
        providers = [
            {"requires": ["example-provider"], "optional": True},
            {"builtin": "abi_dependency"},
            {
                "static-properties": {"cuda_version_lower_bound": ["12.8"]},
                "feature-order": ["cuda_version_lower_bound"],
            },
        ]
        for provider in providers:
            with self.subTest(provider=provider):
                data = variant_metadata()
                data["providers"]["nvidia"] = provider
                self.mock_metadata(data)
                assert download_json(JSON_URL) == data

    def test_schema_url_does_not_select_validation_or_trigger_conversion(self) -> None:
        for schema in (
            "https://example.org/custom-schema.json",
            "https://variants-schema.wheelnext.dev/",
            "https://variants-schema.wheelnext.dev/v0.0.2.json",
        ):
            with self.subTest(schema=schema):
                data = variant_metadata()
                data["$schema"] = schema
                self.mock_metadata(data)
                assert download_json(JSON_URL) == data
                assert data["$schema"] == schema
        assert self.get.call_count == 3

    def test_legacy_metadata_is_rejected_without_conversion(self) -> None:
        for version in ("0.0.1", "0.0.2", "0.0.3"):
            with self.subTest(version=version):
                data = variant_metadata()
                data["$schema"] = (
                    f"https://variants-schema.wheelnext.dev/v{version}.json"
                )
                data["default-priorities"]["feature"] = {"nvidia": ["cuda_version"]}
                data["providers"]["nvidia"]["enable-if"] = "True"
                original = deepcopy(data)
                self.mock_metadata(data)
                with self.assertRaises(jsonschema_rs.ValidationError):
                    download_json(JSON_URL)
                assert data == original

    def test_each_required_field_is_enforced(self) -> None:
        for field in variant_metadata():
            with self.subTest(field=field):
                data = variant_metadata()
                del data[field]
                self.mock_metadata(data)
                with self.assertRaises(jsonschema_rs.ValidationError):
                    download_json(JSON_URL)

    def test_non_object_documents_are_rejected(self) -> None:
        for data in (None, [], "text", 42, True):
            with self.subTest(data=data):
                self.mock_metadata(data)
                with self.assertRaises(jsonschema_rs.ValidationError):
                    download_json(JSON_URL)

    def test_current_schema_constraints_are_enforced(self) -> None:
        invalid_fields = [
            {"$schema": None},
            {"static-properties": {}},
            {"default-priorities": {"namespace": []}},
            {"default-priorities": {"namespace": ["nvidia", "nvidia"]}},
            {"default-priorities": {"namespace": ["nvidia"], "property": {}}},
            {"providers": {"nvidia": {"install-time": True, "requires": ["plugin"]}}},
            {"providers": {"nvidia": {"plugin-use": "all", "requires": ["plugin"]}}},
            {"providers": {"nvidia": {"builtin": "unknown"}}},
            {"providers": {"nvidia": {"requires": [], "plugin-api": "plugin"}}},
            {
                "providers": {
                    "nvidia": {"requires": ["plugin"], "static-properties": {}}
                }
            },
            {"providers": {"nvidia": {}}},
            {"variants": None},
            {"variants": {"invalid-label": {}}},
            {"variants": {"cu128": {"nvidia": {"cuda_version": []}}}},
            {"variants": {"cu128": {"nvidia": {"cuda_version": ["12.8", "12.8"]}}}},
            {"variants": {"cu128": {"nvidia": {"cuda_version": [128]}}}},
        ]
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                self.mock_metadata(variant_metadata() | fields)
                with self.assertRaises(jsonschema_rs.ValidationError):
                    download_json(JSON_URL)

    def test_unrecognized_schema_url_cannot_bypass_validation(self) -> None:
        self.mock_metadata({"$schema": "https://example.org/anything.json"})
        with self.assertRaises(jsonschema_rs.ValidationError):
            download_json(JSON_URL)

    def test_invalid_download_is_not_cached(self) -> None:
        self.mock_metadata({})
        with self.assertRaises(jsonschema_rs.ValidationError):
            load_variant_json(JSON_URL, PKG_CONFIG)
        assert not (self.build_dir / PKG_CONFIG.name).exists()

    def test_valid_download_is_cached_and_reused_unchanged(self) -> None:
        data = variant_metadata()
        self.mock_metadata(data)
        assert load_variant_json(JSON_URL, PKG_CONFIG) == data
        cached = self.build_dir / PKG_CONFIG.name / JSON_NAME
        assert json.loads(cached.read_text()) == data
        assert load_variant_json(JSON_URL, PKG_CONFIG) == data
        self.get.assert_called_once_with(JSON_URL, timeout=10)

    def test_cached_metadata_is_validated_without_rewriting(self) -> None:
        data = variant_metadata()
        content = json.dumps(data, indent=2)
        cached = self.cache_metadata(content)
        assert load_variant_json(JSON_URL, PKG_CONFIG) == data
        assert cached.read_text() == content
        self.get.assert_not_called()

    def test_incompatible_cached_metadata_is_rejected_and_removed(self) -> None:
        cached = self.cache_metadata(
            json.dumps(variant_metadata() | {"variants": None})
        )
        with self.assertRaises(jsonschema_rs.ValidationError):
            load_variant_json(JSON_URL, PKG_CONFIG)
        assert not cached.exists()
        self.get.assert_not_called()

    def test_malformed_cached_json_is_rejected_and_removed(self) -> None:
        cached = self.cache_metadata("{invalid")
        with self.assertRaises(json.JSONDecodeError):
            load_variant_json(JSON_URL, PKG_CONFIG)
        assert not cached.exists()

    def test_encoded_metadata_filename_matches_local_artifact(self) -> None:
        data = variant_metadata()
        self.mock_metadata(data)
        url = JSON_URL.replace("1.0", "1.0%2Bcuda") + "?download=1"
        assert load_variant_json(url, PKG_CONFIG) == data
        assert (
            self.build_dir / PKG_CONFIG.name / "demo-1.0+cuda-variants.json"
        ).is_file()
        self.get.assert_called_once_with(url, timeout=10)

    def test_http_errors_are_not_treated_as_schema_incompatibility(self) -> None:
        self.get.side_effect = requests.HTTPError("server error")
        with self.assertRaises(requests.HTTPError):
            load_variant_json(JSON_URL, PKG_CONFIG)

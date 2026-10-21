from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from src.build import PkgConfig

SCHEMA_URL = "https://variants-schema.wheelnext.dev/peps/850/v0.2.0.json"
PKG_CONFIG = PkgConfig(name="demo", registry="https://example.org/simple/")
JSON_NAME = "demo-1.0-variants.json"
JSON_URL = f"{PKG_CONFIG.registry}{PKG_CONFIG.name}/{JSON_NAME}"


def variant_metadata() -> dict[str, Any]:
    return {
        "$schema": SCHEMA_URL,
        "default-priorities": {"namespace": ["nvidia"]},
        "providers": {
            "nvidia": {
                "requires": ["nvidia-variant-provider"],
                "plugin-api": "nvidia_variant_provider:Plugin",
            }
        },
        "variants": {"cu128": {"nvidia": {"cuda_version_lower_bound": ["12.8"]}}},
    }


class BuildTestCase(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.build_dir = Path(directory.name) / "build"
        self.build_dir.mkdir()
        self.enterContext(patch("src.build.BUILD_DIR", self.build_dir))
        self.get = self.enterContext(patch("src.build.requests.get"))
        self.get.side_effect = AssertionError("Unexpected network request")

    def mock_metadata(self, data: Any) -> None:
        self.get.side_effect = None
        self.get.return_value.json.return_value = data

    def cache_metadata(self, content: str, name: str = JSON_NAME) -> Path:
        cached = self.build_dir / PKG_CONFIG.name / name
        cached.parent.mkdir(exist_ok=True)
        cached.write_text(content)
        return cached

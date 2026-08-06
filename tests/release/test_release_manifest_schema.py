from __future__ import annotations

import json

from jsonschema import Draft202012Validator, FormatChecker

from .conftest import ROOT


def test_release_manifest_fixture_is_valid_and_schema_is_strict(release_inputs) -> None:
    manifest_path, _, manifest, _ = release_inputs()
    schema_path = ROOT / "contracts/gate6/release-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)
    assert manifest_path.is_file()


def test_schema_rejects_floating_images(release_inputs) -> None:
    _, _, manifest, _ = release_inputs()
    schema = json.loads(
        (ROOT / "contracts/gate6/release-manifest.schema.json").read_text(encoding="utf-8")
    )
    manifest["images"]["app"] = "registry.example.invalid/gbos/app:latest"

    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest)
    )

    assert any("does not match" in error.message for error in errors)

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from release_tools.manifest import (
    ManifestValidationError,
    load_manifest,
    validate_manifest,
    verify_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "fixtures" / "manifest.valid.json"


class ManifestValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_public_schema_is_versioned_and_fixture_is_valid(self) -> None:
        schema = json.loads(
            (ROOT / "schema" / "release-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(validate_manifest(self.manifest), [])

    def test_malformed_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"release":', encoding="utf-8")
            with self.assertRaises(ManifestValidationError) as context:
                load_manifest(path)
        self.assertIn("invalid JSON", str(context.exception))

    def test_malformed_manifest_reports_errors_without_crashing(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["artifacts"]["client"]["url"] = "https://[malformed"
        errors = validate_manifest(manifest)
        self.assertIn("artifacts.client.url: must be an HTTPS URL", errors)

    def test_missing_artifact_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        del manifest["artifacts"]["server"]
        errors = validate_manifest(manifest)
        self.assertIn("artifacts.server: is required", errors)

    def test_minimum_compatible_version_cannot_be_newer(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["minimumCompatibleVersions"]["client"] = "9.0.0"
        errors = validate_manifest(manifest)
        self.assertIn(
            "minimumCompatibleVersions.client: must not be newer than versions.client",
            errors,
        )

    def test_release_note_range_must_match_anchors(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["release"]["releaseNotes"]["range"]["from"] = "1.1.0"
        manifest["release"]["releaseNotes"]["range"]["to"] = "1.3.0"
        errors = validate_manifest(manifest)
        self.assertIn(
            "release.releaseNotes.range.from: must equal previousRelease.version",
            errors,
        )
        self.assertIn(
            "release.releaseNotes.range.to: must equal release.version",
            errors,
        )

    def test_artifact_checksum_and_size_are_verified(self) -> None:
        client_path = ROOT / "fixtures" / "artifacts" / "client.txt"
        self.assertEqual(verify_artifact(self.manifest, "client", client_path), [])

        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "client.txt"
            tampered.write_bytes(b"tampered artifact\n")
            errors = verify_artifact(self.manifest, "client", tampered)
        self.assertTrue(any("size mismatch" in error for error in errors))
        self.assertTrue(any("SHA-256 mismatch" in error for error in errors))

    def test_cli_validates_manifest_and_artifact(self) -> None:
        client_path = ROOT / "fixtures" / "artifacts" / "client.txt"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "release_tools.validate_manifest",
                str(MANIFEST_PATH),
                "--artifact",
                f"client={client_path}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("valid manifest", result.stdout)
        self.assertIn("verified artifact: client", result.stdout)


if __name__ == "__main__":
    unittest.main()

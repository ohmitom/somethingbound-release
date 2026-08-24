from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from release_tools.channel_manifest import (
    ChannelManifestValidationError,
    load_channel_manifest,
    validate_channel_manifest,
)
from release_tools.patch_notes import render_patch_notes
from release_tools.update_engine import (
    ArtifactVerificationError,
    AtomicInstallError,
    InstalledBuild,
    UpdateEngine,
    atomic_install,
    download_and_verify,
    update_available,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "fixtures" / "manifest.local-dev.json"
ARTIFACT_PATH = ROOT / "fixtures" / "artifacts" / "launcher-local-dev.txt"


class LauncherFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_channel_manifest(MANIFEST_PATH)

    def test_channel_identity_shape_and_schema_are_exact(self) -> None:
        self.assertEqual(
            set(self.manifest),
            {"channel", "version", "gitSha", "releasedAt", "notes", "artifacts"},
        )
        schema = json.loads(
            (ROOT / "schema" / "release-manifest.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["required"], ["channel", "version", "gitSha", "releasedAt", "notes", "artifacts"])
        self.assertEqual(set(schema["properties"]), set(self.manifest))

    def test_loader_rejects_bad_checksum_missing_identity_and_regression(self) -> None:
        bad_checksum = copy.deepcopy(self.manifest)
        bad_checksum["artifacts"][0]["sha256"] = "not-a-sha"
        self.assertTrue(any("sha256" in error for error in validate_channel_manifest(bad_checksum)))

        missing_identity = copy.deepcopy(self.manifest)
        del missing_identity["gitSha"]
        self.assertIn("gitSha: is required", validate_channel_manifest(missing_identity))

        with self.assertRaises(ChannelManifestValidationError) as context:
            load_channel_manifest(MANIFEST_PATH, installed_version="2.0.0")
        self.assertIn("must not regress below installed version 2.0.0", str(context.exception))

    def test_version_comparison_uses_version_and_git_identity(self) -> None:
        self.assertTrue(
            update_available(self.manifest, "1.0.0", "0" * 40)
        )
        self.assertTrue(
            update_available(self.manifest, "1.1.0", "0" * 40)
        )
        self.assertFalse(
            update_available(self.manifest, "1.1.0", self.manifest["gitSha"])
        )

    def test_download_verifies_checksum_before_install(self) -> None:
        artifact = self.manifest["artifacts"][0]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "downloaded.bin"
            downloaded = download_and_verify(
                artifact,
                destination,
                base_dir=MANIFEST_PATH.parent,
            )
            self.assertEqual(downloaded.read_bytes(), ARTIFACT_PATH.read_bytes())

            tampered = copy.deepcopy(artifact)
            tampered["sha256"] = "0" * 64
            with self.assertRaises(ArtifactVerificationError):
                download_and_verify(
                    tampered,
                    Path(directory) / "tampered.bin",
                    base_dir=MANIFEST_PATH.parent,
                )
            self.assertFalse((Path(directory) / "tampered.bin").exists())

    def test_atomic_install_keeps_previous_and_rolls_back_simulated_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "current"
            backup = root / "previous"
            source = root / "new"
            target.write_bytes(b"old build")
            source.write_bytes(b"new build")
            atomic_install(source, target, backup=backup)
            self.assertEqual(target.read_bytes(), b"new build")
            self.assertEqual(backup.read_bytes(), b"old build")

            source.write_bytes(b"failed build")
            with self.assertRaises(AtomicInstallError):
                atomic_install(
                    source,
                    target,
                    backup=backup,
                    failure_hook=lambda _phase: (_ for _ in ()).throw(RuntimeError("simulated")),
                )
            self.assertEqual(target.read_bytes(), b"new build")

    def test_engine_rolls_back_payload_and_state_on_simulated_swap_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fail_after_swap(_phase: str) -> None:
                raise RuntimeError("simulated launcher failure")

            engine = UpdateEngine(root, failure_hook=fail_after_swap)
            old = InstalledBuild("1.0.0", "0" * 40, "old-launcher.txt")
            engine.seed_installed(old)
            engine.active_path.write_bytes(b"old build")
            with self.assertRaises(AtomicInstallError):
                engine.update(
                    self.manifest,
                    installed=old,
                    base_dir=MANIFEST_PATH.parent,
                )
            self.assertEqual(engine.active_path.read_bytes(), b"old build")
            self.assertEqual(engine.read_installed(), old)

    def test_engine_updates_local_build_and_persists_recoverable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = UpdateEngine(root)
            old = InstalledBuild("1.0.0", "0" * 40, "old-launcher.txt")
            engine.seed_installed(old)
            engine.active_path.write_bytes(b"old build")
            result = engine.update(
                self.manifest,
                installed=old,
                base_dir=MANIFEST_PATH.parent,
            )
            self.assertTrue(result.updated)
            self.assertEqual(engine.active_path.read_bytes(), ARTIFACT_PATH.read_bytes())
            self.assertEqual(engine.previous_path.read_bytes(), b"old build")
            self.assertEqual(engine.read_installed().git_sha, self.manifest["gitSha"])

    def test_patch_notes_show_summary_exact_commits_and_running_sha(self) -> None:
        rendered = render_patch_notes(self.manifest, self.manifest["gitSha"])
        self.assertIn(self.manifest["notes"]["summary"], rendered)
        self.assertIn("Running Git SHA: " + self.manifest["gitSha"], rendered)
        self.assertIn("0123456 feat(launcher)", rendered)
        self.assertIn("PR #24", rendered)
        self.assertIn("aaaaaaa fix(updates)", rendered)


if __name__ == "__main__":
    unittest.main()

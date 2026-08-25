"""Tests for the launcher replacing its own executable."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release_tools.launcher_core import Launcher, LauncherError
from release_tools.self_update import (
    SelfUpdateError,
    can_replace,
    clean_retired,
    download_replacement,
    plan_self_update,
    retired_path,
    running_executable,
    staged_path,
    swap_in_place,
)
from release_tools.settings import LauncherSettings

SHA = "a" * 40


def manifest_for(path: Path, version: str) -> dict:
    data = path.read_bytes()
    return {
        "channel": "launcher",
        "version": version,
        "gitSha": SHA,
        "releasedAt": "2026-08-25T12:00:00Z",
        "notes": {"summary": f"Launcher {version}.", "commits": []},
        "artifacts": [
            {
                "name": path.name,
                "url": path.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        ],
    }


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)


class PlanTests(unittest.TestCase):
    def _manifest(self, version: str) -> dict:
        return {"version": version}

    def test_a_newer_published_launcher_is_offered(self) -> None:
        plan = plan_self_update(self._manifest("0.2.0"), "0.1.0", frozen=True)
        self.assertTrue(plan.available)
        self.assertEqual(plan.channel_version, "0.2.0")

    def test_the_same_or_older_launcher_is_not(self) -> None:
        self.assertFalse(plan_self_update(self._manifest("0.1.0"), "0.1.0", frozen=True).available)
        self.assertFalse(plan_self_update(self._manifest("0.0.9"), "0.1.0", frozen=True).available)

    def test_running_from_source_is_explained_not_failed(self) -> None:
        plan = plan_self_update(self._manifest("9.9.9"), "0.1.0", frozen=False)
        self.assertFalse(plan.available)
        self.assertIn("running from source", plan.reason)

    def test_running_from_source_has_no_executable_to_replace(self) -> None:
        self.assertIsNone(running_executable())


class SwapTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.exe = self.root / "SomethingBoundLauncher.exe"
        self.exe.write_text("old launcher", encoding="utf-8")

    def test_the_running_launcher_is_retired_and_replaced(self) -> None:
        staged = staged_path(self.exe)
        staged.write_text("new launcher", encoding="utf-8")

        retired = swap_in_place(staged, self.exe)

        self.assertEqual(self.exe.read_text(), "new launcher")
        self.assertEqual(retired.read_text(), "old launcher")
        self.assertFalse(staged.exists())

    def test_the_retired_copy_is_cleared_on_the_next_run(self) -> None:
        retired_path(self.exe).write_text("old launcher", encoding="utf-8")

        self.assertTrue(clean_retired(self.exe))
        self.assertFalse(retired_path(self.exe).exists())
        # Nothing to clear the second time, and that is not a failure.
        self.assertFalse(clean_retired(self.exe))

    def test_a_leftover_retired_copy_does_not_block_a_later_update(self) -> None:
        retired_path(self.exe).write_text("ancient", encoding="utf-8")
        staged = staged_path(self.exe)
        staged.write_text("new launcher", encoding="utf-8")

        swap_in_place(staged, self.exe)

        self.assertEqual(self.exe.read_text(), "new launcher")
        self.assertEqual(retired_path(self.exe).read_text(), "old launcher")

    def test_a_failed_install_puts_the_old_launcher_straight_back(self) -> None:
        staged = staged_path(self.exe)
        staged.write_text("new launcher", encoding="utf-8")

        real_replace = __import__("os").replace
        calls = {"count": 0}

        def flaky(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated failure installing the new launcher")
            return real_replace(source, destination)

        with mock.patch("release_tools.self_update.os.replace", flaky):
            with self.assertRaises(SelfUpdateError):
                swap_in_place(staged, self.exe)

        self.assertTrue(self.exe.exists())
        self.assertEqual(self.exe.read_text(), "old launcher")

    def test_a_missing_staged_file_is_refused_before_anything_moves(self) -> None:
        with self.assertRaises(SelfUpdateError):
            swap_in_place(staged_path(self.exe), self.exe)
        self.assertEqual(self.exe.read_text(), "old launcher")
        self.assertFalse(retired_path(self.exe).exists())

    def test_a_writable_directory_is_detected_and_leaves_nothing_behind(self) -> None:
        self.assertTrue(can_replace(self.exe))
        self.assertEqual(
            sorted(entry.name for entry in self.root.iterdir()),
            ["SomethingBoundLauncher.exe"],
        )


class DownloadTests(TempDirTestCase):
    def test_a_corrupt_replacement_never_reaches_the_swap(self) -> None:
        exe = self.root / "SomethingBoundLauncher.exe"
        exe.write_text("old launcher", encoding="utf-8")
        source = self.root / "channel"
        source.mkdir()
        published = source / "SomethingBoundLauncher.exe"
        published.write_text("new launcher", encoding="utf-8")

        manifest = manifest_for(published, "0.2.0")
        manifest["artifacts"][0]["sha256"] = "0" * 64

        with self.assertRaises(Exception):
            download_replacement(manifest, exe, base_dir=source)

        self.assertEqual(exe.read_text(), "old launcher")
        self.assertFalse(staged_path(exe).exists())


class LauncherIntegrationTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.channel = self.root / "channel"
        self.channel.mkdir()
        self.published = self.channel / "SomethingBoundLauncher.exe"
        self.published.write_text("new launcher", encoding="utf-8")
        (self.channel / "launcher.json").write_text(
            json.dumps(manifest_for(self.published, "9.9.9")), encoding="utf-8"
        )
        self.settings = LauncherSettings(
            install_dir=self.root / "install",
            launcher_manifest_url=str(self.channel / "launcher.json"),
        )

    def test_the_launcher_channel_is_read_separately_from_the_client_channel(self) -> None:
        launcher = Launcher(self.settings)
        manifest = launcher.fetch_launcher_manifest()
        self.assertEqual(manifest["channel"], "launcher")
        self.assertEqual(manifest["version"], "9.9.9")

    def test_a_source_checkout_refuses_to_replace_an_executable_it_does_not_have(self) -> None:
        launcher = Launcher(self.settings)
        manifest = launcher.fetch_launcher_manifest()

        with self.assertRaises(LauncherError) as context:
            launcher.update_launcher(manifest)
        self.assertIn("running from source", str(context.exception))

    def test_a_read_only_location_is_reported_before_anything_is_downloaded(self) -> None:
        exe = self.root / "SomethingBoundLauncher.exe"
        exe.write_text("old launcher", encoding="utf-8")
        launcher = Launcher(self.settings)
        manifest = launcher.fetch_launcher_manifest()

        with mock.patch("release_tools.launcher_core.running_executable", return_value=exe):
            with mock.patch("release_tools.launcher_core.can_replace", return_value=False):
                with self.assertRaises(LauncherError) as context:
                    launcher.update_launcher(manifest)

        self.assertIn("cannot write to", str(context.exception))
        self.assertEqual(exe.read_text(), "old launcher")

    def test_a_full_self_update_replaces_the_executable_and_relaunches(self) -> None:
        exe = self.root / "SomethingBoundLauncher.exe"
        exe.write_text("old launcher", encoding="utf-8")
        launcher = Launcher(self.settings)
        manifest = launcher.fetch_launcher_manifest()

        with mock.patch("release_tools.launcher_core.running_executable", return_value=exe):
            with mock.patch("release_tools.launcher_core.relaunch") as started:
                retired = launcher.update_launcher(manifest)

        started.assert_called_once_with(exe)
        self.assertEqual(exe.read_text(), "new launcher")
        self.assertEqual(retired.read_text(), "old launcher")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

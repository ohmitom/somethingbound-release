"""Tests for the installable launcher: build trees, settings, and hand-off."""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from release_tools.channel_manifest import (
    ChannelManifestValidationError,
    load_channel_manifest,
    manifest_source_base_dir,
)
from release_tools.game_process import (
    SERVER_ENDPOINT_ARGUMENT,
    GameLaunchError,
    build_command,
    build_environment,
    find_executable,
)
from release_tools.install_tree import (
    InstallTreeError,
    TreeInstaller,
    UnsafeArchiveError,
    build_directory_name,
    unpack_archive,
)
from release_tools.launcher_core import Launcher, LauncherError
from release_tools.server import (
    MANUAL_SERVER_ENDPOINT_ENV,
    ServerEndpointConfigurationError,
)
from release_tools.settings import (
    DEFAULT_EXECUTABLE,
    DEFAULT_SERVER_ENDPOINT,
    LauncherSettings,
    SettingsError,
    load_settings,
    normalise_server_endpoint,
    save_settings,
    settings_from_json,
)


SHA_A = "a" * 40
SHA_B = "b" * 40


def make_client_zip(destination: Path, *, version: str, wrapper: str | None = None) -> Path:
    """Write a zip shaped like a Unity player build."""

    prefix = f"{wrapper}/" if wrapper else ""
    with zipfile.ZipFile(destination, "w") as bundle:
        bundle.writestr(f"{prefix}{DEFAULT_EXECUTABLE}", f"client {version}")
        bundle.writestr(f"{prefix}SomethingBound_Data/resources.dat", "payload")
        bundle.writestr(f"{prefix}UnityPlayer.dll", "runtime")
    return destination


def manifest_for(artifact: Path, *, version: str, git_sha: str) -> dict:
    data = artifact.read_bytes()
    return {
        "channel": "test",
        "version": version,
        "gitSha": git_sha,
        "releasedAt": "2026-08-25T12:00:00Z",
        "notes": {
            "summary": f"Test release {version}.",
            "commits": [
                {
                    "sha": git_sha,
                    "subject": f"Ship {version}",
                    "category": "feat",
                    "scope": "client",
                    "pr": 1,
                }
            ],
        },
        "artifacts": [
            {
                "name": artifact.name,
                "url": artifact.name,
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


class ArchiveSafetyTests(TempDirTestCase):
    def test_unpacks_a_normal_client_archive(self) -> None:
        archive = make_client_zip(self.root / "client.zip", version="1.0.0")
        payload = unpack_archive(archive, self.root / "out")
        self.assertTrue((payload / DEFAULT_EXECUTABLE).is_file())
        self.assertTrue((payload / "SomethingBound_Data" / "resources.dat").is_file())

    def test_rejects_parent_traversal_and_absolute_entries(self) -> None:
        traversal = self.root / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as bundle:
            bundle.writestr("../escaped.txt", "no")
        with self.assertRaises(UnsafeArchiveError):
            unpack_archive(traversal, self.root / "a")

        absolute = self.root / "absolute.zip"
        with zipfile.ZipFile(absolute, "w") as bundle:
            bundle.writestr("/etc/passwd", "no")
        with self.assertRaises(UnsafeArchiveError):
            unpack_archive(absolute, self.root / "b")

        drive = self.root / "drive.zip"
        with zipfile.ZipFile(drive, "w") as bundle:
            bundle.writestr("C:/Windows/system32/evil.dll", "no")
        with self.assertRaises(UnsafeArchiveError):
            unpack_archive(drive, self.root / "c")

    def test_rejects_symbolic_link_entries(self) -> None:
        archive = self.root / "symlink.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            info = zipfile.ZipInfo("link")
            info.external_attr = (0xA1FF << 16)
            bundle.writestr(info, "/etc/passwd")
        with self.assertRaises(UnsafeArchiveError):
            unpack_archive(archive, self.root / "d")

    def test_rejects_a_file_that_is_not_a_zip(self) -> None:
        broken = self.root / "broken.zip"
        broken.write_bytes(b"not a zip at all")
        with self.assertRaises(InstallTreeError):
            unpack_archive(broken, self.root / "e")


class TreeInstallerTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.channel = self.root / "channel"
        self.channel.mkdir()
        self.install = self.root / "install"
        self.installer = TreeInstaller(self.install)

    def _publish(self, version: str, git_sha: str, *, wrapper: str | None = None) -> dict:
        artifact = make_client_zip(
            self.channel / f"client-{version}.zip", version=version, wrapper=wrapper
        )
        return manifest_for(artifact, version=version, git_sha=git_sha)

    def test_first_install_activates_a_versioned_build(self) -> None:
        manifest = self._publish("1.0.0", SHA_A)
        self.assertTrue(self.installer.is_update_available(manifest))

        result = self.installer.install(manifest, base_dir=self.channel)

        self.assertTrue(result.updated)
        self.assertIsNone(result.previous)
        self.assertEqual(result.installed.directory.name, build_directory_name("1.0.0", SHA_A))
        payload = self.installer.payload_dir()
        assert payload is not None
        self.assertEqual((payload / DEFAULT_EXECUTABLE).read_text(), "client 1.0.0")
        self.assertFalse(self.installer.is_update_available(manifest))

    def test_wrapper_folder_is_lifted_so_every_install_has_one_shape(self) -> None:
        manifest = self._publish("1.0.0", SHA_A, wrapper="SomethingBound-1.0.0")
        self.installer.install(manifest, base_dir=self.channel)
        payload = self.installer.payload_dir()
        assert payload is not None
        self.assertTrue((payload / DEFAULT_EXECUTABLE).is_file())

    def test_update_retains_the_previous_build_and_rolls_back_by_pointer(self) -> None:
        first = self._publish("1.0.0", SHA_A)
        self.installer.install(first, base_dir=self.channel)
        second = self._publish("1.1.0", SHA_B)
        result = self.installer.install(second, base_dir=self.channel)

        self.assertTrue(result.updated)
        assert result.previous is not None
        self.assertEqual(result.previous.build.version, "1.0.0")
        payload = self.installer.payload_dir()
        assert payload is not None
        self.assertEqual((payload / DEFAULT_EXECUTABLE).read_text(), "client 1.1.0")

        restored = self.installer.rollback()

        self.assertEqual(restored.build.version, "1.0.0")
        payload = self.installer.payload_dir()
        assert payload is not None
        self.assertEqual((payload / DEFAULT_EXECUTABLE).read_text(), "client 1.0.0")
        # Rolling back is itself reversible, because no payload was deleted.
        self.assertEqual(self.installer.rollback().build.version, "1.1.0")

    def test_a_corrupt_artifact_leaves_the_installed_build_untouched(self) -> None:
        first = self._publish("1.0.0", SHA_A)
        self.installer.install(first, base_dir=self.channel)

        second = self._publish("1.1.0", SHA_B)
        second["artifacts"][0]["sha256"] = "0" * 64

        with self.assertRaises(InstallTreeError.__mro__[1]):
            self.installer.install(second, base_dir=self.channel)

        current = self.installer.read_current()
        assert current is not None
        self.assertEqual(current.build.version, "1.0.0")
        payload = self.installer.payload_dir()
        assert payload is not None
        self.assertEqual((payload / DEFAULT_EXECUTABLE).read_text(), "client 1.0.0")
        self.assertEqual(list(self.installer.cache_dir.iterdir()), [])

    def test_a_failure_before_activation_leaves_no_staging_directory(self) -> None:
        def explode(stage: str) -> None:
            if stage == "before-activate":
                raise RuntimeError("simulated failure")

        installer = TreeInstaller(self.install, failure_hook=explode)
        manifest = self._publish("1.0.0", SHA_A)
        with self.assertRaises(RuntimeError):
            installer.install(manifest, base_dir=self.channel)

        self.assertIsNone(installer.read_current())
        leftovers = [entry for entry in installer.builds_dir.iterdir()]
        self.assertEqual(leftovers, [])

    def test_a_regressive_manifest_is_refused(self) -> None:
        self.installer.install(self._publish("1.1.0", SHA_B), base_dir=self.channel)
        older = self._publish("1.0.0", SHA_A)
        with self.assertRaises(ChannelManifestValidationError):
            self.installer.is_update_available(older)

    def test_a_missing_payload_makes_the_build_reinstallable(self) -> None:
        manifest = self._publish("1.0.0", SHA_A)
        self.installer.install(manifest, base_dir=self.channel)
        current = self.installer.read_current()
        assert current is not None

        for entry in sorted(current.directory.rglob("*"), reverse=True):
            entry.unlink() if entry.is_file() else entry.rmdir()
        current.directory.rmdir()

        self.assertIsNone(self.installer.payload_dir())
        self.assertTrue(self.installer.is_update_available(manifest))
        self.installer.install(manifest, base_dir=self.channel)
        self.assertIsNotNone(self.installer.payload_dir())

    def test_prune_keeps_the_active_and_previous_builds(self) -> None:
        self.installer.install(self._publish("1.0.0", SHA_A), base_dir=self.channel)
        self.installer.install(self._publish("1.1.0", SHA_B), base_dir=self.channel)
        self.installer.install(self._publish("1.2.0", "c" * 40), base_dir=self.channel)

        self.installer.prune(keep=0)

        remaining = sorted(entry.name for entry in self.installer.builds_dir.iterdir())
        self.assertEqual(
            remaining,
            sorted(
                [
                    build_directory_name("1.1.0", SHA_B),
                    build_directory_name("1.2.0", "c" * 40),
                ]
            ),
        )
        # The retained build is still a real payload, so rollback still works.
        self.assertEqual(self.installer.rollback().build.version, "1.1.0")

    def test_a_pointer_naming_a_path_is_refused(self) -> None:
        self.install.mkdir(parents=True, exist_ok=True)
        (self.install / "current.json").write_text(
            json.dumps(
                {"version": "1.0.0", "gitSha": SHA_A, "directory": "../../elsewhere"}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(InstallTreeError):
            self.installer.read_current()


class SettingsTests(TempDirTestCase):
    def test_round_trips_through_disk(self) -> None:
        settings = LauncherSettings(
            channel="playtest",
            manifest_url="https://example.invalid/playtest.json",
            install_dir=self.root / "install",
            server_endpoint="https://server.example.invalid",
            auto_update=False,
        )
        save_settings(settings, self.root)
        restored = load_settings(self.root)
        self.assertEqual(restored, settings)

    def test_absent_settings_are_defaults_not_an_error(self) -> None:
        settings = load_settings(self.root)
        self.assertEqual(settings.channel, "playtest")
        self.assertEqual(settings.server_endpoint, DEFAULT_SERVER_ENDPOINT)
        self.assertTrue(settings.auto_update)
        self.assertIn("channels/playtest.json", settings.resolved_manifest_url())

    def test_an_omitted_endpoint_defaults_but_an_explicit_null_clears(self) -> None:
        self.assertEqual(
            settings_from_json({"channel": "playtest"}).server_endpoint,
            DEFAULT_SERVER_ENDPOINT,
        )
        self.assertIsNone(settings_from_json({"serverEndpoint": None}).server_endpoint)
        self.assertIsNone(settings_from_json({"serverEndpoint": "  "}).server_endpoint)

    def test_server_endpoint_rules_match_the_release_tooling(self) -> None:
        self.assertEqual(
            normalise_server_endpoint("  https://play.example.invalid  "),
            "https://play.example.invalid",
        )
        self.assertEqual(
            normalise_server_endpoint("http://localhost:8080"), "http://localhost:8080"
        )
        self.assertIsNone(normalise_server_endpoint("   "))
        self.assertIsNone(normalise_server_endpoint(None))

        for rejected in (
            "http://play.example.invalid",
            "https://user:secret@play.example.invalid",
            "https://has space.invalid",
            "ftp://play.example.invalid",
            "not a url",
        ):
            with self.subTest(rejected=rejected):
                with self.assertRaises(ServerEndpointConfigurationError):
                    normalise_server_endpoint(rejected)

    def test_corrupt_or_unknown_settings_are_reported_clearly(self) -> None:
        (self.root / "launcher.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(self.root)

        with self.assertRaises(SettingsError):
            settings_from_json({"channel": "playtest", "whatIsThis": 1})
        with self.assertRaises(SettingsError):
            settings_from_json({"executableName": "sub/dir/Game.exe"})
        with self.assertRaises(SettingsError):
            settings_from_json({"autoUpdate": "yes"})

    def test_a_byte_order_mark_does_not_brick_the_launcher(self) -> None:
        """Notepad, Out-File, and Set-Content all write one by default."""

        body = json.dumps({"channel": "beta", "serverEndpoint": None})
        (self.root / "launcher.json").write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))

        settings = load_settings(self.root)

        self.assertEqual(settings.channel, "beta")
        self.assertIsNone(settings.server_endpoint)

    def test_writing_settings_never_leaves_a_truncated_file(self) -> None:
        save_settings(LauncherSettings(server_endpoint="https://a.invalid"), self.root)
        save_settings(LauncherSettings(server_endpoint="https://b.invalid"), self.root)
        stray = [entry.name for entry in self.root.iterdir() if entry.name.startswith(".")]
        self.assertEqual(stray, [])
        self.assertEqual(load_settings(self.root).server_endpoint, "https://b.invalid")


class GameProcessTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.payload = self.root / "payload"
        (self.payload / "SomethingBound_Data").mkdir(parents=True)
        (self.payload / DEFAULT_EXECUTABLE).write_text("client", encoding="utf-8")
        (self.payload / "UnityCrashHandler64.exe").write_text("crash", encoding="utf-8")

    def test_finds_the_named_executable_and_not_a_neighbour(self) -> None:
        found = find_executable(self.payload, DEFAULT_EXECUTABLE)
        self.assertEqual(found.name, DEFAULT_EXECUTABLE)

    def test_reports_a_missing_executable_by_name(self) -> None:
        with self.assertRaises(GameLaunchError) as context:
            find_executable(self.payload, "NotThere.exe")
        self.assertIn("NotThere.exe", str(context.exception))

    def test_the_endpoint_reaches_the_client_by_argument_and_environment(self) -> None:
        endpoint = "https://play.example.invalid"
        command = build_command(self.payload / DEFAULT_EXECUTABLE, server_endpoint=endpoint)
        self.assertEqual(command[1:], [SERVER_ENDPOINT_ARGUMENT, endpoint])
        self.assertEqual(
            build_environment(endpoint, base={})[MANUAL_SERVER_ENDPOINT_ENV], endpoint
        )

    def test_an_unset_endpoint_is_not_inherited_from_the_launcher(self) -> None:
        command = build_command(self.payload / DEFAULT_EXECUTABLE, server_endpoint=None)
        self.assertEqual(command, [str(self.payload / DEFAULT_EXECUTABLE)])
        inherited = {MANUAL_SERVER_ENDPOINT_ENV: "https://stale.example.invalid"}
        self.assertNotIn(MANUAL_SERVER_ENDPOINT_ENV, build_environment(None, base=inherited))


class LauncherCoreTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.channel = self.root / "channel"
        self.channel.mkdir()
        self.settings = LauncherSettings(
            channel="test",
            manifest_url=str(self.channel / "manifest.json"),
            install_dir=self.root / "install",
        )

    def _write_channel(self, version: str, git_sha: str) -> dict:
        artifact = make_client_zip(
            self.channel / f"client-{version}.zip", version=version
        )
        manifest = manifest_for(artifact, version=version, git_sha=git_sha)
        (self.channel / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return manifest

    def test_first_run_installs_then_reports_up_to_date(self) -> None:
        self._write_channel("1.0.0", SHA_A)
        launcher = Launcher(self.settings)

        status = launcher.status()
        self.assertTrue(status.is_first_install)
        self.assertFalse(status.can_play)
        self.assertIn("Ready to install 1.0.0", status.summary())

        assert status.manifest is not None
        seen: list[tuple[int, int]] = []
        result = launcher.update(
            status.manifest, progress=lambda done, total: seen.append((done, total))
        )
        self.assertTrue(result.updated)
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], seen[-1][1])

        after = launcher.status()
        self.assertFalse(after.update_available)
        self.assertTrue(after.can_play)
        self.assertEqual(after.summary(), "Up to date on 1.0.0.")
        self.assertEqual(launcher.client_executable().name, DEFAULT_EXECUTABLE)

    def test_a_new_channel_release_is_offered_as_an_update(self) -> None:
        self._write_channel("1.0.0", SHA_A)
        launcher = Launcher(self.settings)
        launcher.update(launcher.status().manifest)

        self._write_channel("1.1.0", SHA_B)
        status = launcher.status()
        self.assertTrue(status.update_available)
        self.assertEqual(status.summary(), "Update available: 1.0.0 to 1.1.0.")
        self.assertIn("Ship 1.1.0", launcher.patch_notes(status.manifest))

    def test_an_unreachable_channel_still_allows_playing_offline(self) -> None:
        self._write_channel("1.0.0", SHA_A)
        launcher = Launcher(self.settings)
        launcher.update(launcher.status().manifest)

        (self.channel / "manifest.json").unlink()
        status = launcher.status()

        self.assertIsNone(status.manifest)
        self.assertIsNotNone(status.channel_error)
        self.assertTrue(status.can_play)
        self.assertIn("Offline", status.summary())

    def test_an_empty_install_with_an_unreachable_channel_cannot_play(self) -> None:
        launcher = Launcher(self.settings)
        status = launcher.status()
        self.assertFalse(status.can_play)
        self.assertIn("no build is installed", status.summary())
        with self.assertRaises(LauncherError):
            launcher.play()

    def test_a_remote_manifest_is_fetched_over_https(self) -> None:
        manifest = self._write_channel("1.0.0", SHA_A)
        payload = json.dumps(manifest).encode("utf-8")
        requested: list[str] = []

        def opener(request, timeout=None):
            requested.append(request.full_url)
            # A stale channel pointer is the one answer a launcher must not get.
            self.assertEqual(request.headers.get("Cache-control"), "no-cache")
            return io.BytesIO(payload)

        settings = LauncherSettings(
            channel="test",
            manifest_url="https://releases.example.invalid/channels/test.json",
            install_dir=self.root / "install",
        )
        fetched = Launcher(settings, opener=opener).fetch_manifest()

        self.assertEqual(fetched["version"], "1.0.0")
        self.assertEqual(requested, ["https://releases.example.invalid/channels/test.json"])
        # A remote manifest has no local base directory to resolve against.
        self.assertIsNone(manifest_source_base_dir(settings.resolved_manifest_url()))

    def test_a_plain_http_manifest_host_is_refused(self) -> None:
        settings = LauncherSettings(
            channel="test",
            manifest_url="http://releases.example.invalid/channels/test.json",
            install_dir=self.root / "install",
        )
        with self.assertRaises(LauncherError) as context:
            Launcher(settings).fetch_manifest()
        self.assertIn("HTTPS", str(context.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

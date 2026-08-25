"""Tests for promoting a client build to a release channel."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from release_tools.channel_manifest import validate_channel_manifest
from release_tools.install_tree import TreeInstaller
from release_tools.publish import (
    INITIAL_RELEASE_COMMIT_LIMIT,
    PublishError,
    anchor_is_reachable,
    filter_player_facing,
    build_manifest,
    collect_commits,
    find_token,
    head_sha,
    package_build,
    parse_commit,
    release_asset_url,
    sha256_of,
    working_tree_is_clean,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


class CommitNoteTests(unittest.TestCase):
    def test_a_conventional_subject_becomes_a_categorised_note(self) -> None:
        note = parse_commit(SHA, "feat(world): Build the first outdoor gate")
        self.assertEqual(note["category"], "feat")
        self.assertEqual(note["scope"], "world")
        self.assertEqual(note["subject"], "Build the first outdoor gate")
        self.assertIsNone(note["pr"])

    def test_a_scopeless_and_a_breaking_subject_both_parse(self) -> None:
        self.assertIsNone(parse_commit(SHA, "fix: Stop the crash")["scope"])
        self.assertEqual(parse_commit(SHA, "feat!: Replace saves")["category"], "feat!")

    def test_a_pull_request_suffix_is_lifted_out_of_the_subject(self) -> None:
        note = parse_commit(SHA, "feat(ui): Add character creation (#28)")
        self.assertEqual(note["pr"], 28)
        self.assertEqual(note["subject"], "Add character creation")

    def test_a_plain_subject_still_produces_a_valid_note(self) -> None:
        note = parse_commit(SHA, "Polish the login screen")
        self.assertEqual(note["category"], "change")
        self.assertIsNone(note["scope"])
        self.assertEqual(note["subject"], "Polish the login screen")

    def test_the_full_sha_is_retained_and_lowercased(self) -> None:
        note = parse_commit(SHA.upper(), "fix: Something")
        self.assertEqual(note["sha"], SHA)


class PlayerFacingNoteTests(unittest.TestCase):
    def _notes(self, *subjects: str) -> list[dict]:
        return [parse_commit(SHA, subject) for subject in subjects]

    def test_repository_work_is_hidden_from_players(self) -> None:
        commits = self._notes(
            "feat(world): Build the first outdoor gate",
            "docs: reflow a paragraph",
            "fix(login): Stop the flicker",
            "chore: bump a pin",
            "ci: retry the runner",
        )

        kept = filter_player_facing(commits)

        self.assertEqual(
            [note["subject"] for note in kept],
            ["Build the first outdoor gate", "Stop the flicker"],
        )

    def test_a_breaking_marker_does_not_smuggle_a_category_through(self) -> None:
        self.assertEqual(filter_player_facing(self._notes("docs!: rewrite it all")), [])
        self.assertEqual(len(filter_player_facing(self._notes("feat!: replace saves"))), 1)

    def test_uncategorised_commits_are_kept(self) -> None:
        """A plain subject parses as "change" and is real work until proven otherwise."""

        kept = filter_player_facing(self._notes("Polish the login screen"))
        self.assertEqual(len(kept), 1)

    def test_a_release_of_only_internal_work_still_produces_a_valid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            build.mkdir()
            (build / "SomethingBound.exe").write_text("player", encoding="utf-8")
            archive = package_build(build, root / "client.zip")

            manifest = build_manifest(
                channel="playtest",
                version="0.3.1",
                git_sha=SHA,
                artifact=archive,
                artifact_url=archive.name,
                summary="Documentation only.",
                commits=filter_player_facing(self._notes("docs: tidy", "chore: pin")),
            )

        self.assertEqual(validate_channel_manifest(manifest), [])
        self.assertEqual(manifest["notes"]["commits"], [])


class GitRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.repo = Path(self._temp.name)
        self._git("init", "--initial-branch=main")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        self._git("config", "commit.gpgsign", "false")

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _commit(self, subject: str) -> str:
        (self.repo / "file.txt").write_text(subject, encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "-m", subject)
        return self._git("rev-parse", "HEAD").lower()

    def test_only_commits_after_the_anchor_are_collected(self) -> None:
        first = self._commit("feat(a): One")
        self._commit("fix(b): Two")
        self._commit("docs: Three")

        commits = collect_commits(self.repo, since=first)

        self.assertEqual([note["subject"] for note in commits], ["Three", "Two"])

    def test_the_first_release_is_capped_rather_than_unbounded(self) -> None:
        for index in range(INITIAL_RELEASE_COMMIT_LIMIT + 5):
            self._commit(f"feat(x): Commit {index}")

        commits = collect_commits(self.repo, since=None)

        self.assertEqual(len(commits), INITIAL_RELEASE_COMMIT_LIMIT)

    def test_an_anchor_from_another_repository_falls_back_to_initial(self) -> None:
        """A channel seeded elsewhere must not fail every later publish."""

        self._commit("feat(a): One")
        self._commit("fix(b): Two")
        stranger = "9" * 40

        self.assertFalse(anchor_is_reachable(self.repo, stranger))
        commits = collect_commits(self.repo, since=stranger)

        self.assertEqual([note["subject"] for note in commits], ["Two", "One"])

    def test_a_reachable_anchor_is_still_honoured(self) -> None:
        first = self._commit("feat(a): One")
        self._commit("fix(b): Two")

        self.assertTrue(anchor_is_reachable(self.repo, first))
        self.assertEqual(len(collect_commits(self.repo, since=first)), 1)

    def test_a_dirty_tree_is_visible_before_a_release_claims_a_commit(self) -> None:
        self._commit("feat(a): One")
        self.assertTrue(working_tree_is_clean(self.repo))
        self.assertEqual(len(head_sha(self.repo)), 40)

        (self.repo / "file.txt").write_text("uncommitted", encoding="utf-8")
        self.assertFalse(working_tree_is_clean(self.repo))

    def test_a_directory_that_is_not_a_repository_reports_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(PublishError):
                head_sha(Path(outside))


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.build = self.root / "build"
        (self.build / "SomethingBound_Data").mkdir(parents=True)
        (self.build / "SomethingBound.exe").write_text("player", encoding="utf-8")
        (self.build / "SomethingBound_Data" / "data.unity3d").write_text("x", encoding="utf-8")

    def test_the_archive_has_no_wrapper_directory(self) -> None:
        archive = package_build(self.build, self.root / "client.zip")
        with zipfile.ZipFile(archive) as bundle:
            names = sorted(bundle.namelist())
        self.assertEqual(
            names, ["SomethingBound.exe", "SomethingBound_Data/data.unity3d"]
        )

    def test_an_empty_or_missing_build_is_refused(self) -> None:
        with self.assertRaises(PublishError):
            package_build(self.root / "nope", self.root / "a.zip")
        (self.root / "empty").mkdir()
        with self.assertRaises(PublishError):
            package_build(self.root / "empty", self.root / "b.zip")

    def test_the_manifest_validates_and_describes_the_real_bytes(self) -> None:
        archive = package_build(self.build, self.root / "client.zip")
        manifest = build_manifest(
            channel="playtest",
            version="0.3.0",
            git_sha=SHA,
            artifact=archive,
            artifact_url=release_asset_url("owner/repo", "v0.3.0", archive.name),
            summary="A release.",
            commits=[parse_commit(SHA, "feat(world): Ship it (#31)")],
        )

        self.assertEqual(validate_channel_manifest(manifest), [])
        artifact = manifest["artifacts"][0]
        self.assertEqual(artifact["sha256"], sha256_of(archive))
        self.assertEqual(artifact["size"], archive.stat().st_size)
        self.assertEqual(
            artifact["url"],
            "https://github.com/owner/repo/releases/download/v0.3.0/client.zip",
        )

    def test_a_published_manifest_is_installable_by_the_launcher(self) -> None:
        """The publish and install halves must agree on one contract."""

        archive = package_build(self.build, self.root / "channel" / "client.zip")
        manifest = build_manifest(
            channel="playtest",
            version="0.3.0",
            git_sha=SHA,
            artifact=archive,
            artifact_url=archive.name,
            summary="A release.",
            commits=[parse_commit(SHA, "feat(world): Ship it")],
        )

        installer = TreeInstaller(self.root / "install")
        result = installer.install(manifest, base_dir=archive.parent)

        self.assertTrue(result.updated)
        payload = installer.payload_dir()
        assert payload is not None
        self.assertTrue((payload / "SomethingBound.exe").is_file())

    def test_a_manifest_that_would_not_validate_is_refused_before_upload(self) -> None:
        archive = package_build(self.build, self.root / "client.zip")
        with self.assertRaises(PublishError):
            build_manifest(
                channel="playtest",
                version="not-a-version",
                git_sha=SHA,
                artifact=archive,
                artifact_url=archive.name,
                summary="A release.",
                commits=[],
            )


class TokenTests(unittest.TestCase):
    def test_the_first_configured_token_variable_wins(self) -> None:
        self.assertEqual(
            find_token({"GITHUB_TOKEN": "second", "SOMETHINGBOUND_RELEASE_TOKEN": "first"}),
            "first",
        )
        self.assertEqual(find_token({"GH_TOKEN": "only"}), "only")

    def test_a_missing_token_names_the_variables_to_set(self) -> None:
        with self.assertRaises(PublishError) as context:
            find_token({"SOMETHINGBOUND_RELEASE_TOKEN": "   "})
        self.assertIn("SOMETHINGBOUND_RELEASE_TOKEN", str(context.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

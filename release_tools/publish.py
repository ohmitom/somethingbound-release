"""Turn a built client into a release the launcher can install.

One command does the whole promotion: package the build, derive patch notes
from the game repository's history since the last published release, write a
channel manifest, and upload it all as a GitHub release.  The channel pointer
committed to this repository is what the launcher polls, so publishing is
finished only when that file names the new release.

Uploading is opt-in.  Without ``--publish`` this builds and validates
everything locally and changes nothing outside the staging directory, which is
the state you want when checking what a release would contain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .channel_manifest import ChannelManifestValidationError, validate_channel_manifest
from .manifest import _GIT_SHA_RE

GITHUB_API = "https://api.github.com"
GITHUB_UPLOADS = "https://uploads.github.com"
TOKEN_VARIABLES = ("SOMETHINGBOUND_RELEASE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")

# Conventional commit subjects give the launcher a category and scope for free.
_CONVENTIONAL = re.compile(
    r"^(?P<category>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s*(?P<subject>.+)$"
)
_PR_SUFFIX = re.compile(r"\s*\(#(?P<pr>\d+)\)\s*$")
_RECORD = "\x1f"
_LINE = "\x1e"

# How many commits the very first release on a channel lists.
INITIAL_RELEASE_COMMIT_LIMIT = 20


class PublishError(RuntimeError):
    """A release could not be prepared or uploaded."""


@dataclass(frozen=True)
class Release:
    """Everything one publication needs."""

    channel: str
    version: str
    git_sha: str
    tag: str
    artifact: Path
    manifest: dict[str, Any]


def _git(repo: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PublishError("git is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise PublishError(f"git {' '.join(arguments)} failed: {detail}") from exc
    return result.stdout.strip()


def head_sha(repo: Path) -> str:
    """Return the full lowercase commit SHA the build was made from."""

    return _git(repo, "rev-parse", "HEAD").lower()


def working_tree_is_clean(repo: Path) -> bool:
    """Whether the build can be attributed to the commit it claims."""

    return _git(repo, "status", "--porcelain") == ""


def parse_commit(sha: str, subject: str) -> dict[str, Any]:
    """Turn one commit subject into a manifest note entry.

    A subject that is not a conventional commit still produces a valid entry;
    it simply has no scope and the generic ``change`` category.
    """

    pull_request: int | None = None
    match = _PR_SUFFIX.search(subject)
    if match:
        pull_request = int(match.group("pr"))
        subject = subject[: match.start()].rstrip()

    conventional = _CONVENTIONAL.match(subject)
    if conventional:
        category = conventional.group("category")
        if conventional.group("breaking"):
            category = f"{category}!"
        scope = conventional.group("scope") or None
        text = conventional.group("subject").strip()
    else:
        category = "change"
        scope = None
        text = subject.strip()

    return {
        "sha": sha.lower(),
        "subject": text or subject.strip() or "(no subject)",
        "category": category,
        "scope": scope,
        "pr": pull_request,
    }


def anchor_is_reachable(repo: Path, sha: str) -> bool:
    """Whether a previous release anchor is a commit in this repository.

    A channel can name an anchor this repository has never seen: the channel
    was seeded from elsewhere, history was rewritten, or the commit was pruned.
    """

    try:
        _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    except PublishError:
        return False
    return True


def collect_commits(repo: Path, *, since: str | None, until: str = "HEAD") -> list[dict[str, Any]]:
    """Return first-parent commits in the published range, newest first.

    First-parent history is what a tester recognises: one entry per merge to
    main rather than every commit on every branch behind it.

    An anchor this repository does not contain is treated as no anchor at all,
    so the release is labelled an initial release rather than failing or
    inventing a range.
    """

    if since is not None and not anchor_is_reachable(repo, since):
        since = None

    span = until if since is None else f"{since}..{until}"
    arguments = ["log", "--first-parent", f"--format=%H{_RECORD}%s{_LINE}"]
    if since is None:
        # The first release on a channel has no anchor, so cap the list rather
        # than pouring the project's entire history into the launcher window.
        arguments += ["-n", str(INITIAL_RELEASE_COMMIT_LIMIT)]
    arguments.append(span)
    raw = _git(repo, *arguments)
    commits = []
    for line in raw.split(_LINE):
        entry = line.strip()
        if not entry:
            continue
        sha, _, subject = entry.partition(_RECORD)
        if sha and subject:
            commits.append(parse_commit(sha, subject))
    return commits


def package_build(build_dir: str | Path, destination: str | Path) -> Path:
    """Zip a built client, refusing anything that is obviously not one."""

    source = Path(build_dir)
    if not source.is_dir():
        raise PublishError(f"build directory does not exist: {source}")
    files = sorted(entry for entry in source.rglob("*") if entry.is_file())
    if not files:
        raise PublishError(f"build directory is empty: {source}")

    archive = Path(destination)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for entry in files:
            bundle.write(entry, entry.relative_to(source).as_posix())
    return archive


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    *,
    channel: str,
    version: str,
    git_sha: str,
    artifact: Path,
    artifact_url: str,
    summary: str,
    commits: Iterable[dict[str, Any]],
    released_at: str | None = None,
) -> dict[str, Any]:
    """Assemble and validate the channel manifest for one release."""

    manifest = {
        "channel": channel,
        "version": version,
        "gitSha": git_sha,
        "releasedAt": released_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": {"summary": summary, "commits": list(commits)},
        "artifacts": [
            {
                "name": artifact.name,
                "url": artifact_url,
                "sha256": sha256_of(artifact),
                "size": artifact.stat().st_size,
            }
        ],
    }
    errors = validate_channel_manifest(manifest)
    if errors:
        raise PublishError("generated manifest is invalid: " + "; ".join(errors))
    return manifest


# -- GitHub ------------------------------------------------------------------


def find_token(env: dict[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    for name in TOKEN_VARIABLES:
        token = (values.get(name) or "").strip()
        if token:
            return token
    raise PublishError(
        "no GitHub token found. Set one of "
        + ", ".join(TOKEN_VARIABLES)
        + " to a token with contents:write on the release repository."
    )


def _api(
    method: str,
    url: str,
    token: str,
    *,
    body: bytes | None = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise PublishError(f"GitHub {method} {url} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise PublishError(f"GitHub {method} {url} failed: {exc}") from exc
    return json.loads(payload) if payload else {}


def create_github_release(
    repository: str,
    release: Release,
    *,
    token: str,
    body: str,
    assets_to_upload: Iterable[Path],
    draft: bool = False,
) -> dict[str, Any]:
    """Create the release and upload the client artifact and manifest."""

    payload = json.dumps(
        {
            "tag_name": release.tag,
            "target_commitish": release.git_sha,
            "name": f"SomethingBound {release.version}",
            "body": body,
            "draft": draft,
            "prerelease": "-" in release.version,
        }
    ).encode("utf-8")
    created = _api("POST", f"{GITHUB_API}/repos/{repository}/releases", token, body=payload)

    upload_base = f"{GITHUB_UPLOADS}/repos/{repository}/releases/{created['id']}/assets"
    assets: dict[str, str] = {}
    for path in assets_to_upload:
        content_type = "application/zip" if path.suffix == ".zip" else "application/json"
        uploaded = _api(
            "POST",
            f"{upload_base}?name={path.name}",
            token,
            body=path.read_bytes(),
            content_type=content_type,
        )
        assets[path.name] = uploaded["browser_download_url"]

    created["uploadedAssets"] = assets
    return created


def release_asset_url(repository: str, tag: str, name: str) -> str:
    """Return the stable public download URL an asset will have."""

    return f"https://github.com/{repository}/releases/download/{tag}/{name}"


# -- command -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish",
        description="Package a client build and publish it to a release channel.",
    )
    parser.add_argument("--build-dir", type=Path, required=True, help="built client directory")
    parser.add_argument("--version", required=True, help="SemVer version for this release")
    parser.add_argument(
        "--game-repo",
        type=Path,
        required=True,
        help="the client repository the build was made from",
    )
    parser.add_argument("--channel", default="playtest", help="release channel name")
    parser.add_argument(
        "--repository",
        default="ohmitom/somethingbound-release",
        help="owner/name of the release repository",
    )
    parser.add_argument("--summary", help="one-line player-facing summary of this release")
    parser.add_argument(
        "--staging",
        type=Path,
        default=Path("dist"),
        help="directory for the packaged artifact and manifest",
    )
    parser.add_argument(
        "--channels-dir",
        type=Path,
        default=Path("channels"),
        help="directory in this repository holding channel pointers",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="actually create the GitHub release and upload the artifact",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="publish even though the game repository has uncommitted changes",
    )
    parser.add_argument(
        "--git-sha",
        help=(
            "commit the build was made from. Supplying it asserts that the caller "
            "already verified the tree, which is what the build script does before "
            "the build writes the version into project settings."
        ),
    )
    return parser


def _previous_sha(channels_dir: Path, channel: str) -> str | None:
    pointer = channels_dir / f"{channel}.json"
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sha = value.get("gitSha")
    return sha if isinstance(sha, str) else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.git_sha:
            # The caller verified the tree before the build ran.  Checking it
            # again here would fail on the version the build itself wrote into
            # project settings.
            git_sha = args.git_sha.strip().lower()
            if _GIT_SHA_RE.fullmatch(git_sha) is None:
                raise PublishError("--git-sha must be 40 lowercase hexadecimal characters")
        else:
            if not args.allow_dirty and not working_tree_is_clean(args.game_repo):
                raise PublishError(
                    "the game repository has uncommitted changes, so this build cannot be "
                    "attributed to a commit. Commit them or pass --allow-dirty."
                )
            git_sha = head_sha(args.game_repo)
        tag = f"v{args.version}"
        previous = _previous_sha(args.channels_dir, args.channel)
        if previous is not None and not anchor_is_reachable(args.game_repo, previous):
            print(
                f"note: the previous channel anchor {previous[:7]} is not a commit in "
                f"{args.game_repo}, so this is an initial release for the "
                f"{args.channel} channel"
            )
            previous = None
        commits = collect_commits(args.game_repo, since=previous)
        summary = args.summary or (
            commits[0]["subject"] if commits else f"SomethingBound {args.version}."
        )

        artifact_name = f"somethingbound-client-{args.version}.zip"
        artifact = package_build(args.build_dir, args.staging / artifact_name)
        manifest = build_manifest(
            channel=args.channel,
            version=args.version,
            git_sha=git_sha,
            artifact=artifact,
            artifact_url=release_asset_url(args.repository, tag, artifact_name),
            summary=summary,
            commits=commits,
        )

        manifest_path = args.staging / f"{args.channel}.json"
        manifest_text = json.dumps(manifest, indent=2) + "\n"
        manifest_path.write_text(manifest_text, encoding="utf-8")

        print(f"channel:  {args.channel}")
        print(f"version:  {args.version} ({git_sha})")
        print(f"artifact: {artifact} ({artifact.stat().st_size} bytes)")
        print(f"sha256:   {manifest['artifacts'][0]['sha256']}")
        print(f"notes:    {len(commits)} commit(s) since {previous or 'the initial release'}")
        print(f"manifest: {manifest_path}")

        if not args.publish:
            print()
            print("Dry run. Nothing was uploaded and no channel pointer was moved.")
            print("Re-run with --publish to create the GitHub release.")
            return 0

        token = find_token()
        release = Release(args.channel, args.version, git_sha, tag, artifact, manifest)
        body = "\n".join(
            [summary, ""]
            + [
                f"- {commit['sha'][:7]} {commit['category']}"
                + (f"({commit['scope']})" if commit["scope"] else "")
                + f": {commit['subject']}"
                for commit in commits
            ]
        )
        created = create_github_release(
            args.repository,
            release,
            token=token,
            body=body,
            assets_to_upload=[artifact, manifest_path],
        )
        print(f"released: {created['html_url']}")

        args.channels_dir.mkdir(parents=True, exist_ok=True)
        pointer = args.channels_dir / f"{args.channel}.json"
        pointer.write_text(manifest_text, encoding="utf-8")
        print(f"pointer:  {pointer}")
        print()
        print("Commit and push the channel pointer to finish the promotion:")
        print(f"  git add {pointer} && git commit -m \"release: {args.channel} {args.version}\"")
        print("  git push")
        return 0
    except (PublishError, ChannelManifestValidationError) as exc:
        print(f"publish error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Minimal local launcher entry point for the release-channel foundation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .channel_manifest import ChannelManifestValidationError, load_channel_manifest
from .patch_notes import render_patch_notes
from .update_engine import InstalledBuild, InstalledStateError, UpdateEngine, UpdateError


_ZERO_SHA = "0" * 40


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local SomethingBound launcher update foundation."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="channel manifest JSON (a local path or local HTTP manifest source)",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        required=True,
        help="local launcher install directory used by this development run",
    )
    parser.add_argument(
        "--installed-version",
        help="seed version when --install-dir has no installed.json (default: 0.0.0)",
    )
    parser.add_argument(
        "--installed-git-sha",
        help="seed Git SHA when --install-dir has no installed.json (default: all zeroes)",
    )
    parser.add_argument(
        "--collapsed-notes",
        action="store_true",
        help="show the commit count instead of expanding exact commits",
    )
    return parser


def _load_or_seed_state(args: argparse.Namespace, engine: UpdateEngine) -> InstalledBuild:
    try:
        return engine.read_installed()
    except InstalledStateError:
        version = args.installed_version or "0.0.0"
        git_sha = args.installed_git_sha or _ZERO_SHA
        return engine.seed_installed(InstalledBuild(version, git_sha))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = UpdateEngine(args.install_dir)
    try:
        installed = _load_or_seed_state(args, engine)
        manifest = load_channel_manifest(
            args.manifest,
            installed_version=installed.version,
        )
        print(
            f"Installed build: {installed.version} ({installed.git_sha})"
        )
        print(
            "Update available: "
            + ("yes" if engine.is_update_available(manifest, installed) else "no")
        )
        result = engine.update(
            manifest,
            installed=installed,
            base_dir=args.manifest.resolve().parent,
        )
        if result.updated:
            print(
                f"Installed update: {result.installed.version} "
                f"({result.installed.git_sha})"
            )
            print(f"Artifact: {result.artifact_path}")
            if result.previous_path is not None:
                print(f"Previous build recoverable at: {result.previous_path}")
            else:
                print("No previous artifact payload was seeded in this development run.")
        else:
            print("No update installed.")
        running = result.installed
        print(render_patch_notes(manifest, running.git_sha, expanded=not args.collapsed_notes))
        return 0
    except (ChannelManifestValidationError, UpdateError, ValueError) as exc:
        print(f"launcher error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Command-line validation for a SomethingBound release manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .manifest import ManifestValidationError, load_manifest, verify_artifact


def _artifact_argument(value: str) -> tuple[str, Path]:
    kind, separator, path = value.partition("=")
    if not separator or not kind or not path:
        raise argparse.ArgumentTypeError("must use KIND=PATH")
    return kind, Path(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a SomethingBound release manifest and optional local artifacts."
    )
    parser.add_argument("manifest", type=Path, help="path to the JSON manifest")
    parser.add_argument(
        "--artifact",
        dest="artifacts",
        action="append",
        type=_artifact_argument,
        metavar="KIND=PATH",
        help="also verify a local artifact against its manifest SHA-256 and size (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ManifestValidationError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    seen: set[str] = set()
    for kind, path in args.artifacts or []:
        if kind in seen:
            print(f"error: duplicate artifact kind: {kind}", file=sys.stderr)
            return 2
        seen.add(kind)
        errors = verify_artifact(manifest, kind, path)
        if errors:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"verified artifact: {kind}")

    print(f"valid manifest: {args.manifest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

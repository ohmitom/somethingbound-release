"""Terminal-friendly patch-note rendering for the launcher foundation."""

from __future__ import annotations

from typing import Any

from .channel_manifest import validate_channel_manifest


def render_patch_notes(
    manifest: dict[str, Any],
    running_git_sha: str,
    *,
    expanded: bool = True,
) -> str:
    """Render the summary, running SHA, and optionally the exact commit list."""

    errors = validate_channel_manifest(manifest)
    if errors:
        raise ValueError("invalid channel manifest: " + "; ".join(errors))
    if not isinstance(running_git_sha, str) or not running_git_sha:
        raise ValueError("running_git_sha must be visible and non-empty")

    notes = manifest["notes"]
    lines = [
        f"Patch notes - {manifest['channel']} {manifest['version']}",
        f"Running Git SHA: {running_git_sha}",
        f"Summary: {notes['summary']}",
    ]
    commits = notes["commits"]
    if not expanded:
        lines.append(f"Commits: {len(commits)} (expand to view exact commits)")
        return "\n".join(lines)

    lines.append("Commits:")
    if not commits:
        lines.append("  (none)")
    for commit in commits:
        scope = f"({commit['scope']})" if commit["scope"] else ""
        pr = commit.get("pr")
        pr_text = f" PR #{pr}" if isinstance(pr, int) else (f" PR {pr}" if pr else "")
        lines.append(
            f"  - {commit['sha'][:7]} {commit['category']}{scope}: "
            f"{commit['subject']}{pr_text}"
        )
    return "\n".join(lines)


__all__ = ["render_patch_notes"]

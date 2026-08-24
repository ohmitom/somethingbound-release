# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

## Project map

- The launcher channel contract is authoritative in `docs/release-channel-manifest.md` and `schema/release-manifest.schema.json`; the local end-to-end example is `fixtures/manifest.local-dev.json`.
- Run the full dependency-free validation with `python3 -m unittest discover --start-directory tests --verbose`; the launcher demo command is documented in `README.md`.
- Channel loading/update/rendering lives in `release_tools/channel_manifest.py`, `release_tools/update_engine.py`, and `release_tools/patch_notes.py`. `release_tools/manifest.py` also preserves the earlier coordinated manifest contract used by legacy fixtures.

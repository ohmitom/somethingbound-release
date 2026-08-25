# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

## Project map

- The launcher channel contract is authoritative in `docs/release-channel-manifest.md` and `schema/release-manifest.schema.json`. `channels/` holds the live pointers players actually read; `fixtures/manifest.local-dev.json` is the offline example.
- Run the full dependency-free validation with `python3 -m unittest discover --start-directory tests --verbose`. `README.md` documents every launcher and publish command.
- Publishing is `release_tools/publish.py`, driven from the game repository by `tools/publish-playtest.ps1`. Installing is `release_tools/launcher_core.py` over `install_tree.py`, `settings.py`, and `game_process.py`, with the window in `gui.py`.
- Channel parsing and single-file update mechanics stay in `channel_manifest.py`, `update_engine.py`, and `patch_notes.py`. `manifest.py` preserves the earlier coordinated contract used by legacy fixtures.

## The three repositories

- `ohmitom/SomethingBound` is the Unity client. It builds releases through `tools/publish-playtest.ps1`.
- `ohmitom/somethingbound-release` is this repository: the launcher and the channel players read.
- `ohmitom/somethingbound-server` is the private server, deployed to Railway at
  `https://somethingbound-server-production.up.railway.app`. That URL is the launcher's default
  server endpoint in `release_tools/settings.py`. `dispatch-railway.yml` reaches the server
  repository through the `SOMETHINGBOUND_SERVER_REPOSITORY` Actions variable, which is why the
  name appears nowhere in that workflow.

## Sharp edges

- Builds install into `builds/<version>+<short sha>/` with the active one named by `current.json`. Activation is a pointer replace, never a directory rename, because a directory cannot be swapped atomically on Windows once the destination exists. Anything that changes activation must keep that property.
- The fixture artifacts in `fixtures/artifacts/` are hashed byte for byte. `.gitattributes` marks them `-text`; without it a Windows checkout rewrites their line endings and six tests fail on a clean clone.
- Windows PowerShell turns any stderr line from a native executable into a terminating error under `ErrorActionPreference = 'Stop'`. PyInstaller and unittest both report progress on stderr, so the `.ps1` scripts here check `$LASTEXITCODE` instead. See `Invoke-Native` in `launcher_build/build-launcher.ps1`.
- Publishing uploads the release but deliberately does not commit the channel pointer. Until that commit is pushed, no launcher sees the release.
- `raw.githubusercontent.com` serves channel pointers with `max-age=300` and honours neither a `no-cache` request header nor a cache-busting query; both were measured. A launcher can be five minutes behind a release. When a publish looks like it did nothing, wait it out before debugging.
- Anything a frozen launcher starts must be given `self_update.clean_environment()`. PyInstaller's onefile bootloader marks its process tree with `_PYI*`, and a child that inherits them validates its parent's image against its own and refuses to start. After a self-update the parent is the superseded executable under its retired name, so the replacement dies with "parent process has different executable".
- Test the packaged executable, not just the source. The two bugs above and the byte-order-mark one were all invisible from a source checkout.

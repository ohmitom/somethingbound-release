# SomethingBound release

This repository is the SomethingBound launcher and the release channel it
watches. It packages a Windows client build, publishes it as a GitHub release,
and installs it on a player's machine with verification, an atomic activation,
and a retained previous build.

It has no third-party runtime dependencies. The launcher is CPython and Tk.

## The two halves

**Publishing** turns a client build into a release. `release_tools/publish.py`
packages the build, derives patch notes from the game repository's history since
the last published release, writes a channel manifest, uploads the release, and
updates the channel pointer in `channels/`.

**Installing** is what a player runs. `release_tools/launcher.py` opens a window
that reads the channel pointer, compares it with what is installed, downloads
and verifies the artifact, activates it, and starts the client against the
selected server.

The manifest in `channels/` is the only thing joining them, and its contract is
in [`docs/release-channel-manifest.md`](docs/release-channel-manifest.md) and
[`schema/release-manifest.schema.json`](schema/release-manifest.schema.json).

## Run the launcher

```sh
python -m release_tools.launcher
```

That opens the window. The same operations are available without one, which is
how a playtest problem gets diagnosed over a terminal:

```sh
python -m release_tools.launcher status
python -m release_tools.launcher update
python -m release_tools.launcher play
python -m release_tools.launcher rollback
python -m release_tools.launcher config --server https://your-server.example
```

`--data-dir` points every command at a different `launcher.json`, which is how
to try a channel without touching the real install.

### Settings

Settings live in `launcher.json` under `%LOCALAPPDATA%\SomethingBound`, beside
the install rather than inside it, so replacing or rolling back a build never
discards the player's server choice.

| Setting | Meaning |
|---|---|
| `serverEndpoint` | The server the client connects to. HTTPS, or HTTP for localhost only. |
| `channel` | Which release channel to follow. |
| `manifestUrl` | Explicit channel source. Blank uses the published channel for `channel`. |
| `installDir` | Where builds are installed. Blank uses local application data. |
| `autoUpdate` | Whether the window checks the channel when it opens. |

## Package the executable

```powershell
./launcher_build/build-launcher.ps1
```

This runs the tests, creates an isolated build environment under
`launcher_build/.venv`, and produces `launcher_build/dist/SomethingBoundLauncher.exe`.
PyInstaller is a build-time tool only and is never imported by the launcher.

## Publish a build

From the game repository, one command builds the client and promotes it:

```powershell
./tools/publish-playtest.ps1 -Version 0.3.0
```

That is a dry run. It builds, packages, and reports exactly what would be
released without uploading anything. Add `-Publish` to create the release:

```powershell
$env:SOMETHINGBOUND_RELEASE_TOKEN = '<token with contents:write on this repo>'
./tools/publish-playtest.ps1 -Version 0.3.0 -Publish
```

Publishing uploads the artifact and manifest to a GitHub release and rewrites
`channels/<channel>.json`. **The promotion is not finished until that pointer is
committed and pushed**, because the pointer is what installed launchers read.
Keeping that step manual means an upload can be checked before every player
sees it.

## How an install cannot corrupt itself

Every build lives in its own immutable directory and the active build is a
pointer file, so choosing which build runs is one atomic `os.replace` of a small
JSON file. A directory cannot be swapped atomically on Windows once the
destination exists, which is why the pointer exists at all.

1. The manifest is validated before anything is downloaded, and a manifest
   below the installed version is refused.
2. The artifact is downloaded to a temporary file and checked against the
   declared byte count and SHA-256.
3. It is unpacked into staging. Archive entries with absolute paths, parent
   traversal, or symbolic links are refused rather than sanitised.
4. The staged payload is moved into its versioned directory.
5. The pointer is replaced. Only now does the new build become active.

A failure at any step leaves the previous build active and its payload intact.
`rollback` moves the pointer back, and because no payload was deleted, a
rollback is itself reversible.

These are integrity checks, not a signature system. Code signing and signed
manifests remain later work.

## Local development without a host

The manifest source may be a local path, so a channel can be exercised entirely
offline:

```sh
python -m release_tools.launcher --data-dir /tmp/dev config \
  --manifest-url "$PWD/fixtures/manifest.local-dev.json"
```

`fixtures/manifest.local-dev.json` and `fixtures/artifacts/` are a complete
local channel. `python -m http.server` plus an `http://localhost:<port>/` URL
covers the HTTP path.

## Tests

```sh
python -m unittest discover --start-directory tests --verbose
```

Everything is deterministic and offline: no test reaches the network.

## Earlier coordinated release tooling

`release_tools/compatibility.py`, `release_tools/server.py`,
`release_tools/deployment.py`, the `dispatch-railway.yml` and
`publish-release.yml` workflows, and the legacy fixture manifests predate the
channel contract above. `server.py` is still live - the launcher validates
endpoints with it - but the coordinated manifest path is superseded and kept
only pending the server work. Do not add a compatibility layer or a second
schema on top of it. It stays covered by the full test command.

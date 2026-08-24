# SomethingBound release

This repository contains the dependency-free foundation for a Windows launcher
release channel. It validates local channel metadata, verifies an artifact, and
installs it with an atomic swap. It does **not** host releases, schedule
updates, sign binaries, connect to Railway, or integrate with the game.

## Release-channel contract

The public contract is documented in
[`docs/release-channel-manifest.md`](docs/release-channel-manifest.md) and
machine-readable at [`schema/release-manifest.schema.json`](schema/release-manifest.schema.json).
The top-level identity fields are exactly:

```text
channel, version, gitSha, releasedAt, notes, artifacts
```

`notes` contains a human `summary` and exact full-SHA commit entries. Each
artifact contains `name`, `url`, `sha256`, and `size`. `url` can be a local path,
a `file:` URL, or HTTP(S); only local files and local HTTP are intended for this
foundation. The server `/version` naming remains exact: `version`, `gitSha`, and
`protocolVersion` where that server contract applies—never aliases such as
`release` or `protocol`.

`fixtures/manifest.local-dev.json` and
`fixtures/artifacts/launcher-local-dev.txt` are a complete local channel.

## Run from a clean checkout

The tools use only the Python standard library (Python 3.11+ recommended):

```sh
python3 -m unittest discover --start-directory tests --verbose
python3 -m release_tools.validate_manifest \
  fixtures/manifest.local-dev.json \
  --artifact launcher-local-dev.txt=fixtures/artifacts/launcher-local-dev.txt
```

Run the launcher foundation end to end. The command seeds an old installed
identity, resolves the relative local artifact path from the manifest directory,
verifies its size and SHA-256, atomically installs it, and prints the patch
notes with the running Git SHA:

```sh
install_dir="$(mktemp -d)"
python3 -m release_tools.launcher \
  --manifest fixtures/manifest.local-dev.json \
  --install-dir "$install_dir" \
  --installed-version 1.0.0 \
  --installed-git-sha 0000000000000000000000000000000000000000
rm -rf "$install_dir"
```

For a local HTTP source, serve the artifact directory with
`python3 -m http.server` and use an `http://localhost:<port>/...` URL in a
copy of the development manifest. No production host is required.

## Update safety

`release_tools.channel_manifest` parses JSON, validates the channel schema,
rejects malformed or regressive manifests, and rejects unsafe artifact names and
URLs. `release_tools.update_engine` then:

1. compares installed `version` and `gitSha` to the manifest;
2. downloads to a temporary file;
3. verifies the declared byte count and lowercase SHA-256;
4. swaps the verified payload into `current` with `os.replace`;
5. retains the prior payload as `previous` and prior state as `previous.json`.

A simulated failure after the payload swap restores the old payload. Signed
manifests, code signing, automatic scheduling, hosted serving, and product
process hand-off are intentionally later work.

## Existing release contract tools

The repository also retains the earlier coordinated release/server compatibility
fixtures and checks in `release_tools/compatibility.py`, `release_tools/server.py`,
and the legacy fixture manifests. They remain covered by the full test command;
the launcher foundation uses the separate channel contract above.

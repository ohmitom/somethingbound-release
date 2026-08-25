# Release-channel manifest

`schema/release-manifest.schema.json` is the machine-readable contract consumed by
this launcher's local update foundation. `fixtures/manifest.local-dev.json` is a
complete local-development example.

## Identity contract

The top-level fields are intentionally exact - no `release`, `releaseNotes`,
`sizeBytes`, or alternate aliases are used:

```json
{
  "channel": "local-dev",
  "version": "1.2.0",
  "gitSha": "<40 lowercase hex characters>",
  "releasedAt": "2026-08-24T12:00:00Z",
  "notes": {
    "summary": "Human-readable summary.",
    "commits": [
      {
        "sha": "<full 40-character commit SHA>",
        "subject": "Short commit subject",
        "category": "feat",
        "scope": "launcher",
        "pr": 123
      }
    ]
  },
  "artifacts": [
    {
      "name": "SomethingBoundLauncher.exe",
      "url": "file:///absolute/path/or/http://localhost:8000/launcher.exe",
      "sha256": "<64 lowercase hex characters>",
      "size": 123456
    }
  ]
}
```

`notes.commits[].pr` is optional and may be a number, string, or `null`.
`scope` is required by the shape but may be `null`. The launcher renders the
first seven characters of each full commit SHA and retains the exact SHA in the
manifest. Artifact names are filenames, not paths; this prevents a manifest
from escaping the install directory.

`url` may be a plain path, a `file:` URL, or HTTP(S). Published channels use
HTTPS release-asset URLs; local paths and `http://localhost` serve development
channels. Credentials in URLs and remote-host UNC file URLs are rejected.

The manifest itself is fetched from the same set of sources, with one extra
rule: a manifest URL must be HTTPS unless its host is localhost. A manifest
decides which bytes a player executes, so it is never fetched over plain HTTP
from a remote host. Manifests are also size-capped while being read, so a wrong
URL cannot stream without bound into launcher memory.

The server `/version` identity uses the exact names `version`, `gitSha`, and,
where that contract applies, `protocolVersion`. The launcher channel manifest
has no protocol field: it must not invent `protocol`, `release`, or another
identity alias. If a later server-facing contract carries the protocol value,
its field remains `protocolVersion`.

## Validation and safety rules

- `version` and every version supplied as installed state are SemVer 2.0.0.
- A manifest below the installed version is rejected; equal versions are only
  eligible when the Git SHA differs.
- `gitSha` and commit `sha` are 40 lowercase hexadecimal characters.
- Artifact `sha256` is exactly 64 lowercase hexadecimal characters and `size`
  is a non-negative byte count.
- The loader rejects malformed JSON, missing fields, unknown fields, duplicate
  artifact names, invalid timestamps, unsafe artifact names, and unsupported
  URLs before download.
- Downloaded bytes are checked for both declared size and SHA-256 before the
  install swap.

These checks are structural and integrity checks, not a signature system. Code
signing and hosted manifest authentication are later work.

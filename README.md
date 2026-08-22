# SomethingBound release

This public repository contains the release-control contracts for SomethingBound. It
publishes safe metadata and validation tooling; it does **not** contain client or
server source, build binaries, Railway credentials, or a default public server.

## Repository layout

- `schema/release-manifest.schema.json` — versioned release-manifest contract (v1).
- `release_tools/manifest.py` — dependency-free manifest and local artifact checks.
- `release_tools/compatibility.py` — deterministic launcher update selection and
  client/server compatibility checks.
- `release_tools/server.py` — endpoint resolution and release-bound server health
  checks.
- `release_tools/deployment.py` — sanitized, opt-in Railway deployment preflight;
  it never calls Railway.
- `fixtures/` — small, public manifests, health responses, and text artifacts used
  by the tests.
- `tests/` — deterministic contract tests.
- `.github/workflows/` — validation, manual release publishing, and optional server
  repository dispatch templates.

## Local validation

Use Python 3. The tools use only the standard library:

```sh
python3 -m release_tools.validate_manifest \
  fixtures/manifest.valid.json \
  --artifact client=fixtures/artifacts/client.txt \
  --artifact server=fixtures/artifacts/server.txt
python3 -m release_tools.validate_manifest \
  fixtures/manifest.update.json \
  --artifact client=fixtures/artifacts/client.txt \
  --artifact server=fixtures/artifacts/server.txt
python3 -m unittest discover --start-directory tests --verbose
```

A launcher-style selection check can be run without network access:

```sh
python3 -m release_tools.compatibility 1.4.0 fixtures/manifest.update.json \
  --version client=1.4.0 \
  --version protocol=2.1.0 \
  --version content=3.0.0 \
  --version ruleset=2.2.0 \
  --version schema=1.1.0
```

The selector ignores malformed, incompatible, and non-newer candidates and chooses
the greatest compatible SemVer release. Artifact installation must happen only
after the downloaded bytes pass the manifest's SHA-256 and size checks.

## Release-manifest contract

A manifest has one release identity: the human-facing `release.version` and the
full lowercase `release.gitSha`. It also records a UTC build timestamp, coordinated
SemVer values for `client`, `server`, `protocol`, `content`, `ruleset`, and `schema`,
and the corresponding `minimumCompatibleVersions` window.

Both client and server artifacts must have an HTTPS URL without credentials, a
lowercase SHA-256 checksum, and a non-negative byte size. Release notes contain a
`from`/`to` range: `to` must equal the release version, and `from` must equal
`previousRelease.version` (or be `null` for an initial release). `rollbackTarget`
is an older, known-good release reference; it is metadata for an operator rollback,
not an automatic database migration.

`release_tools.validate_manifest` is the executable contract and reports all
cross-field errors. The JSON Schema is included for consumers that also support
JSON Schema validation.

## Launcher and server connection

The endpoint resolver accepts these operator-provided environment values:

- `SOMETHINGBOUND_SERVER_ENDPOINT` — an explicitly selected endpoint; this always
  takes precedence.
- `RAILWAY_SERVER_ENDPOINT` — a configured Railway endpoint.
- `RAILWAY_PUBLIC_DOMAIN` — Railway's configured public domain; when it is present
  without a scheme, the resolver uses `https://`.

Discovered and non-local endpoints must use HTTPS and may not contain credentials.
Explicit `http://localhost`, `http://127.0.0.1`, or `http://[::1]` is allowed only
for local development. If no value is configured, resolution returns
`manual-fallback` with no URL. The launcher must show that state and ask the
operator to select an endpoint; it must not invent or assume a public server.

After connecting, the server health response must report `status: "ok"`, the exact
manifest release version and Git SHA, all six version dimensions, and a non-negative
`migrationLevel`. `validate_server_health` rejects a mismatched release, an endpoint
outside the manifest's compatibility window, or an unexpected migration level. It
only validates supplied data: it does not make a request or execute migrations.

## Coordinated promotion and rollback

1. Build client and server artifacts from the same source release identity and
   publish one manifest.
2. Validate both artifacts and the compatibility window.
3. Deploy the server through the operator's deployment system and run the required
   database migration as an explicit deployment/operator hook.
4. Check the server health response against the manifest identity, versions, and
   migration level before enabling the client update.
5. Let the launcher select and checksum-verify the client artifact.

If promotion fails, stop client rollout and use the manifest's `rollbackTarget` and
its matching server/client artifacts. Database rollback or forward-compatible
migration handling remains an explicit operator decision; this repository does not
pretend to perform or reverse migrations.

## GitHub Actions and Railway setup

`validate.yml` runs the local checks on pull requests and relevant pushes.
`publish-release.yml` is a manual workflow: the operator supplies an existing tag
named `v<release.version>`, a manifest path, and already-built client/server artifact
paths. It validates the tag, manifest, checksums, and release-note range before
creating a GitHub release. This repository does not build either product.

`dispatch-railway.yml` is also manual and is safe by default. Deployment requires
all of the following operator configuration:

- repository variables: `SOMETHINGBOUND_RAILWAY_DEPLOY=true`,
  `SOMETHINGBOUND_SERVER_REPOSITORY`, and `SOMETHINGBOUND_SERVER_DEPLOY_EVENT`;
- repository secrets: `RAILWAY_PROJECT_ID`, `RAILWAY_SERVICE_ID`,
  `RAILWAY_TOKEN`, and `SOMETHINGBOUND_SERVER_REPOSITORY_TOKEN`;
- workflow inputs: a checked-out manifest path and the HTTPS URL of its published
  manifest.

If opt-in, any required value, or the configured repository/event format is absent,
the workflow skips dispatch. It does not print secret values and does not send the
Railway token. When enabled, it sends a release payload to the configured server
repository; that receiver must own the Railway credentials, deployment command,
migration hook, and health endpoint. A `ready` preflight means only that dispatch
is allowed—it is not evidence of a Railway deployment or a healthy server.

### Captain action still required

No Railway connection, deployment, database creation, server-repository dispatch,
or launcher update was exercised while creating this foundation. Before using the
optional path, the captain/operator must connect the intended Railway project and
service, configure the values above in GitHub, choose the server repository and its
workflow event, publish real artifacts and a reachable manifest URL, and verify the
receiver's migration and health checks. Until then, use an explicitly selected
server endpoint or the documented manual fallback.

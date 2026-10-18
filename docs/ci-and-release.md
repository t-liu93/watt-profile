# CI and Container Releases

> **English — source of truth** · [中文](ci-and-release_zh.md)

This delivery step sits between accepted [M3](milestone/m3.md) and [M4](milestone/m4.md). It adds GitHub Actions checks and GHCR delivery for the Logger and Server. Current M4 source includes upload, four-week capacity measurement and recovery checks; `scripts/ci.sh` explicitly runs the capacity selector. The already published `v0.1.0-alpha.1` Logger remains a pre-M4 image without an uploader; a changed image needs a separately authorized new release. No host, Raspberry Pi, Nginx or production service is deployed or configured by these workflows. See the [M4 capacity and owner upgrade procedure](milestone/m4.md#owner-pi-upgrade-and-acceptance-handoff).

`v0.1.0-alpha.2` introduced M4 upload/recovery and LOGGER-001 repair. [Logger alpha.3](https://github.com/t-liu93/watt-profile/releases/tag/v0.1.0-alpha.3), published on 2026-10-18 from `ae54b63`, fixes quadratic backlog batch construction. [Main CI](https://github.com/t-liu93/watt-profile/actions/runs/39126887798), [release CI](https://github.com/t-liu93/watt-profile/actions/runs/39126887699), all four native image checks and publication passed. Logger and Server alpha.3 images provide AMD64/ARM64 and anonymous access; the owner's Server alpha.2 remains compatible and need not be upgraded. Previous tags are unchanged, and this prerelease does not set `latest`. The owner deployed Logger alpha.3, confirmed backlog clearance and concurrent collection/upload without further reported errors, and accepted M4/MVP. See the [final acceptance and evidence limits](milestone/m4.md#final-owner-acceptance-and-mvp-closure-2026-10-18).

## Checks and release contract

1. Every branch push, non-version tag push, pull request and manual CI dispatch runs `CI`. A `v*` tag instead starts `Release images`, which calls the same complete CI against the resolved tag commit before building any release image. Invalid version tags fail validation.
2. CI installs `requirements-dev.lock` on Python 3.12, installs Chromium and runs `bash scripts/ci.sh`: Ruff lint/format, the default suite with temporary PostgreSQL, explicit Logger/Server browser tests, Server development/standalone Compose tests, backup/restore acceptance, the full synthetic 241,920-record capacity selector, M4 HTTP recovery and Logger/Server Compose/browser acceptance. The default suite's opt-in skips are covered by those explicit selectors.
3. Release tags must be strict `vMAJOR.MINOR.PATCH`, optionally with SemVer prerelease/build metadata. Four GitHub-hosted native builds cover Logger/Server × `linux/amd64`/`linux/arm64` using `ubuntu-24.04` and `ubuntu-24.04-arm`. Neither self-hosted runners nor QEMU registration is needed.
4. Each candidate runs without network or physical devices, as UID/GID 10001: native imports, packaged assets/migrations and CLI startup checks. The Logger additionally creates and reopens synthetic SQLite data and verifies identity, Token and pending snapshot preservation.
5. Only after complete CI and all four native smoke tests pass, a publishing job with `packages: write` uses the built-in `GITHUB_TOKEN`. It loads and publishes **exactly the tested images**, then combines their digests into one multi-platform image per component. Build jobs have read-only repository permissions; no separate registry password is required. Third-party actions are pinned to commit SHAs.

Ordinary pushes build disposable images as part of existing Compose regression tests; **release image builds and registry publication happen only for version tags**. Actions do not SSH into devices or automatically update any running container. Local validation cannot establish that GitHub permissions, cloud runners or GHCR publication work; that requires the first authorized push/tag run.

## Images and tags

For this repository the packages are:

| Package | Architectures | Tags |
| --- | --- | --- |
| `ghcr.io/t-liu93/watt-profile-logger` | AMD64, ARM64 | version, full `sha-COMMIT`, stable `latest` |
| `ghcr.io/t-liu93/watt-profile-server` | AMD64, ARM64 | version, full `sha-COMMIT`, stable `latest` |

Names are derived from the lowercase GitHub repository, so a fork publishes to its own namespace. `v0.1.0` produces `:0.1.0`; `v0.1.1-rc.1` produces `:0.1.1-rc.1` without changing `latest`. Build metadata `+build.2` becomes `_build.2` in Docker tags. Internal `build-RUN-ATTEMPT-ARCH` tags retain platform images for the manifest; tested tar artifacts expire after one day. Pin a version or manifest digest for deployment. `latest` follows the last successfully published stable tag, including an older version if it is released later.

After reviewing and committing the implementation, an owner can push the branch, check the Actions result, and separately authorize a release, for example:

```sh
git tag v0.1.0
git push origin v0.1.0
```

Use a new version for changed source. GitHub repository settings must permit Actions and package writes with `GITHUB_TOKEN`; an existing package must grant the repository access. GHCR packages may initially be private. Choose public visibility in each package's settings for anonymous Pi pulls, or authenticate Docker on the Pi with a token allowed to read those packages. Do not place that token in Compose files or this repository. See [GitHub package publishing](https://docs.github.com/en/actions/use-cases-and-examples/publishing-packages/publishing-docker-images) and [Container registry authentication/visibility](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## Pulling the Logger on a Raspberry Pi

Use Raspberry Pi OS **64-bit** and Docker Compose v2 or newer. `uname -m` should report `aarch64`; Docker selects ARM64 from the manifest automatically. The Pi needs the repository's Compose file and environment configuration, but does not need Python or a local image build.

1. Copy `deploy/logger/.env.example` to ignored `deploy/logger/.env.local`. Set the operator name, absolute private password-file path, stable `/dev/serial/by-id/...` device, its **numeric host group ID**, reachable bind address and published port. The password is 16–256 characters, file mode 0600 owned by UID/GID 10001; its parent directory is private (0700). These are owner-managed host settings; see [M2 operating instructions](milestone/m2.md#container-operations-m2-09).
2. Add an actual published version, for example `WATT_LOGGER_IMAGE=ghcr.io/t-liu93/watt-profile-logger:0.1.0-alpha.3`. Leave this variable unset when building locally. The initializer and Logger use the same selected image. For direct LAN HTTP choose an appropriate Pi interface/port and keep `WATT_LOGGER_COOKIE_SECURE=false`; HTTPS is managed outside the container.
3. From the repository root, validate and start only Logger and its initializer:

```sh
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml config --quiet
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml up --no-build --pull always -d --wait --wait-timeout 120 logger
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml ps --all
```

Open `http://PI_ADDRESS:CONFIGURED_PORT`, log in and create a site, use read preview, then create a paused deployment and start collection. A preview alone stores no readings. Collected snapshots survive restart/recreation in the named volume. The published alpha.3 image includes upload, the preview repair and the backlog performance fix; alpha.1 does not upload. Physical P1 compatibility, storage allocation and Pi resource use still require the owner walkthrough. Before upgrading to alpha.3, follow the [online backup, copy-out, upgrade and rollback steps](milestone/m4.md#owner-pi-upgrade-and-acceptance-handoff); preserve the Compose project and volume.

Normal shutdown preserves the database, identity and Token:

```sh
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml down
```

Do not use `down --volumes` on a real Logger installation. Upgrade by changing the pinned image version and repeating `up --no-build --pull always`; back up existing data first using M2 operations.

For the Server, set `WATT_SERVER_IMAGE=ghcr.io/t-liu93/watt-profile-server:VERSION` in its ignored environment file and use the same `up --no-build --pull always` pattern with `deploy/server/compose.yml` and service `server`. PostgreSQL remains private, with one exposed application HTTP port; credentials and external Nginx stay owner-managed as described in [M3](milestone/m3.md#m3-11-deployment-operations).

## Local frontend artifact and image builds

Use Python 3.12, Node **24.22.0** and npm **11.16.0** on `PATH`. From the repository root, in one shell:

```sh
python scripts/frontend_artifact.py build
export WATT_FRONTEND_DIGEST="$(python -c 'import json; print(json.load(open("frontend/dist/artifact.json"))["digest"])')"
python scripts/frontend_artifact.py stage --expected "$WATT_FRONTEND_DIGEST"
export WATT_DEV_IMAGE_TAG=watt-local-UNIQUE
docker compose -p "$WATT_DEV_IMAGE_TAG" -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 server
# Direct Docker alternative after the same build/export/stage:
docker build --build-arg WATT_FRONTEND_DIGEST -f deploy/server/Dockerfile -t watt-profile-server:local .
```

Choose an unused disposable project name and `WATT_DEV_IMAGE_TAG` before starting; see [Server containers](../README.md#server-containers) for standalone credentials/configuration and startup. The build performs locked installation, component preparation/verification, lint, type checking, tests and bundling. Export/stage again after rebuilding. CI/release reuse the producer's trusted digest with `stage --expected`; they must not derive a replacement digest from an untrusted artifact. A missing digest fails image building with configuration instructions. Published-image `up --no-build` needs no local artifact/digest.

## Local verification and cleanup

Install the development lock in an isolated Python 3.12 virtual environment, then install Chromium (`python -m playwright install --with-deps chromium`) into a new `PLAYWRIGHT_BROWSERS_PATH`. Set `WATT_CI_PYTHON` to that environment's Python if it is not activated, then run `bash scripts/ci.sh` from the checkout. Docker and Compose must be available. Database/browser services use only synthetic data, random names and temporary loopback ports; deployment database URLs are discarded. The script owns and removes its temporary directories and Logger project/image. Server and PostgreSQL fixtures remove their own projects, volumes and images. Existing resources are never adopted; no global Docker prune is used. The caller removes its separately created virtual environment, browser directory and dependency cache when finished.

Acceptance requires workflow syntax validation, release-tag boundary tests, default and explicit CI suites, native local candidate smoke tests, and a resource cleanup audit. Cloud AMD64/ARM64 build/publication and the real Pi walkthrough must be recorded separately after owner-triggered release execution.

## M5 artifact reuse and verification status

The checked artifact producer pins Node 24.22.0/npm 11.16.0 and both frontend locks, installs/prepares/verifies components, runs frontend checks and builds once. Its digest accompanies the uploaded artifact. Consumers validate that trusted digest and file/lock hashes, then stage the same bytes into Python packaging and native Server image builds. Do not rebuild frontend independently per architecture. The release matrix still covers both Logger and Server on native AMD64 and ARM64; Server smoke verifies packaged frontend without Node or network.

M5 full local CI, synthetic Compose, four-week analysis performance and both AMD64 offline image smokes passed. Cloud workflow execution/native ARM64 for M5 remain unrun: this local implementation has no push/tag/publication authorization. The historical M4 release results above remain separate. See the [M5 acceptance record](milestone/m5.md#acceptance-record) for exact measurements and pending owner walkthrough/component freeze, and [Server upgrade instructions](../README.md#m5-analysis-workspace-and-upgrade) for automatic known-schema migration and owner backup/recovery boundaries.

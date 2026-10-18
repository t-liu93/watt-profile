# CI and Container Releases

> **English — source of truth** · [中文](ci-and-release_zh.md)

This delivery step sits between accepted [M3](milestone/m3.md) and [M4](milestone/m4.md). It adds GitHub Actions checks and GHCR delivery for the Logger and Server. Current M4 source includes upload, four-week capacity measurement and recovery checks; `scripts/ci.sh` explicitly runs the capacity selector. The already published `v0.1.0-alpha.1` Logger remains a pre-M4 image without an uploader; a changed image needs a separately authorized new release. No host, Raspberry Pi, Nginx or production service is deployed or configured by these workflows. See the [M4 capacity and owner upgrade procedure](milestone/m4.md#owner-pi-upgrade-and-acceptance-handoff).

`v0.1.0-alpha.2` was [published on 2026-10-16](https://github.com/t-liu93/watt-profile/releases/tag/v0.1.0-alpha.2) with M4 upload/recovery and the LOGGER-001 repair. Logger and Server each provide native-tested AMD64/ARM64 images at `:0.1.0-alpha.2`; alpha.1 is unchanged and this prerelease does not set `latest`. Full release CI and native tests passed. The separate main CI test race, the committed test-only correction `e5127d4` and its successful [main CI](https://github.com/t-liu93/watt-profile/actions/runs/38973592828) are recorded in [M4](milestone/m4.md#owner-walkthrough-and-release-preflight-2026-10-16); owner Pi/backlog/72-hour acceptance remains pending.

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
2. Add an actual published version, for example `WATT_LOGGER_IMAGE=ghcr.io/t-liu93/watt-profile-logger:0.1.0-alpha.2`. Leave this variable unset when building locally. The initializer and Logger use the same selected image. For direct LAN HTTP choose an appropriate Pi interface/port and keep `WATT_LOGGER_COOKIE_SECURE=false`; HTTPS is managed outside the container.
3. From the repository root, validate and start only Logger and its initializer:

```sh
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml config --quiet
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml up --no-build --pull always -d --wait --wait-timeout 120 logger
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml ps --all
```

Open `http://PI_ADDRESS:CONFIGURED_PORT`, log in and create a site, use read preview, then create a paused deployment and start collection. A preview alone stores no readings. Collected snapshots survive restart/recreation in the named volume. The published alpha.2 image includes upload and the preview repair; alpha.1 does not upload. Physical P1 compatibility, storage allocation and Pi resource use still require the owner walkthrough. Before upgrading to alpha.2, follow the [online backup, copy-out, upgrade and rollback steps](milestone/m4.md#owner-pi-upgrade-and-acceptance-handoff); preserve the Compose project and volume.

Normal shutdown preserves the database, identity and Token:

```sh
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml down
```

Do not use `down --volumes` on a real Logger installation. Upgrade by changing the pinned image version and repeating `up --no-build --pull always`; back up existing data first using M2 operations.

For the Server, set `WATT_SERVER_IMAGE=ghcr.io/t-liu93/watt-profile-server:VERSION` in its ignored environment file and use the same `up --no-build --pull always` pattern with `deploy/server/compose.yml` and service `server`. PostgreSQL remains private, with one exposed application HTTP port; credentials and external Nginx stay owner-managed as described in [M3](milestone/m3.md#m3-11-deployment-operations).

## Local verification and cleanup

Install the development lock in an isolated Python 3.12 virtual environment, then install Chromium (`python -m playwright install --with-deps chromium`) into a new `PLAYWRIGHT_BROWSERS_PATH`. Set `WATT_CI_PYTHON` to that environment's Python if it is not activated, then run `bash scripts/ci.sh` from the checkout. Docker and Compose must be available. Database/browser services use only synthetic data, random names and temporary loopback ports; deployment database URLs are discarded. The script owns and removes its temporary directories and Logger project/image. Server and PostgreSQL fixtures remove their own projects, volumes and images. Existing resources are never adopted; no global Docker prune is used. The caller removes its separately created virtual environment, browser directory and dependency cache when finished.

Acceptance requires workflow syntax validation, release-tag boundary tests, default and explicit CI suites, native local candidate smoke tests, and a resource cleanup audit. Cloud AMD64/ARM64 build/publication and the real Pi walkthrough must be recorded separately after owner-triggered release execution.

# Known Issues

> **English — source of truth** · [中文](known-issues_zh.md)

This tracked register records unresolved defects found during deployment and owner walkthroughs. A workaround is not a fix. Resolve each issue on a separate branch and update its status only after the relevant checks pass. The repository's primary branch is `main`.

## LOGGER-002 — Backlog batch construction repeats work quadratically

- **Status:** Code-verified on 2026-10-18 under [M4-11](milestone/m4.md#m4-11--linear-time-pending-batch-construction); alpha.3 publication and physical throughput verification remain pending.
- **Affected release:** Logger `v0.1.0-alpha.2`.
- **Impact and evidence:** Each added record rebuilds, validates and serializes every growing batch prefix. A deterministic synthetic regression counts 40,200 canonical record validations for a 200-record selection. Successful batches already schedule the next immediately; changing the idle scan interval does not remove this CPU bottleneck.
- **Repair branch:** `codex/logger-backlog-performance`. Account for exact encoded bytes incrementally and construct the final validated batch once. Preserve canonical content, bounds, ordering, corruption rejection and atomic acknowledgement; no database migration is needed.
- **Verification:** Default suite **684 passed, 29 skipped**, Ruff lint/format and diff checks passed. On the same 32,786-record synthetic desktop database, 200-record construction changed from 7.8595 s to a three-run median of 0.2084 s (37.7×). Exact selected batch and body bytes matched; pending count was unchanged. This is not a Pi end-to-end upload rate.

## LOGGER-003 — Owner collection errors during real backlog upload need diagnosis

- **Status:** Open; not repaired by LOGGER-002.
- **Observed release:** Owner-upgraded ARM64 Pi Logger `v0.1.0-alpha.2`; the owner confirmed existing data survived and reported successful old-record uploads, then stopped collection while draining backlog.
- **Observed behavior:** Collection reported `storage_full` / `storage_error` despite approximately 24.7 GB available; subsequent resume attempts reportedly showed `snapshot_invalid` / `no_valid_reading`. Upload confirmations continued after the last saved collection reception.
- **Evidence limits:** Runtime maps non-corruption storage failures broadly to `storage_full`; SQLite lock waits are limited to five seconds. Slow storage or write contention is a hypothesis, not an established cause. `snapshot_invalid` covers rejected frames/readings and snapshot validation; `no_valid_reading` means no reading was admitted for at least 15 seconds. Neither proves cable failure. No private exception, raw telegram or physical diagnostic has been captured by the agent.
- **Next acceptance:** Verify backlog throughput after LOGGER-002, then separately diagnose collection using owner-operated status/serial observations. Do not delete pending records, change identity or claim hardware/72-hour acceptance to bypass these errors.

## LOGGER-001 — Standalone preview can leave the Logger stuck in `previewing`

- **Status:** Code-verified on 2026-10-15 in [M4-01–M4-02](milestone/m4.md#task-execution-evidence); integrated into `main` before uploader work. Published in [v0.1.0-alpha.2](https://github.com/t-liu93/watt-profile/releases/tag/v0.1.0-alpha.2) on 2026-10-16; owner hardware verification remains pending. The published `v0.1.0-alpha.1` image is unchanged and still affected.
- **Affected release:** `v0.1.0-alpha.1`, Logger image `ghcr.io/t-liu93/watt-profile-logger:0.1.0-alpha.1`.
- **Observed environment:** Owner-operated Raspberry Pi with a real USB/P1 connection. The standalone diagnostic probe successfully read valid data. No production device or data was accessed by the development agent.
- **Impact:** Local setup can become unusable after preview: creating sites, creating deployments and other mutation controls remain disabled. This is separate from the expected absence of upload functionality before M4.
- **Repair branch:** `fix/logger-preview-cleanup`, based on `main`; M4-01–M4-02 implement the repair and integrate it before uploader work.

### Reproduction and observations

1. Start the standalone Logger and save its container-side serial path `/dev/p1` in **Site and settings**. The host `/dev/serial/by-id/...` mapping and numeric device group belong in the deployment environment file; they do not automatically populate the saved application setting.
2. Before creating a site or deployment, open **Reading preview** and click **Read preview**.
3. A valid reading may appear, followed by `serial_close_failed`. In the owner's observed failure, the application remained in `previewing` after the request ended.
4. **Reload status** continued to show `Collection=previewing`, no session/site, pending count zero, no persisted blocks, and both collection/runtime error fields empty. Mutation buttons remained disabled and there was no preview cancellation control. Refreshing the page did not resolve the runtime state.

Preview should acquire one valid reading and release its temporary serial owner, without saving a snapshot. The UI polls status every two seconds; this does not turn preview into continuous collection. Normal collection requires a site, a deployment and **Start / resume**.

### Evidence and investigation boundaries

Code inspection identifies two relevant paths in `src/watt_profile/logger/runtime.py`: both `_read()` cleanup and `_release()` call `port.close()`, allowing competing close operations; `preview()` calls `_release()` before `finish_preview()`, so a release exception skips the state reset. In `logger/static/logger.js`, `controls()` disables mutation buttons while the runtime reports `previewing`. Some release failures are not copied to `runtime_error`, explaining why an error may be absent from status.

Temporary tests using only fake serial handles and temporary SQLite reproduced duplicate-close failure and a preview state left behind by a close exception. Those experimental source/test changes have been rolled back; they are not accepted regression coverage or a delivered fix. The exact driver-level exception on the owner's Pi has not been captured. Hardware compatibility and resource acceptance remain separate from this issue.

### Owner workaround

In the device's directory containing `compose.yml` and `.env`, restart only the Logger:

```bash
docker compose --env-file .env -p watt-logger -f compose.yml restart logger
```

Refresh the page and log in again. This preserves the named data volume, saved settings, device identity, Token and existing records, while interrupting collection during the restart. Avoid standalone **Read preview** in the affected release. Create/select a site, click **Create paused deployment / switch site**, and start collection when ready. The owner confirmed site/deployment creation after recovery and subsequently reported ongoing Pi 4 collection during M4 planning, with no server origin and increasing pending records. This is owner-reported pre-M4 operation, not verification of the repair or uploader. Do not remove the data volume as a recovery action.

### Acceptance criteria for the later repair

- Serialize or otherwise give one owner responsibility for closing each serial handle. Preserve exclusive ownership; never open a replacement while a prior handle or reader remains active.
- Restore an appropriate lifecycle and release preview bookkeeping on success, cancellation, timeout, reader startup failure and serial cleanup failure. No completed request may leave an unexplained permanent `previewing` state.
- Surface real close/join failures in status and API responses, without disclosing raw telegrams, credentials or private exception text. A visible error must not imply that an unreleased handle is safe to reuse.
- Add deterministic fake-serial regression tests for competing shutdown paths and genuine close failure. Add API/browser coverage for repeated preview, status responsiveness, restored controls, site/deployment creation and subsequent collection. Preview must not create stored/pending snapshots.
- Run the applicable runtime, lifecycle, API, browser and container persistence checks using isolated synthetic resources; clean up only those resources. Arrange real Pi verification separately with the owner.
- Record verification and release status here. Do not move or overwrite `v0.1.0-alpha.1`; a later repair release needs its own authorization and version.

### Code verification, 2026-10-15

Reader-only close ownership and guaranteed preview teardown are implemented; real close/join failures remain explicit and prevent unsafe reuse. Runtime/lifecycle/probe/maintenance: 120 passed; API/runtime/synthetic: 58 passed; isolated browser: 7 passed; disposable Logger Compose/persistence: 1 passed. Both task diffs passed fresh independent reviews. Only synthetic serial and temporary resources were used, and the disposable project was removed. The repair is published in alpha.2; owner paused-preview recovery and real driver behavior remain pending. The existing workaround still applies to the affected alpha.1 release.

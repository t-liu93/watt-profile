# Known Issues

> **English — source of truth** · [中文](known-issues_zh.md)

This tracked register records unresolved defects found during deployment and owner walkthroughs. A workaround is not a fix. Resolve each issue on a separate branch and update its status only after the relevant checks pass. The repository's primary branch is `main`.

## LOGGER-001 — Standalone preview can leave the Logger stuck in `previewing`

- **Status:** Open; deferred on 2026-10-14 and now scheduled in [M4-01–M4-02](milestone/m4.md#m4-01--serial-owner-and-preview-cleanup-repair), design ready 2026-10-15. No fix is included in the primary branch or the published image.
- **Affected release:** `v0.1.0-alpha.1`, Logger image `ghcr.io/t-liu93/watt-profile-logger:0.1.0-alpha.1`.
- **Observed environment:** Owner-operated Raspberry Pi with a real USB/P1 connection. The standalone diagnostic probe successfully read valid data. No production device or data was accessed by the development agent.
- **Impact:** Local setup can become unusable after preview: creating sites, creating deployments and other mutation controls remain disabled. This is separate from the expected absence of upload functionality before M4.
- **Planned repair branch:** `fix/logger-preview-cleanup`, based on `main`. M4 implements and integrates the repair before uploader work; the branch contains no repair implementation at the time of this record.

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

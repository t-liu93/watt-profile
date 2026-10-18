# Watt Profile: MVP Roadmap

> **English — source of truth** · [中文](mvp-roadmap_zh.md)

Status, 2026-10-18: M1–M3 are accepted (M3 on 2026-10-14). M4-01–M4-10 software implementation, automated checks and independent reviews are complete. The owner accepted the guided browser walkthrough within its recorded manual coverage limits on 2026-10-16, and confirmed Server deployment, HTTPS/admin login and manual local/remote backup on 2026-10-18. The M4 alpha.2 pilot is published. Pi version/upgrade, real-backlog upload, storage allocation, physical compatibility/resources and continuous 72-hour acceptance remain pending; M4 as a whole remains open.

A delivery preparation step precedes M4: [CI and container releases](ci-and-release.md). Every push runs complete checks; version tags build and publish separate AMD64/ARM64 Logger and Server images to GHCR. The published `v0.1.0-alpha.2` Logger includes M4 upload/backlog recovery and LOGGER-001 repair; the unchanged alpha.1 image remains pre-M4. Physical-hardware acceptance has not started.

This roadmap defines the first usable release. See the [high-level architecture](architecture-and-roadmap.md) for the system direction and the [protocol handover](home-automation-energy-handover.md) for reusable source code and its limitations.

## 1. Goal and release boundary

Deliver this workflow for an internal operator:

**Prepare a Raspberry Pi → configure a site and P1 through its local page → copy registration details to the server → collect, store locally, and upload → find the site's data and export CSV.**

The Logger targets the owner's existing ARM64 Raspberry Pi 4, with its actual RAM recorded and a continuous 72-hour M4 run. On 2026-10-14 the owner replaced the earlier Pi 4 1 GB/Pi 3B+ physical acceptance requirement; no additional board is required and Pi 3B+ compatibility remains unverified future work. The Centralized Server runs in containers on the owner's home server with public access. Start with one Logger and one P1 source per monitoring session; the system is expected to grow to a few devices. The sole MVP meter profile is Netherlands DSMR 5 (`dsmr5_nl`), based on DSMR 5.0.2; additional meter profiles are deferred.

The original planning target was approximately one to two weeks of development, assuming the hardware, P1 cable, test meter, and host environment were available. The original milestone estimates totaled 6–10 working days, not a delivery guarantee. Re-estimate M4 implementation with its known-issue repair scope; the continuous 72-hour hardware run adds calendar time. Meter-specific adaptation and hardware failures can extend the schedule. Software readiness and the completion of several weeks of field monitoring are separate outcomes.

| Included in MVP | Deferred |
| --- | --- |
| Local configuration, P1 read test, collection controls, and status | Modbus TCP, serial RS485/RTU, and other source types |
| Site UUID, device credential, and manual server registration | Automatic provisioning and remote configuration/upgrades |
| Ten-second snapshots, SQLite backup, and reliable batch upload | Charts, derived energy statistics, AI reports, and PDF export |
| Server authentication, storage, site/device status, filtered queries, and CSV | React interface expansion, customer accounts, battery/PV sizing |

SSH, Ethernet addressing, DHCP, Wi-Fi, internet access, and WireGuard are prepared and managed by the owner outside this project. The application does not configure the host network, run a DHCP server, or manage VPN keys. On site, the operator connects a computer to the Pi, establishes host-level connectivity, and opens the local page by its reachable address.

## 2. Identity, configuration, and data ownership

### Site UUID and monitoring sessions

- When creating a new site, the Logger generates a random UUID v4 as `site_id` and saves it with the address and description before collection starts. This works offline. The server adopts the supplied UUID rather than generating a replacement.
- A Site UUID identifies a site, not a device or credential. Editing the address or description, restarting the Logger, or changing its Token does not change that UUID. It is not derived from the address or Token.
- The local page offers **create a new site** and **use an existing Site UUID**. Revisit an existing site with its UUID and a new monitoring session; do not infer identity by matching addresses.
- Keep separate identities for the Logger device, site, monitoring session, measurement point, and source. The MVP has one P1 source per session; it does not need a multi-meter configuration interface.
- Every stored snapshot retains its site, device, session, measurement point, source, and stable record ID. Moving a Logger to a new site starts a new session. Already stored and pending records retain their original ownership.
- Stop/start and process restarts continue an open monitoring session. Explicit completion, changing sites, or confirmed meter replacement starts a new session/source; the same site retains its grid measurement point.

### Credentials and manual registration

- During initial Logger setup, generate an independent random Bearer Token, persist it in the protected local data directory, and allow an authenticated operator to copy it. Restarts and ordinary configuration edits do not regenerate it.
- Display a copyable registration summary containing the Site UUID, address, description, device identifier, and Token. Never include the Token in readings, CSV files, or logs.
- An administrator manually creates or selects the site, registers the device and Token, and grants that device access to the site. Store only the Token hash on the server; support disabling device credentials.
- If the UUID already exists, reuse the site's server record without silently overwriting its metadata. Address and description changes on the server are explicit administrator edits.
- A valid Token authenticates the device; it does not authorize arbitrary Site UUIDs. Reject unknown or unauthorized sites without acknowledging their readings. The Logger keeps those readings pending.
- When registering a device at a new site, retain its previous site authorization for outstanding uploads unless the administrator explicitly removes that authorization or disables its credential.
- Local setup credentials, server administrator credentials, and upload Tokens have separate scopes. Initial operator accounts come from deployment configuration; account-management features are deferred.

## 3. Runtime and collection behavior

### Data Logger

- Use Raspberry Pi OS Lite 64-bit, an ARM64 container, Python/FastAPI, `pyserial`, and SQLite in a persistent volume. A single application process owns collection, upload, and the local API. Do not run Node.js, a separate database server, or an AI runtime on the Pi.
- Serve a small HTML page with minimal JavaScript. Configure the site, server URL, P1 serial path and parameters, and installed meter profile. Provide read preview, start/stop controls, and status. Parser rules are maintained in the application, not edited on site.
- Continuously read and validate complete P1 telegrams. At each ten-second window boundary, persist at most one snapshot: the latest new valid telegram received in that window, including all sanitized parsed fields, original device time, reception time, and quality metadata.
- If a window contains no new valid telegram, leave a gap. Do not copy an old reading into a new timestamp. The DSMR 5 profile rejects CRC mismatches and unverifiable frames. Preserve safe standard fields, including supplementary meter readings; redact identifiers, free text, and unknown field values, retaining unknown codes and redaction markers.
- Store timestamps in UTC while retaining the original meter timestamp. Preserve cumulative import/export energy precision and missing values; missing phases are not zero. Do not retain raw serial bytes as the long-term record.
- Commit the snapshot and pending-upload state locally before transmission. Keep the entire monitoring session locally, including uploaded records. Manual cleanup is limited to uploaded records from completed sessions; pending records must survive cleanup.
- Show the latest valid reading time, collection errors, latest successful upload, authorization/upload errors, backlog, and storage pressure. At capacity, alert and stop adding records rather than deleting old data or silently treating collection as healthy.

Ten-second snapshots reduce the stored record count to at most 8,640 per day or 241,920 over four weeks. This does not imply a tenfold increase in SD-card lifetime. Avoid per-telegram logging and per-record upload acknowledgements, preserve database durability, and measure actual disk writes on the target devices. Later energy statistics should use cumulative meter readings; instantaneous snapshots cannot establish every short-lived power peak.

### Centralized Server and delivery contract

- Use Docker Compose, Python/FastAPI and PostgreSQL. Publish one configurable application HTTP port; the owner's existing host/upstream Nginx owns HTTPS termination, certificates, domain routing and reverse proxying. The project delivers and configures no bundled reverse proxy. Initially serve simple HTML forms, lists, and tables from the backend; React is a later interface option.
- Provide administrator login, site and device registration, authorization management, status, filtered reading queries, and CSV. Support locating a site by UUID or address and filtering its readings by session, device, and time.
- Store all accepted snapshots, including common electrical metrics in queryable fields and remaining sanitized fields in JSONB. Preserve at least one year of accepted snapshots; local cleanup must not delete server history.
- Upload to the configured server URL. Public access uses HTTPS; HTTP inside an owner-managed WireGuard tunnel is supported. Collection and onboarding do not depend on WireGuard or on immediate server connectivity.
- Normally attempt upload every ten seconds. Use bounded batches and one request in flight, with timeouts and retry backoff. Drain pending batches continuously when connectivity returns, without waiting ten seconds between successful backlog batches or blocking collection.
- Authenticate and authorize the entire batch, and commit it atomically before confirming success. Deduplicate by device and stable record ID. An invalid batch is not partially acknowledged.
- Mark records uploaded only after the server's persistence confirmation. A timeout or lost response leaves them pending; retry the same IDs. Keep upload-state updates grouped by confirmed batch.
- Track the last authenticated device contact separately from the latest reading timestamp. A successful contact during backlog recovery does not imply that fresh measurements are arriving.

The [M1 contracts](milestone/m1.md) fix the ingest endpoint, payload schema, limits, migration boundaries, and serial lifecycle before dependent implementation. M1 executable types, diagnostic probe and offline checks are implemented and independently reviewed; C5 storage is implemented in the M2 SQLite and M3 PostgreSQL repositories. The behaviors and identity boundaries above are agreed.

## 4. Milestones and acceptance

Advance in order: **M1 → M2 → M3 → M4**. Finish each milestone's acceptance checks before advancing to its dependent work. Split implementation into small task cards with boundaries, acceptance criteria, and checks before starting that milestone; this roadmap does not itself activate delegated orchestration. Each milestone uses one English document and its Chinese mirror under `docs/milestone/`; the [M1 document](milestone/m1.md) contains its design, contracts and eight atomic task sections. The [M2 design](milestone/m2.md) defines the standalone Logger contracts and ten ordered atomic tasks; implementation and automated offline acceptance are complete; the owner accepted the manual walkthrough on 2026-10-11.

| Milestone | Deliverable | Acceptance | Estimate |
| --- | --- | --- | --- |
| **[M1 — Protocol and contracts](milestone/m1.md)** | Independent project skeleton, extracted P1 parser, electrical mapping and sanitized fixtures, identity/storage/upload contracts | Parser and mapping checks pass; DSMR 5 profile serial defaults and fields are established, with physical meter/cable compatibility verified in M4; contracts distinguish site, device, session, source, and record identity | 1–2 days |
| **[M2 — Standalone Logger](milestone/m2.md)** | Local configuration and read test, Site UUID and Token, ten-second snapshots, SQLite, collection lifecycle, status; Logger Dockerfile/Compose and local browser walkthrough | Local Compose build/start and browser walkthrough pass; offline setup and collection work; identity, configuration, readings, and pending state survive restart; disconnects recover; moving sites preserves old record ownership | 2–3 days |
| **[M3 — Manually managed server (accepted 2026-10-14)](milestone/m3.md)** | Server Dockerfile/Compose with PostgreSQL, one application HTTP port, external-origin configuration and local browser walkthrough, login, site/device registration, authorized batch ingest, status, filtered queries and CSV | Local Compose build/start and browser walkthrough pass; unknown credentials/sites are rejected; registration permits ingest; replay does not duplicate readings; data can be found by Site UUID; CSV matches queries | 2–3 days |
| **[M4 — Integrated pilot release (software accepted; Pi acceptance pending)](milestone/m4.md)** | LOGGER-001 repair, Logger uploader and backlog recovery, integrated Compose, deployment/upgrade instructions, end-to-end status and hardware/resource verification; ten ordered tasks | Required software checks and isolated walkthrough must pass; network loss, restart and lost responses recover; site ownership remains separate; documented Pi allocation covers measured four-week capacity; owner ARM64 Pi 4 continuous 72-hour run passes | Owner walkthrough and 72-hour run |

The [M3 milestone](milestone/m3.md) was accepted on 2026-10-14. The original ten tasks and ordered M3-11→M3-12→M3-13 deployment revision passed implementation, automated checks and independent task reviews. The owner completed the revised one-port walkthrough and confirmed cleanup; manual omissions and automated coverage are documented in its completion record. The [M4 design and acceptance record](milestone/m4.md) fixes contracts and M4-01–M4-10, beginning with [LOGGER-001](known-issues.md). Accepted source enables upload when an origin is first configured, preserves existing offline backlog, provides manual Retry after registration/authorization repair, and forbids replacing/clearing a non-null origin while any pending records remain. M4-01–M4-10 software tasks and independent reviews are complete; the complete CI and isolated two-page browser walkthrough passed; the owner accepted the guided browser run within its recorded manual scope on 2026-10-16 and verified Server deployment/backup on 2026-10-18. The measured representative requirement is 1,374,522,219 bytes including reserve; owner allocation/fit, native Pi ARM64 and physical 72-hour acceptance remain pending. The published alpha.2 Logger includes the uploader; alpha.1 is unchanged and does not include it.

### Container delivery and browser acceptance

From M2 onward, each milestone that adds or changes a runnable module must deliver its container build and Docker Compose configuration in the same milestone. A module is not complete with source code alone. Later chart/report workers, Modbus support and operational modules follow the same rule: extend the relevant image/Compose service, or add a separate service when needed, together with its configuration and verification. M2 Logger and M3 Server container files and automated checks are implemented; future modules must follow the same delivery rule.

| Stage | Required container deliverables | Local acceptance entry point |
| --- | --- | --- |
| M2 — Data Logger | Logger Dockerfile, a standalone Logger deployment Compose file, and root `docker-compose.dev.yml` supporting the Logger; configuration examples, persistent SQLite/credential volume and documented explicit serial-device mapping for hardware deployment. Support local development builds and the target Linux ARM64 image. | Build and start locally with Compose, then open the documented Logger URL. Development defaults to an explicitly labeled synthetic P1 source with no host serial devices. Walk through site configuration, preview, start/stop, status and persistence across container recreation. A central server is not required. |
| M3 — Centralized Server | Server Dockerfile and standalone deployment Compose containing only the application/PostgreSQL, migration/startup handling, persistent storage and one configurable application HTTP port; extend `docker-compose.dev.yml` with the server and an isolated development PostgreSQL service. Keep Logger and server independently deployable on their respective hosts. | Build and start the server locally; open the documented administrator URL and walk through login, site/device registration, authorization, seeded synthetic readings, filtering and CSV. Development startup requires no public domain, production credentials, bundled proxy or M4 uploader. Upstream Nginx/certificates are configured by the owner; the project does not modify production proxy settings. |
| M4 — Integrated pilot | Update both deployment configurations for the uploader and delivery settings; provide a combined development Compose workflow using the real Logger/server services and synthetic P1 input. Validate the application deployment instructions and target hardware configurations; upstream proxy configuration remains owner-managed. | Through both pages, complete registration, collection, upload, querying and CSV; reproduce network loss, restart and backlog recovery in isolated containers. Physical Pi/meter acceptance remains a separate required check. |

Each delivery includes exact Compose build/start/stop commands, configuration examples without real secrets, browser URLs, required development credentials/setup steps, expected page behavior, health/readiness checks, logs and resource cleanup instructions. Development services bind locally by default and use a dedicated Compose project, network and disposable volumes/data; they never reuse production configuration, external production volumes or host device mappings. Cleanup removes only resources created for that walkthrough. Deployment configurations preserve application data and credentials across container recreation; destructive volume cleanup is confined to the disposable development environment.

M1 acceptance uses automated protocol/contract checks and delegated command-line verification; it has no UI and does not require the owner to repeat developer commands. Owner-facing product walkthroughs begin in M2 and focus on browser actions after Compose startup. Local container delivery begins with each application milestone, while M4 verifies the complete deployment and hardware integration.

Relevant checks by milestone:

- **M1:** arbitrary serial chunking, CRC failure, malformed input and bounded buffering, redaction, UTC conversion, units, cumulative precision, and single-/three-phase missing fields. Reuse the [handover](home-automation-energy-handover.md) selectively; the WarmteLink defaults and existing fixtures do not establish electricity-meter compatibility.
- **M2:** ten-second selection, empty windows, durable restart, serial reconnect and resource release, credential persistence, existing-site reuse, session switching, cleanup preserving pending data, and explicit storage-full behavior. M2 performs no network upload: new records remain pending. Test local confirmation/cleanup primitives with synthetic batches and confirmations; the real uploader belongs to M4.
- **M3:** administrator access, Token hash verification and disabling, site authorization, existing UUID metadata handling, atomic batch persistence, retry deduplication, and query/CSV consistency.
- **M4:** LOGGER-001 preview/resource recovery, registration after offline collection, prolonged network loss and catch-up while new snapshots arrive, restart during upload, response loss after server commit, credential rejection/manual Retry, blocked-group isolation, pending-origin protection, preserved upgrade data, old-session uploads after moving sites, and matching local/server record IDs.

Run development checks against isolated services, temporary databases, and synthetic or sanitized data. Agents do not access or change production services/devices/data. The owner performs physical validation on the existing ARM64 Pi 4, without competing with another serial reader. Record actual RAM, CPU, disk writes and sustained collection over a continuous 72-hour interval; perform fault drills separately. Use synthetic volume testing to establish at least four weeks of offline storage capacity and verify the owner's storage allocation against measured bytes. Pi 3B+ testing is deferred; unavailable required Pi 4 checks remain pending rather than passed.

M4 closes only when its required checks pass. Deliver exact startup, configuration, registration, status interpretation, troubleshooting, and cleanup instructions. Continued multiweek field observation follows MVP delivery and is recorded separately.

## 5. Follow-up roadmap and documentation

1. **Charts and deterministic analysis:** power trends, daily energy, import/export totals, coverage and missing periods. Calculate energy from cumulative readings within valid meter/session boundaries and distinguish sampled peaks from actual maxima.
2. **AI report generation and review:** manually select a period, compute a sanitized statistics snapshot, generate narrative analysis in a separate worker, preserve the snapshot/model/prompt versions, review, and export PDF. Model failure must not affect collection or access to data.
3. **Modbus collection:** add independently tested profiles and transports. Historical DDSU666 evidence is a starting point; DTSU666 needs its exact manual and device verification. Serial RTU and RTU over TCP are separate extensions.
4. **Provisioning and operations:** automatic registration, remote configuration, upgrades, and richer React interfaces as operational needs become clear. Network administration remains outside the application unless separately agreed.

Default MVP page language is English. Tracked project documents have equivalent English and Chinese versions, with English authoritative. Keep the paired architecture, roadmap, future task cards, and operation instructions aligned. Record future completion evidence in the relevant task/progress documents rather than treating planned work here as implemented.

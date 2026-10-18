# Watt Profile: Post-MVP Roadmap

> **English — source of truth** · [中文](post-mvp-roadmap_zh.md)

Status, 2026-10-18: **The MVP is accepted and complete; Post-MVP implementation has not started.** This high-level direction is agreed. Detailed M5 design and task cards are the next planning step.

This is the ongoing roadmap for development after the MVP. It records product direction, stage goals, priorities, dependencies, and changes to scope. Detailed designs, interface contracts, atomic tasks, and acceptance records remain in each milestone's English document and Chinese mirror under `docs/milestone/`.

See the [MVP roadmap](mvp-roadmap.md) for the completed M1–M4 scope and closure, and the [high-level architecture](architecture-and-roadmap.md) for system boundaries and cross-stage technical principles.

## 1. Starting point and goals

The MVP established a reliable data path:

**On-site setup → P1 collection → local persistence → offline backlog recovery and upload → Server storage, queries, and CSV export.**

The Server currently offers basic administration and raw reading access. Charts, derived statistics, analysis reports, and design assistance are not implemented. The [M4 closure record](milestone/m4.md#final-owner-acceptance-and-mvp-closure-2026-10-18) distinguishes owner acceptance from measurements that were not independently recorded; this roadmap does not reopen that acceptance or claim those measurements passed.

Post-MVP work should let internal operators and designers:

1. Assess whether a monitoring period has sufficient, complete data for analysis.
2. Understand several weeks of grid import, export, and daily patterns at a site.
3. Turn verifiable analysis into reports.
4. Gradually assess battery and solar PV scenarios after the necessary site inputs are defined.

The main sequence is **M5 analysis workspace → M6 analysis reports**. Device and operational extensions are scheduled by actual need; they do not all have to wait for reporting to finish.

## 2. M5: Server analysis workspace

**Goal: replace the basic data-management pages with an internal workspace suitable for routine analysis.**

### Capabilities

- **Workspace and navigation:** organize sites, analysis, devices, and registration; provide site search, data summaries, and separate latest-contact and latest-reading times.
- **Core analysis:** import/export power curves, daily import/export energy, selected-period statistics, sampled peaks, and typical daily profiles.
- **Data quality:** show sampling coverage, gaps, and missing key fields, with access to the relevant raw readings.
- **Phase diagnostics:** inspect L1/L2/L3 voltage, current, and import/export power, handling single-phase devices and absent fields correctly.
- **Shared filters:** analyze by site, monitoring session, and time range, with date selection, chart zoom, and value tooltips.
- **Current observations:** supplement historical analysis with latest readings and pausable automatic refresh; show data timestamps and refresh failures explicitly.
- **Administration migration:** integrate existing registration, site editing, device credentials, grants, raw readings, and complete CSV export.
- **Interface:** support English/Chinese switching and light/dark themes. Prioritize desktop analysis while retaining basic usability on narrower screens.

### Technical direction

Use **React, TypeScript, and Vite**. Ship the built frontend in the Server image and serve it from the existing FastAPI application on the same origin. Retain PostgreSQL and the existing deployment model.

Compute statistics and aggregates on the Server. Return data at an appropriate display resolution for the requested range rather than requiring the browser to download every reading for calculation. Keep raw readings available for verification.

M5 retains the existing Logger upload contract.

### Completion criteria

An internal user can open a site, inspect several weeks of data, identify major import/export patterns and gaps, examine phase metrics, and verify results against raw readings. Existing administration and export workflows remain usable.

Verify statistical correctness, query and chart responsiveness with four weeks of synthetic data, both interface languages, container delivery, and regression of the existing upload workflow. Exact checks and acceptance thresholds belong in the M5 design before implementation.

**Excluded from M5:** AI reports, PDF export, battery/PV sizing recommendations, customer accounts, and remote device management.

## 3. M6: Analysis reports

**Goal: turn workspace results into traceable reports that internal users can review and export.**

Deliver in two increments:

1. **Deterministic reports:** select a site, session, and period; produce a report with monitoring context, data quality, key statistics, and charts.
2. **AI interpretation and review:** describe usage patterns and items to check from deterministic statistics, then support internal review and PDF export.

Preserve the statistics used at generation time, the analysis version, and applicable model/prompt versions. Later backlog uploads must not silently alter existing reports.

A separate worker calls OpenRouter using statistics without site-identifying information. Verify model and API capabilities when designing this milestone. Generation failures or provider outages must not affect collection, ingest, queries, or access to existing reports.

Reports first explain observations. Specific equipment selection, capacity, and financial recommendations require additional inputs and calculation methods to be defined in later design work.

## 4. Extensions driven by actual need

These directions have no fixed milestone numbers or delivery dates yet.

| Direction | Intended capability | Trigger |
| --- | --- | --- |
| **Devices and collection** | Modbus TCP, additional P1 profiles, and later separate serial RTU / RTU-over-TCP support | A concrete site need, exact model documentation, and suitable validation conditions |
| **Battery/PV design assistance** | Compare scenarios, capacity and operating strategies, adding financial assessment when needed | Defined site inputs, generation data, equipment constraints, and calculation assumptions |
| **Operations and deployment** | Recovery drills, better diagnostics, automatic registration, remote configuration and upgrades | Demonstrated maintenance cost or deployment volume |
| **Capacity and performance** | Query optimization, pre-aggregation, storage organization, and deployment scaling | Measured data volume, latency, or resource consumption warrants it |

Standard Modbus TCP is the first Modbus transport direction. The historical DDSU666 implementation is a reference; DTSU666 and other devices need exact model documentation and physical validation.

Host networking, Wi-Fi, DHCP, VPN, certificates, and reverse-proxy management retain their existing boundaries unless a later decision explicitly includes them.

## 5. Analysis and delivery principles

- **Verifiable statistics:** calculate energy from cumulative readings within valid source, session, and meter boundaries. Identify portions that cannot be calculated or attributed reliably.
- **Visible limits:** do not replace missing values with zero or hide gaps in charts. A ten-second sampled peak is not the true instantaneous maximum.
- **Explicit measurement meaning:** P1 measures grid import and export. With solar PV present, these do not directly establish total household consumption, total PV generation, or self-consumption ratio.
- **Consistent time:** keep storage and API timestamps in UTC. Default the first daily analysis to `Europe/Amsterdam` and handle daylight-saving transitions correctly.
- **Bounded status claims:** distinguish device contact from reading freshness. Without lifecycle events, do not infer session completion or accurate uptime from silence.
- **Verified delivery:** each milestone includes the relevant image, configuration, upgrade instructions, and isolated acceptance checks. Use synthetic or sanitized data; plans are not implementation evidence.

## 6. Roadmap and milestone maintenance

- The **MVP roadmap** retains M1–M4 historical scope and closure information.
- **This roadmap** is the single ongoing home for Post-MVP direction, stage sequencing, and changes to high-level scope.
- The **architecture document** maintains system boundaries and cross-stage technical principles, linking to both roadmaps.
- **Milestone documents** hold stage-specific designs, contracts, atomic tasks, and acceptance records, with one English document and Chinese mirror per milestone.

Prepare the detailed M5 design and task cards next. Refine M6 and other extensions before starting them; this roadmap does not commit to delivery dates or detailed implementations. Maintain semantically equivalent English and Chinese documents, with English authoritative.

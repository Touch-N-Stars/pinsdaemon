# pinsdaemon Agent Guidelines

pinsdaemon is a lightweight, secure Python/FastAPI daemon for Raspberry Pi/Linux systems. It exposes a bearer-token-protected REST API and WebSocket log stream that let Touch-N-Stars/PINS clients perform tightly scoped system-management actions such as package upgrades, firmware installation, Samba and PHD2 service control, Wi-Fi and hotspot management, system telemetry/time operations, diagnostics archive creation, INDI/ASTAP package handling, and plugin package management.

This repository is **system-adjacent and hardware-adjacent**. Changes can affect live Raspberry Pi hosts, networking, package state, firmware, services, diagnostics data, PINS runtime behavior, and users' observing sessions. Every implementation agent must prioritize safety, least privilege, explicit validation, and stable client contracts over speed.

These rules apply to all coding, review, documentation, test, packaging, systemd, shell-script, and automation work in this repository.

This file is the project's **static agent harness**: the always-loaded rules that keep AI-assisted development disciplined. Keep it high-signal. Put task-specific logs, API examples, package versions, device details, user reports, and investigation notes in the task prompt or companion docs instead of bloating this file.

## Operating Principle: Agentic Engineering, Not Vibe Coding

Use AI agents as implementation engines inside a controlled engineering system. Do not rely on "it seems to work." Production work requires clear intent, repository inspection, scoped changes, deterministic validation, and human-reviewable evidence.

For pinsdaemon, agentic engineering means:

- **Specification before generation:** define the endpoint/script/service behavior, non-goals, safety level, acceptance criteria, and rollback expectations before editing.
- **Context before code:** inspect the actual app, shell scripts, sudoers, systemd unit, packaging hooks, and README contract before proposing changes.
- **Security before convenience:** preserve bearer authentication, least-privilege sudoers, parameterized commands, and no-shell-injection behavior.
- **Safety before automation:** never let tests or generated code mutate a real host, Wi-Fi, firmware, packages, services, clock, or PINS runtime unless the task explicitly says a safe lab device is available.
- **Verification before confidence:** run relevant Python, shell, packaging, and API contract validation, or state exactly why validation was not run.
- **Review before final:** inspect the final diff against client contracts, Raspberry Pi deployment impact, systemd behavior, sudoers scope, script safety, and recovery paths.
- **Human judgment stays in charge:** architecture, auth, privileged operations, package/firmware install behavior, live-device validation, releases, and rollback decisions must remain reviewable by a maintainer.

## Product Priorities

Implementations must respect these priorities, in this order:

1. Raspberry Pi/Linux system safety and recoverability.
2. Least-privilege security: bearer token auth, restricted service user, narrow sudoers, no root API process.
3. No shell injection: hard-coded or strictly validated commands and argument arrays.
4. Stable Touch-N-Stars/PINS API contracts and response shapes.
5. Long-running operations must remain job-owned and observable through status/logs.
6. Wi-Fi/hotspot changes must preserve reconnect/fallback behavior and avoid locking users out.
7. Firmware/package/plugin/ASTAP/INDI installation must be explicit, version-aware, and failure-reporting.
8. systemd and Debian packaging behavior must remain idempotent and service-safe.
9. Diagnostics must collect useful support data without leaking secrets or over-collecting sensitive material.
10. Development mocks and local paths are for tests/dev only; never present them as proof of Pi behavior.
11. Small, testable, additive changes over broad rewrites.
12. Documentation and README/API examples must stay current when behavior changes.

If priorities conflict, choose the earlier priority and document the trade-off.

## Project Snapshot Agents Must Preserve

pinsdaemon currently includes:

- A FastAPI daemon under `app/`, with a large `app/main.py` defining API models, route handlers, startup tasks, package metadata fetchers, Wi-Fi helpers, diagnostics orchestration, and job-facing responses.
- App support modules such as `app/auth.py`, `app/job_manager.py`, `app/wifi_config.py`, and `app/hotspot_config.py`.
- Shell and Python helper scripts under `scripts/` for Wi-Fi recovery, diagnostics collection, required package installation, hotspot management, firmware installation, INDI package installation, ASTAP star database installation, plugin management, Samba management, system upgrade, Wi-Fi auto-management, Wi-Fi connection, and Wi-Fi scanning.
- Debian packaging under `packaging/`, including `DEBIAN` maintainer scripts, sudoers rules, and udev-related rules.
- A systemd unit under `systemd/sysupdate-api.service`.
- A restricted service architecture around the `sysupdate-api` user and `sudo -n` delegated commands.
- Bearer token authentication for HTTP endpoints and token-bearing WebSocket access for job logs.
- A job manager pattern where long-running commands return a job ID immediately and can be polled or streamed.
- API clients such as Touch-N-Stars/PINS that rely on stable endpoint paths, payload fields, status semantics, and background job behavior.

Do not describe local development mocks, unvalidated subprocess stubs, or manually simulated output as proof of real Raspberry Pi, NetworkManager, systemd, sudoers, firmware, package, PHD2, Samba, INDI, ASTAP, or PINS behavior.

## Static vs Dynamic Context

Use this file for stable, always-applicable rules. Load dynamic context only when relevant:

- **Repository files:** inspect actual implementation before editing.
- **README/API contract:** check the README endpoint list, data models, installation notes, and behavior descriptions.
- **App code:** inspect `app/main.py`, auth, job manager, config helpers, and route-specific models before changing API behavior.
- **Scripts:** inspect target scripts before changing subprocess invocations, sudoers, arguments, outputs, or error handling.
- **System integration:** inspect `systemd/`, `packaging/`, maintainer scripts, sudoers, udev rules, and service names before changing deployment behavior.
- **Client context:** include Touch-N-Stars/PINS expected endpoints, payload examples, and UI behavior when client compatibility matters.
- **Dependency docs:** use Context7 MCP for third-party packages and APIs.
- **Validation output:** treat tests, shell checks, API probes, packaging checks, and service dry-runs as feedback, not decoration.

Avoid dumping entire unrelated files into the prompt. Prefer precise file paths, relevant excerpts, endpoint contracts, and task-specific assumptions.

## Documentation Freshness And Context7 MCP

When working with third-party libraries, frameworks, APIs, SDKs, package configuration, or dependency-specific implementation details, use the Context7 MCP server before proposing or changing code.

Required rules:

- Prefer current Context7 documentation over built-in model knowledge for external dependencies.
- Use Context7 before implementing or reviewing changes involving FastAPI, Starlette, Pydantic, Uvicorn, pytest, pytest-asyncio, Python packaging, systemd unit semantics, Debian packaging, shell tooling, NetworkManager/nmcli, WebSockets, or other external APIs.
- Use current official docs or local manpages/reference output for system tools when behavior matters, especially `systemctl`, `timedatectl`, `nmcli`, `iw`, `iwlist`, `dpkg`, `dpkg-query`, `apt`, `journalctl`, and `sudoers`.
- Do not rely on outdated examples when library behavior, config syntax, CLI flags, or APIs may have changed.
- If Context7 or external docs are unavailable, say so explicitly and continue only with repository inspection and clearly stated assumptions.
- Mention relevant documentation sources, package versions, command versions, or assumptions in the plan, review notes, or final response when they affected the implementation.
- Context7 supplements repository inspection; it does not replace reading the actual project files before editing.

## Agent Operating Modes

Choose the lowest-autonomy mode that fits the risk.

### Conductor Mode

Use for ambiguous, risky, architectural, security-sensitive, system-mutating, network-mutating, package/firmware, sudoers, systemd, packaging, auth/token, API-contract, or live-device work. The agent should make small changes, surface decisions early, and preserve human control.

### Orchestrator Mode

Use for well-specified, bounded tasks with clear tests and existing patterns. The agent may handle multi-file changes, but must still plan, inspect, validate, and review.

### Background/Delegated Mode

Use only for narrow, reproducible tasks that are easy to validate, such as updating docs, adding API-model tests for existing routes, improving diagnostics labels, or applying repeated error-message patterns. The task prompt must include explicit acceptance criteria and validation commands.

Never use high-autonomy execution for work that lacks a clear rollback path or touches Wi-Fi, firmware, package installation, service restarts, sudoers, system time, diagnostics privacy, auth, or live Raspberry Pi behavior.

## Architecture Boundaries

Keep implementation layers separated:

- **API layer:** FastAPI app, request/response models, route handlers, dependency injection, auth dependencies, WebSocket handlers.
- **Job orchestration:** job creation, subprocess lifecycle, status tracking, stdout/stderr log capture, persistence of selected job state, WebSocket streaming.
- **Privileged command facade:** safe construction of `sudo -n` commands and explicit script/tool invocation.
- **System scripts:** shell/Python scripts under `scripts/` that perform privileged work or system introspection.
- **Wi-Fi and hotspot management:** config files, interface validation, NetworkManager/nmcli/iw interactions, auto-connect, fallback hotspot, dispatcher behavior.
- **Package and firmware management:** system upgrades, firmware archive parsing/install, PINS package update checks, plugin package management, INDI/ASTAP installers.
- **Service management:** Samba, PHD2, pins, sysupdate-api, gvfs-gphoto2-volume-monitor, and systemd integration.
- **Diagnostics:** option model, archive job lifecycle, support bundle contents, redaction/privacy behavior, retention/cleanup.
- **Deployment/packaging:** Debian package metadata/hooks, sudoers, systemd unit, udev rules, file ownership/permissions, service user behavior.
- **Client compatibility:** Touch-N-Stars/PINS expectations for endpoints, job responses, error details, aliases, and WebSocket behavior.
- **Documentation:** README endpoint docs, installation notes, release notes, and operational warnings.

Do not mix unrelated layers unless the task explicitly requires it. If a feature spans layers, keep each layer's contract clear and test the boundary.

## Required Workflow

Use this role sequence for implementation tasks:

1. Planner
2. Engineer
3. Developer
4. Reviewer

For small fixes, the roles can be summarized briefly, but the thinking still has to happen.

### Planner

Planner responsibilities:

- Read the request and relevant project docs.
- Run `git status --short` before edits.
- Identify existing uncommitted changes as user/team work.
- Inspect the current implementation before proposing changes.
- Define affected endpoints, models, scripts, packages, services, files, and client contracts.
- Define non-goals and out-of-scope host/device mutations.
- Define acceptance criteria and validation commands.
- Identify security, Raspberry Pi, sudoers, systemd, packaging, Wi-Fi, firmware/package, API, diagnostics, and Touch-N-Stars/PINS compatibility risks.

Planner output should include:

- Task summary.
- Operating mode and safety level.
- Affected areas.
- Implementation steps.
- Acceptance criteria.
- Test/validation plan.
- Risks, assumptions, and required docs/context.

### Engineer

Engineer responsibilities:

- Convert the plan into a concrete technical design.
- Prefer existing interfaces, models, status values, command patterns, and local conventions.
- Keep public API/client compatibility in mind.
- Avoid rewrites unless the task requires them.
- Define endpoint, model, script, command, job, service, or packaging contracts clearly.
- Note backwards compatibility, aliases, migration/upgrade behavior, and rollback behavior.

Engineer output should include:

- Technical design.
- Files/modules/scripts to modify.
- Interfaces/contracts to add or change.
- Edge cases and failure modes.
- Compatibility and deployment notes.

### Developer

Developer responsibilities:

- Implement only the agreed scope.
- Use targeted patches.
- Preserve existing behavior unless the task explicitly changes it.
- Do not delete, revert, overwrite, or mass-format unrelated work.
- Add or update tests when behavior changes.
- Add comments only for non-obvious logic or safety-critical behavior.
- Re-read files immediately before editing if other changes may exist.
- Use subprocess argument arrays in Python. Never concatenate untrusted values into shell strings.
- Validate user-controlled values before passing them to scripts or system tools.
- Keep shell scripts strict, quoted, and shellcheck-friendly.

Developer output should include:

- Implementation summary.
- Changed files.
- Tests/docs added or updated.
- Deviations from plan, if any.

### Reviewer

Reviewer responsibilities:

- Review the final diff before finishing.
- Check acceptance criteria.
- Check Raspberry Pi deployment impact.
- Check API compatibility and stable response shape.
- Check Touch-N-Stars/PINS client impact.
- Check auth, token handling, and secret exposure.
- Check sudoers/service-user scope and no-shell-injection behavior.
- Check systemd and Debian package safety if deployment files changed.
- Check Wi-Fi/hotspot recovery if networking changed.
- Check firmware/package/plugin install safety if installers changed.
- Check diagnostics privacy and retention if support bundles changed.
- Check that unrelated user/team changes were preserved.
- Run relevant validation or state exactly why it was not run.

Reviewer output should include:

- Result: pass, pass with notes, or needs changes.
- Issues found.
- Required fixes, if any.
- Validation results.
- Remaining risks.

## Repository Safety

Before making changes:

```bash
git status --short
```

Treat any existing uncommitted changes as user/team work. Do not revert or overwrite them.

Never run destructive Git commands unless the user explicitly requests them:

- `git reset --hard`
- `git checkout -- .`
- `git clean -fd`
- force-push
- rebase or amend without explicit instruction

Prefer additive, targeted fixes over broad rewrites.

Before finishing:

```bash
git diff --stat
git diff
```

Review the diff and confirm it contains only task-related changes.

## Security And Privilege Rules

Required rules:

- Preserve bearer token authentication for all protected HTTP routes.
- Preserve token validation for WebSocket log routes.
- Never log bearer tokens, Wi-Fi passwords, hotspot passwords, signing keys, package repository credentials, API keys, or other secrets.
- Do not hardcode local tokens, Wi-Fi credentials, hostnames, private URLs, or developer machine paths.
- Keep the API process non-root. Privileged actions must go through explicit, narrow `sudo -n` commands or approved system interfaces.
- Keep sudoers rules narrow: explicit commands, explicit paths, no broad shell access, no wildcard expansion unless justified and safe.
- Do not add `shell=True` in Python subprocess code for untrusted or parameterized operations.
- Validate all user-controlled arguments before passing them to scripts or system tools.
- Prefer allowlists over blocklists for package names, plugin names, database IDs, service names, band values, channel values, interface names, file extensions, and archive naming.
- Use safe temporary directories/files and clean them up.
- Ensure uploaded archives are validated by name, extension, expected contents, size where practical, and extraction behavior.
- Avoid exposing full command lines when they may include sensitive user values.

## API Contract Rules

API work must:

- Preserve endpoint paths, aliases, response fields, and status semantics unless a breaking change is explicitly approved.
- Preserve `JobResponse` shape for job-starting endpoints.
- Keep long-running actions asynchronous and job-owned.
- Return useful, bounded error details without leaking secrets or raw host internals unnecessarily.
- Keep Pydantic models strict where appropriate, especially for system-mutating routes.
- Keep backward-compatible aliases documented when supported.
- Update README endpoint examples and data models when contracts change.
- Add tests for auth boundaries, validation errors, response shape, and important route behavior.

For client compatibility, assume Touch-N-Stars/PINS may rely on exact fields such as `jobId`, `status`, `exitCode`, `startedAt`, `finishedAt`, `command`, `enabled`, `running`, `connected`, `ssid`, `band`, `configured`, `archiveId`, `pollUrl`, and `downloadUrl`.

## Job And WebSocket Rules

Job-related work must:

- Keep long-running commands off the request path.
- Return a job ID quickly.
- Stream stdout/stderr safely without leaking secrets.
- Preserve status transitions: `started`, `running`, `success`, `failed` unless explicitly changed.
- Capture exit codes and finish times reliably.
- Handle client disconnects without killing unrelated jobs accidentally.
- Avoid unbounded memory growth from logs.
- Keep persisted state files atomic where practical.
- Keep job IDs opaque and unguessable.

## Wi-Fi And Hotspot Rules

Wi-Fi work is high risk because mistakes can lock users out of headless Raspberry Pi devices.

Required rules:

- Use Conductor Mode unless the task is purely documentation or read-only tests.
- Preserve fallback-to-hotspot behavior when client connection fails.
- Preserve configured client/hotspot interface behavior and validate interface names.
- Do not assume every Pi has the same WLAN interface names, bands, drivers, or channel support.
- Do not hardcode SSIDs, passwords, BSSIDs, country codes, or channels unless explicitly requested for a test fixture.
- Never log Wi-Fi or hotspot passwords.
- Treat NetworkManager dispatcher scripts as production-critical.
- Avoid changes that can leave both client and hotspot modes down without a clear rollback/recovery path.
- Prefer dry-run or mocked command tests unless a safe lab Pi is explicitly provided.

## Firmware, Package, Plugin, INDI, And ASTAP Rules

Installation/update work is high risk because it mutates the host.

Required rules:

- Use allowlists for installable packages, plugin packages, ASTAP database IDs, and supported asset names.
- Preserve protected plugin packages and do not allow removal/installation of protected items unless explicitly approved.
- Validate firmware filename format and version ordering.
- Validate archive extraction paths to avoid zip-slip/path traversal.
- Do not install arbitrary `.deb` files from untrusted input.
- Keep GitHub/repository metadata fetches timeout-bounded and failure-reporting.
- Preserve `onlyNotInstalled` and filtering semantics when present.
- Keep installer scripts idempotent where practical.
- Report partial failures clearly and preserve logs for support.
- Do not run package installs in tests unless explicitly using a controlled container or safe lab environment.

## Systemd, Debian Packaging, And Service Rules

Deployment work must:

- Preserve the `sysupdate-api` service behavior unless the task explicitly changes it.
- Preserve restricted service-user operation.
- Preserve service restart/start/stop behavior during package install/upgrade.
- Keep maintainer scripts idempotent and noninteractive.
- Keep file ownership, permissions, sudoers, and executable bits explicit.
- Keep package hooks safe if `pins`, `phd2`, Samba, NetworkManager, gvfs, or other services are touched.
- Validate systemd unit syntax and shell scripts where possible.
- Do not delete user data, firmware state, Wi-Fi config, diagnostics artifacts, PINS configs, plugin registries, or logs without explicit confirmation.

## Diagnostics Rules

Diagnostics work must:

- Default to useful support bundles while minimizing sensitive data.
- Avoid collecting secrets, tokens, Wi-Fi passwords, private keys, signing material, or full unrelated home directories.
- Keep section keys stable for UI checkboxes.
- Keep retention/cleanup behavior bounded.
- Return `202 Accepted` for queued archive creation where that is the established contract.
- Preserve download behavior and archive naming unless explicitly changed.
- Add redaction when adding new log/config sources that may contain secrets.

## Shell Script Rules

Shell changes must:

- Prefer `set -euo pipefail` where compatible.
- Quote variables unless word splitting is intentional and documented.
- Avoid `eval` and unsafe command construction.
- Validate inputs before using them.
- Use explicit paths for privileged operations when practical.
- Keep stdout/stderr actionable because it may be streamed to clients.
- Keep error messages clear and non-secret.
- Remain shellcheck-friendly.

## Python Rules

Python changes must:

- Prefer typed helper functions and small route handlers where practical.
- Keep Pydantic models close to route contracts.
- Use `asyncio.create_subprocess_exec` or `subprocess.run([...])` with argument arrays.
- Avoid blocking the event loop; use `asyncio.to_thread` for blocking I/O when needed.
- Set reasonable timeouts for external network calls and host commands.
- Validate external data from package indexes, GitHub releases, command output, JSON files, and uploads.
- Use atomic writes for state files where practical.
- Keep logging useful but non-secret.

## Testing And Validation Rules

Choose validation that matches the affected layer. Never run destructive or host-mutating commands on a real system unless the task explicitly provides a safe lab context.

Suggested validation:

```bash
git status --short
python -m compileall app
python -m pytest
bash -n scripts/*.sh
shellcheck scripts/*.sh packaging/DEBIAN/* systemd/*
systemd-analyze verify systemd/sysupdate-api.service
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

For API contract changes, add targeted tests using FastAPI's test client or an equivalent test harness.

For shell/script changes, also run script-specific dry-runs or mocked command tests where supported.

For packaging changes, validate maintainer scripts with syntax checks and inspect installed file paths/permissions in a disposable environment.

For Wi-Fi, firmware, package, service, or time-setting changes, prefer mocked tests and code review unless a safe lab Pi is explicitly available.

## Review Checklist

Every reviewer must specifically check:

- Did this preserve Raspberry Pi/Linux deployment behavior?
- Did this preserve bearer auth and WebSocket token behavior?
- Did this preserve least-privilege sudoers and non-root API operation?
- Did this avoid shell injection and unsafe command construction?
- Did this preserve API paths, aliases, response fields, and status semantics?
- Did this keep long-running operations job-owned?
- Did this preserve Touch-N-Stars/PINS compatibility?
- Did this avoid leaking secrets in logs, diagnostics, responses, or commands?
- Did this preserve Wi-Fi/hotspot recovery and avoid lockout risks?
- Did this keep firmware/package/plugin installers allowlisted and safe?
- Did this preserve systemd and Debian package idempotency?
- Did this preserve unrelated user/team changes?
- Were relevant tests/build/lint/shell/package checks run?
- Were README/docs updated when behavior changed?

## Final Response Format

Use this structure for implementation tasks:

```text
Task completed: <short summary>

Planner:
- <what was planned>

Engineer:
- <technical approach>

Developer:
- <what was implemented>

Reviewer:
- <review result>

Changed files:
- <file list>

Validation:
- <commands run and results>

Notes:
- <limitations, assumptions, unrelated local changes, safe-lab/hardware status, or follow-up work>
```

For very small fixes, this can be concise, but it should still report changed files and validation.

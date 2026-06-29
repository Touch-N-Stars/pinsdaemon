# pinsdaemon Agent Task Template

Use this template when assigning implementation work to Codex or a multi-agent coding team. It is designed to move work from casual prompting into agentic engineering: clear intent, bounded context, scoped execution, validation, and reviewable evidence.

pinsdaemon is a security-sensitive Raspberry Pi/Linux system-management daemon. Most work should be treated as system-adjacent and potentially host-mutating unless proven otherwise.

For dependency-specific work, the agent must report whether Context7 MCP was used and note any important documentation source, package version, API version, command version, or assumption that affected the implementation.

````text
# Task For GPT-5.5 Codex Multi-Agent Team

Repository:
<local path to pinsdaemon, for example C:\Users\Aco\Desktop\Dev-Tools\pinsdaemon or /home/pi/dev/pinsdaemon>

Task title:
<short imperative title>

Task:
<describe the concrete implementation task in 1-4 paragraphs>

Why this matters:
<explain the user/client outcome, operational problem, bug, release need, safety issue, or support improvement>

Operating mode:
<Conductor | Orchestrator | Background/Delegated>

Use Conductor for ambiguous, risky, architectural, security-sensitive, system-mutating, Wi-Fi/hotspot, firmware, package/plugin install, sudoers, systemd, Debian packaging, auth/token, API-contract, diagnostics privacy, or live Raspberry Pi work.
Use Orchestrator for well-specified multi-file work with clear tests and existing patterns.
Use Background/Delegated only for narrow, reproducible tasks with explicit acceptance criteria and validation.

Safety level:
<read-only | mocked/dev-only | safe lab Pi allowed | production Pi allowed with explicit confirmation>

Current project context:
- pinsdaemon is a lightweight Python/FastAPI daemon for Raspberry Pi/Linux system management.
- It exposes bearer-token-protected HTTP endpoints and a WebSocket log stream.
- It runs as a restricted service user, typically `sysupdate-api`, and delegates privileged operations through narrow `sudo -n` commands.
- Long-running operations are asynchronous jobs. Clients receive a job ID, poll `/jobs/{jobId}`, and can stream logs through `/logs/{jobId}?token=<token>`.
- The daemon manages or reports on system upgrades, firmware archives, Samba, PHD2, Wi-Fi, hotspot fallback, system temperature, system time, diagnostics archives, package update checks, INDI 3rdparty packages, ASTAP star databases, and PINS plugin packages.
- Touch-N-Stars/PINS clients may depend on exact endpoint paths, aliases, response fields, job status semantics, and error behavior.
- Local mocks and development paths are not proof of real Raspberry Pi, NetworkManager, systemd, sudoers, firmware, package, Samba, PHD2, INDI, ASTAP, or PINS behavior.

Context packet:
- User request / issue text: <paste exact request, bug report, or issue>
- Relevant docs: <README sections, API examples, install/upgrade notes, release notes, Touch-N-Stars/PINS client context>
- Relevant files: <app/main.py, app/auth.py, app/job_manager.py, app/wifi_config.py, app/hotspot_config.py, scripts/*, packaging/*, systemd/*, tests/*>
- Endpoint(s): <paths, methods, request/response examples, aliases, WebSocket behavior>
- Script(s)/command(s): <script path, expected args, stdout/stderr shape, exit codes, dry-run behavior>
- Service/package context: <sysupdate-api, pins, phd2, Samba, NetworkManager, Debian package, sudoers, maintainer hooks>
- Platform details: <Raspberry Pi OS/Debian version, Python version, architecture, NetworkManager version, systemd version, dev host>
- Logs/screenshots: <journalctl, API responses, WebSocket logs, shell output, stack traces>
- Known constraints: <do not mutate host, read-only only, no package install, no Wi-Fi changes, no firmware upload, no service restart>

Non-goals:
- <explicitly list what must not be changed>
- <list unrelated endpoints/scripts/services/packages to avoid scope creep>
- <state whether live Raspberry Pi validation is out of scope>
- <state whether Touch-N-Stars client changes are out of scope>

Project-specific rules:
- Follow `AGENT_GUIDELINES.md` or the repository's current agent guideline file.
- Preserve Raspberry Pi/Linux system safety and recoverability.
- Preserve bearer token authentication and WebSocket token handling.
- Preserve restricted service-user operation and least-privilege sudoers.
- Preserve no-shell-injection behavior; use argument arrays and validated allowlists.
- Preserve API paths, aliases, response fields, and job status semantics unless explicitly approved.
- Preserve async job behavior for long-running commands.
- Preserve Touch-N-Stars/PINS client compatibility.
- Preserve Wi-Fi reconnect/fallback hotspot behavior and avoid lockout risks.
- Preserve firmware/package/plugin/ASTAP/INDI allowlists and version-aware install behavior.
- Preserve systemd and Debian packaging idempotency.
- Preserve diagnostics privacy and do not collect or log secrets.
- Do not run or add default tests that change Wi-Fi, upload firmware, install packages, restart services, alter system time, modify sudoers, or mutate a real host.
- Do not hardcode bearer tokens, Wi-Fi credentials, hotspot passwords, signing material, repository credentials, hostnames, or local machine paths.
- Do not revert, overwrite, delete, or undo unrelated work.
- Do not use destructive Git commands.
- Prefer additive, targeted patches over rewrites.

Layer boundaries:
- API layer: FastAPI routes, request/response models, auth dependencies, WebSocket handlers.
- Job orchestration: job creation, subprocess lifecycle, status tracking, stdout/stderr logs, persisted job state.
- Privileged command facade: validated `sudo -n` command construction and script/tool invocation.
- System scripts: shell/Python helpers under `scripts/`.
- Wi-Fi/hotspot: config files, interface validation, NetworkManager/nmcli/iw interactions, auto-connect, fallback hotspot, dispatcher scripts.
- Package/firmware/plugin/INDI/ASTAP: installers, metadata fetchers, allowlists, version comparison, state files.
- Service management: Samba, PHD2, pins, sysupdate-api, gvfs-gphoto2-volume-monitor, systemd.
- Diagnostics: options, archive jobs, redaction/privacy, retention/cleanup, downloads.
- Deployment/packaging: Debian package metadata/hooks, sudoers, systemd unit, udev rules, file ownership/permissions.
- Client compatibility: Touch-N-Stars/PINS endpoint contracts, response shapes, status values, aliases, WebSocket expectations.
- Docs/release: README, API examples, installation notes, changelog/release notes.

Required workflow:
1. Planner
   - Run `git status --short` before edits.
   - Inspect relevant files before proposing changes.
   - Identify uncommitted user/team work.
   - Identify affected endpoints, models, scripts, commands, services, packages, systemd units, packaging files, docs, and clients.
   - Identify non-goals and host/device mutations that are forbidden.
   - Identify whether Context7 MCP, external docs, manpages, or command references are needed.
   - Define acceptance criteria and validation commands.
   - Identify security, sudoers, systemd, Raspberry Pi, Wi-Fi, firmware/package, diagnostics, API, WebSocket, packaging, and Touch-N-Stars/PINS compatibility risks.

2. Engineer
   - Define the technical design and contracts.
   - Reuse existing models, helpers, scripts, response shapes, and local patterns.
   - Define command arguments, allowlists, validation, timeouts, error handling, and rollback behavior.
   - Define how tests or mocks model real command/API behavior without mutating a host.
   - Note docs/README updates needed.

3. Developer
   - Implement only the scoped changes.
   - Add/update tests and docs when behavior changes.
   - Avoid broad formatting, dependency churn, generated-file churn, or unrelated cleanup.
   - Use safe subprocess argument arrays in Python.
   - Keep shell scripts quoted, strict, and shellcheck-friendly.
   - Avoid live host mutation unless the task explicitly provides a safe lab context.

4. Reviewer
   - Run `git diff --stat` and `git diff` before finishing.
   - Check acceptance criteria and layer boundaries.
   - Check auth, token handling, sudoers scope, command validation, and secret exposure.
   - Check API paths, aliases, response shape, job status semantics, and WebSocket behavior.
   - Check Wi-Fi/hotspot recovery, firmware/package safety, systemd/packaging idempotency, diagnostics privacy, and Touch-N-Stars/PINS compatibility as relevant.
   - Run validation or explain why it was not run.

Acceptance criteria:
1. <observable outcome 1>
2. <observable outcome 2>
3. <observable outcome 3>
4. <test/doc/API/safety requirement if relevant>

Suggested validation:
```bash
git status --short
python -m compileall app
python -m pytest
bash -n scripts/*.sh
shellcheck scripts/*.sh packaging/DEBIAN/* systemd/*
systemd-analyze verify systemd/sysupdate-api.service
```

API validation examples:
```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
curl -s -H "Authorization: Bearer <dev-token>" http://127.0.0.1:8000/system/temperature
curl -s -H "Authorization: Bearer <dev-token>" http://127.0.0.1:8000/updates/check
```

Only run host-mutating API calls on an explicitly approved safe lab Pi:
```bash
# Host-mutating; do not run by default.
curl -X POST -H "Authorization: Bearer <dev-token>" -H "Content-Type: application/json" \
  -d '{"dryRun": true}' http://127.0.0.1:8000/upgrade
```

Task-specific validation:
```bash
<add focused pytest tests, script dry-runs, packaging checks, mocked nmcli/systemctl tests, or API probes>
```

Expected final response:
- Planner summary
- Engineer design summary
- Developer implementation summary
- Reviewer result
- Changed files
- Commands run
- Test/build/check results
- Known limitations
- Safe-lab / live-device status
````

## Common Task Variants

### API Endpoint / Model Task

Add these constraints:

```text
- Preserve route path, method, aliases, and response shape unless explicitly approved.
- Preserve bearer auth.
- Preserve Touch-N-Stars/PINS client compatibility.
- Keep long-running operations job-owned.
- Validate request bodies strictly when the route mutates host state.
- Update README endpoint examples and data models when contracts change.
```

Suggested validation:

```bash
python -m compileall app
python -m pytest
```

### Job Manager / WebSocket Logs Task

Add these constraints:

```text
- Preserve job ID creation, status transitions, exit code capture, timestamps, and log streaming semantics.
- Do not leak secrets in command strings or streamed logs.
- Handle WebSocket disconnects gracefully.
- Avoid unbounded log memory growth.
- Keep job state thread/async safe.
```

Suggested validation:

```bash
python -m compileall app
python -m pytest
```

### Wi-Fi / Hotspot Task

Add these constraints:

```text
- Use Conductor mode.
- Preserve fallback-to-hotspot behavior.
- Validate interface names, SSID inputs, band values, channels, and password constraints.
- Never log Wi-Fi or hotspot passwords.
- Do not assume fixed interface names or channel support.
- Do not run host-mutating Wi-Fi tests unless a safe lab Pi is explicitly approved.
```

Suggested validation:

```bash
python -m compileall app
bash -n scripts/wifi-connect.sh scripts/hotspot.sh scripts/90-pins-wifi-recovery
shellcheck scripts/wifi-connect.sh scripts/hotspot.sh scripts/90-pins-wifi-recovery scripts/wifi-automanage.py scripts/wifi-scan.py
```

### Firmware / Package / Plugin / INDI / ASTAP Task

Add these constraints:

```text
- Use allowlists for installable packages/assets/database IDs.
- Validate firmware filename and archive extraction safety.
- Preserve protected plugin package behavior.
- Preserve version comparison and only-not-installed semantics.
- Do not install packages in tests unless using a controlled safe environment.
- Keep repository/GitHub metadata fetches timeout-bounded and failure-reporting.
```

Suggested validation:

```bash
python -m compileall app
bash -n scripts/install-firmware.sh scripts/install-indi-package.sh scripts/install-astap-star-database.sh scripts/manage-plugin.sh scripts/system-upgrade.sh
shellcheck scripts/install-firmware.sh scripts/install-indi-package.sh scripts/install-astap-star-database.sh scripts/manage-plugin.sh scripts/system-upgrade.sh
```

### Systemd / Debian Packaging / Sudoers Task

Add these constraints:

```text
- Preserve restricted service-user operation.
- Keep sudoers narrow and explicit.
- Keep maintainer scripts idempotent and noninteractive.
- Preserve service restart/start/stop behavior during package install/upgrade.
- Do not delete user data, logs, Wi-Fi config, firmware state, PINS configs, or diagnostics data.
```

Suggested validation:

```bash
bash -n packaging/DEBIAN/* scripts/*.sh
shellcheck packaging/DEBIAN/* scripts/*.sh
systemd-analyze verify systemd/sysupdate-api.service
```

### Diagnostics Archive Task

Add these constraints:

```text
- Preserve diagnostics option keys used by clients.
- Avoid collecting secrets, tokens, Wi-Fi passwords, private keys, signing material, or unrelated home directories.
- Preserve archive job lifecycle and download behavior.
- Keep retention/cleanup bounded.
- Add redaction when adding new sensitive sources.
```

Suggested validation:

```bash
python -m compileall app
bash -n scripts/collect-diagnostics.sh
shellcheck scripts/collect-diagnostics.sh
python -m pytest
```

### Documentation-Only Task

Add these constraints:

```text
- Keep README/API examples accurate.
- Mark host-mutating commands clearly.
- Distinguish dev mocks from verified Raspberry Pi behavior.
- Do not imply features are safer or more complete than the code supports.
```

Suggested validation:

```bash
git diff --check
```

# Handoff

## 2026-08-30 — Reliable session messaging and attention handling

Status: prepared and verified for the public repository.

### Behavior

- Session messages are persisted before delivery and consumed in order by one
  worker per session. Interrupted paste deliveries resume after backend
  restarts; failed deliveries remain recoverable and block later messages
  instead of being silently overtaken.
- Submit handling now observes a real TUI transition for Codex, Claude Code and
  OpenCode. A successful tmux command alone is not recorded as delivery.
- Interactive TUI questions and delivery failures are explicit attention
  states. They appear in the web UI and Telegram; optional Web Push supports
  installed PWAs while suppressing duplicate foreground notifications.
- The mobile session view now respects device safe areas, preserves useful
  terminal height, provides structured-answer controls, and navigates tmux
  history explicitly. Hidden browser tabs no longer keep resizing a shared
  pane.
- Optional Claude usage refresh performs a rate-limited safe-mode CLI startup
  only for an expired renewable local credential and stops before a model
  prompt is sent.

### Public-repository boundary

- VAPID private keys, Push subscription endpoints, OAuth credentials, Telegram
  credentials, inventories and instance state remain host-local.
- New public material contains no live session identifiers, account identities,
  hostnames, private project names or deployment-specific backup paths.
  Examples use role variables and synthetic test values.

### Verification

- Python and JavaScript syntax checks and all 52 unit tests pass.
- Ansible lint, playbook syntax, SSH/firewall regressions and Agent Hub's
  least-privilege default checks pass.
- Tests cover queue recovery and ordering, honest multi-harness submission,
  interactive-question recognition, recurrent attention events, Web Push UI
  contracts, mobile terminal behavior and safe-area layout.
- The repository working tree passes the configured Gitleaks scan.

## 2026-08-25 — Public screenshots and Ansible tooling patched

Status: prepared and verified for the public repository.

### Behavior

- Added two sanitized README screenshots for the complete new-session form and
  compact host-health overview. The capture omits identities, usage balances,
  session data, host paths, and private repository names.
- Updated the development-only `ansible-core` pin from 2.17.14 to 2.18.19,
  the stable patched release for the high-severity `ansible-galaxy` argument
  injection advisory reported by Dependabot. Agent Hub runtime dependencies
  and the live service are unchanged.

### Verification

- A clean Python 3.13 virtual environment installed the exact development
  requirements. Ansible lint, playbook syntax, SSH/firewall safety tests, and
  Agent Hub least-privilege default tests all pass with ansible-core 2.18.19.
- The full application source check passes with 21 tests. Screenshot source
  DOM assertions and visual review found none of the excluded private fields.
- The pinned CI Gitleaks scan covers the complete Git history before publish.

## 2026-08-25 — New-session advanced options clarified

Status: implemented, deployed, and verified on the local Agent Hub instance.

### Behavior

- `Modalità permessi` now lives in the collapsed bottom panel instead of the
  main new-session form, and that panel is named `Opzioni avanzate`.
- Terminal sizing fields now read `Larghezza terminale (colonne)` and
  `Altezza terminale (righe)`. Their help explains that columns are characters
  per line, rows are visible lines, and both affect TUI layout only.
- Added a static UI contract that keeps the permission selector inside the
  advanced panel and preserves the clarified terminal labels.

### Verification

- JavaScript syntax, 21 unit tests, the complete source check, and
  `git diff --check` pass.
- Authenticated Chromium rendered the deployed new-session page at 1100x1800:
  the main form no longer shows the permission selector and the collapsed
  `Opzioni avanzate` panel is visible at the bottom.
- Source and live `app.js` are byte-identical. Only that static asset was
  installed; `agent-hub.service` was not restarted and retained PID 3301127.
  The previous asset is recoverable under
  `/opt/agent-hub/backups/host/20260825-new-session-advanced-options/`.

### Residual work

- No commit or GitHub push was requested or performed.

## 2026-08-25 — Health status and session actions simplified

Status: implemented, deployed, and verified on the local Agent Hub instance.

### Behavior

- The Status page now presents five stable summaries: Hardware, Network/VPN,
  Agent Hub backend, Agent Hub frontend, and General. The underlying health
  collector is unchanged; all 19 deterministic checks remain available under
  an expandable technical-details section, and unknown future checks fall
  back to General.
- The additional service, container, tmux, VPN, version, disk, and directory
  diagnostics remain available in one collapsed technical section instead of
  occupying the initial view.
- The existing Escape-based generation interrupt is now labelled `Pausa` in
  the session UI. Destructive actions are ordered by impact as Restart, Kill,
  then Elimina.

### Verification

- Source checks pass: 12 Python files compile, JavaScript parses, 20 unit tests
  pass, `git diff --check` passes, and the Ansible safety playbook passes.
- Authenticated Chromium rendered the deployed Status and session-detail
  pages at 1440x1200. DOM assertions confirmed five summaries, all 19 detailed
  checks, visible Pause, and Restart before Kill before Elimina.
- A fresh end-to-end health run reports 19/19 OK. Agent Hub, its private
  socket, Tailscale, and SSH remain active; no systemd unit is failed.
- Only `app/static/{app.js,style.css}` was installed. `agent-hub.service` was
  not restarted and retained its PID and activation timestamp. The previous
  assets are recoverable under
  `/opt/agent-hub/backups/host/20260825-status-summary-actions/`.

### Residual work

- No GitHub push was requested or performed; publish the local infrastructure
  commits separately when desired.

## 2026-08-24 — Cross-account prompt handoff repaired

Status: implemented, deployed, and verified on the local Agent Hub instance.

### Behavior

- Moved transient prompt files out of the service-private socket directory and
  into `/run/agent-hub-inputs`, managed as `0710 agenthub:agentprojects`.
  Agent accounts can traverse the directory for the exact random filename they
  receive, while they cannot list other prompt files.
- `session-ctl` accepts only the new input directory (plus the existing private
  `/tmp/agent-hub-*` compatibility path). Launch failures are now classified as
  `LAUNCH_FAILED`; the persisted session and prompt remain visible instead of
  looking like a lost form submission.
- The HTTP exception handler now preserves the diagnostic response headers
  consumed by the UI, so a failed launch opens the saved session detail.

### Verification

- Source checks pass: 12 Python files compile, JavaScript parses, 18 unit tests
  pass, `git diff --check` passes, and the Ansible safety playbook passes.
- Existing explicit initial-prompt test delivered `PROMPT_HANDOFF_OK` and
  recorded `COMPLETED`. A post-restart HTTPS/CSRF follow-up delivered by paste,
  produced `FOLLOWUP_PROMPT_OK`, and recorded a second `COMPLETED` report.
- Restarting only `agent-hub.service` preserved the two real tmux sessions.
  The completed test pane was then closed through the API while its DB history
  and transcript were retained.
- Live/source files are aligned. The pre-audit backend is recoverable under
  `/opt/agent-hub/backups/host/20260824-prompt-handoff-audit/`.

### Residual work

- No GitHub push was requested or performed; publish the local infrastructure
  commits separately when desired.

## 2026-08-20 — Session status, project tiles, and nested meetings restored

Status: implemented, tested, and deployed to the local Agent Hub instance.

### Behavior

- Recovered the August 12 UX from the SSD-resident `agent-hub-legacy`
  history: the TUI keypad remains grouped as `Up`, `Down`, `Enter`, and `Esc`;
  `WAITING_SESSION` is visible in the web UI and Telegram; and project cards
  are again distinct, colored, responsive tiles.
- Session process state and turn outcome are now shown as separate axes. A
  live pane stays in `Sessioni aperte`; a current `COMPLETED` report reads
  `Turno completato` and never archives that live session. Closed sessions
  live in a collapsed section, and report summaries are one-line previews.
- Removed Meetings from the global navigation. Each project now has nested
  `Panoramica`, `Documenti`, `Riunioni`, and `Contesto` tabs. The meeting tab
  is the entry point for history and new meetings, and meeting details return
  there.
- Consolidated the previously deployed project/meeting simplification: project
  context owns target architecture; approved meeting work updates roadmap and
  future-facing documentation rather than application code; open questions,
  evidence, action owner, and due date remain explicit in the report contract.

### Deployment and verification

- Installed only `app/static/{app.js,index.html,style.css}` and
  `libexec/telegram-ctl`. `agent-hub.service` was not restarted; its PID and
  activation timestamp stayed unchanged. Restarting the separate Telegram
  notifier preserved all three live tmux sessions.
- `scripts/check-source.sh` passes with 11 tests, including state dependency,
  navigation, two-axis status, and project UI contracts. Source/live
  checksums, authenticated bootstrap, light-theme screenshots at 1440x1000,
  service health, SSH, and Tailscale were verified.
- Recoverable pre-change files are in
  `/opt/agent-hub/backups/host/20260820-session-project-meetings-ui/`.
- The HDD was neither mounted nor accessed; comparison used only the legacy
  repository and historical handoff already present on the SSD.

### Residual work

- None.

## 2026-08-19 — Project, meeting, and context simplification

Status: implemented and deployed to the local Agent Hub instance. No commit,
push, or remote repository action was requested.

### Behavior

- `Projects` is now a compact project list. A project detail page contains its
  documents, recent meetings, editable consolidated context, Git/deployment
  state, and handoff.
- `Meetings` now has two explicit modes: meeting history and new meeting. The
  project context editor no longer appears there.
- Meeting analysis and revision use the same decision-report contract.
  Decisions require transcript evidence and are separated from open questions
  and actions. Existing target architecture is preserved unless the transcript
  contains an explicit change.
- The report validator drops empty/exact duplicate items, preserves decision
  rationale/evidence and action owner/due date, and records open questions in
  minutes and context packages. Telegram approves only decisions and points to
  open questions as non-approved information.
- Approved actions now reach the documentation session as roadmap work items;
  open questions reach it only as unresolved backlog/blocker information.
- Post-approval UI and Telegram wording now match the actual behavior: the
  dedicated session updates future-facing documentation and roadmap, not code.

### Deployment

- Updated `/opt/agent-hub/app`, `/usr/local/libexec/agent-hub`, and
  `/usr/local/bin/agent-meeting-report` with canonical repository files.
- Restarted `agent-hub.service` and `agent-hub-telegram.service`; both are
  active. Persistent tmux sessions were not stopped.
- Recoverable pre-change backup:
  `/opt/agent-hub/backups/host/20260819-project-meeting-simplification/`.

### Verification

- `./scripts/check-source.sh`: passed; 12 Python sources compiled, JavaScript
  syntax passed, 6 unit tests passed, and `git diff --check` passed.
- Live static assets, installed-file equality, `/api/projects` response shape,
  and post-restart service logs: passed.
- `ansible-playbook` and the announced `agent-executor` launcher were not
  available in this SERVER session, so those two commands could not run.

### Residual risk

- The next real meeting is the end-to-end validation of model behavior for the
  stronger report contract. Existing proposals remain backward-compatible.

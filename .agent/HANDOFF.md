# Handoff

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

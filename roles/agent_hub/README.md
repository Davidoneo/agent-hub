# Role: agent_hub

Installs **Agent Hub**: persistent agent sessions (Claude Code, Codex,
OpenCode) driven from a private web UI, reachable only over Tailscale.

Sessions run inside per-user `tmux` servers, so closing the browser or
restarting the backend does not stop the work in progress.

Session input is a durable SQLite queue, serialized per session. Backend
restarts resume interrupted paste deliveries, and delivery failures are raised
as attention states instead of remaining silent. The wrapper records `sent`
only after observing that the target TUI consumed the submit.

Opt-in, like every role here. It stays inert until you set:

```yaml
agent_hub_install: true
```

## What it sets up

| Component | Purpose |
|---|---|
| `agent-hub.socket` | systemd-owned Unix listener, mode `0600` |
| `agent-hub.service` | FastAPI/uvicorn receives that listener by file descriptor and serves the UI/API |
| `agent-hub-telegram.service` | Optional status/approval/share bot, the only component reaching the internet |
| `agent-hub-backlog-telegram.service` | Optional dedicated collector for text and voice ideas |
| `agent-hub-health.timer` | Deterministic host health snapshot every 30 minutes |
| `agent-hub-harness-update.timer` | Nightly stable harness updates; changed/error cycles get an ephemeral read-only Codex audit |
| `/usr/local/libexec/agent-hub/*-ctl` | Root-owned wrappers, the privilege boundary |
| `/usr/local/bin/agent-*` | Stable report, Telegram share, meeting and document commands for sessions |
| Three Unix accounts | Service account plus two agent accounts, deliberately separated |

## The privilege split

This is the point of the role, so it is worth stating plainly.

The web service runs as an unprivileged **service account**. It never runs
agent commands itself: it may only call the wrappers under
`/usr/local/libexec/agent-hub/`, through a narrow `sudoers` rule, as one of
two **agent accounts**:

- **project account** - runs agent sessions against your repositories, owns
  a rootless Docker daemon and has no account-wide GitHub credential. The
  root-owned `git-ssh-ctl` selects only the current project's deploy key from
  process ancestry; arbitrary root commands remain denied.
- **server account** - runs sessions that may administer the host. Passwordless
  root is disabled by default and requires both `agent_hub_server_admin: true`
  and `agent_hub_server_admin_confirm: true`.

A compromised project session therefore cannot escalate to the host, and the
web service cannot run arbitrary commands even if it is compromised.

Optional passive wrappers append every `gh` invocation and every `git push` to
`/var/log/agent-hub/github-usage.log`. They are disabled by default and never
log token values. Repository deletion is not provisioned: keep `delete_repo`
out of standing tokens and grant it only for an explicit operation.

## Required variables

The role refuses to run without these, because defaults would be unsafe:

```yaml
agent_hub_install: true
agent_hub_origin: "https://myhost.tailXXXX.ts.net"   # for CSRF/WebSocket
agent_hub_allowed_users:                             # Tailscale-User-Login
  - you@example.com
```

Accounts default to `agenthub`, `devagent` and `hostagent`. Override
them if those names are taken:

```yaml
agent_hub_service_user: agenthub
agent_hub_project_user: devagent
agent_hub_server_user: hostagent
```

Telegram is off by default. Keep the token in a vault:

```yaml
agent_hub_telegram_enabled: true
agent_hub_telegram_token: "{{ vault_agent_hub_telegram_token }}"
```

The status bot keeps status commands on demand, handles host-escalation yes/no
and meeting approvals, and delivers `agent-telegram` text/link/file shares. It
does not collect ideas and does not push routine report, lifecycle, or health
messages.

An optional instance overlay can install a root-owned
`service-notification-ctl` wrapper that validates the caller and renders a fixed
vocabulary of application alerts. Application-specific wrappers and tests belong
in the private operations repository. Install and review that wrapper before
enabling its narrowly scoped sudo rule; the public role provides only the queue
and delivery support, without granting the caller the generic `agent-telegram`
interface or access to the bot token:

```yaml
agent_hub_service_notifications_enabled: true
agent_hub_service_notification_user: example-app
```

The Backlog collector is a second, dedicated bot. It accepts free text, Telegram
voice notes, audio and audio documents only from its paired chat:

```yaml
agent_hub_backlog_telegram_enabled: true
```

On first deployment the role creates the root-owned
`/etc/agent-hub/backlog-telegram.env` with an empty BotFather token and a random
pairing code. Complete `AGENT_HUB_BACKLOG_TG_TOKEN`, restart
`agent-hub-backlog-telegram`, then send `/start <pairing-code>` to that bot.
The default `AGENT_HUB_BACKLOG_TRANSCRIBE_PROVIDER=local` uses
`faster-whisper` and cannot incur API charges. For fast cloud transcription,
the recommended option is Groq's Free plan: set the provider to `groq`, add
`AGENT_HUB_BACKLOG_GROQ_API_KEY` and keep
`AGENT_HUB_BACKLOG_GROQ_MODEL=whisper-large-v3`. OpenAI remains an explicit
opt-in with provider `openai` and its dedicated key; merely inserting any key
does not switch provider. A cloud failure leaves the audio available for an
explicit retry and never silently invokes the slower local model. Title,
description and the private launch prompt are prepared by an ephemeral
read-only Codex run, with a deterministic fallback if Codex is temporarily
unavailable.

Standards-based Web Push is also opt-in. The role generates the VAPID private
key directly on the host; do not put that key in inventory:

```yaml
agent_hub_webpush_enabled: true
```

After deployment, each device subscribes independently through the
`notifiche: off` button. On iOS/iPadOS the site must first be added to the Home
Screen. While that PWA is visible Agent Hub uses its in-app attention panel;
when it is in the background the service worker shows the system notification.

The Claude usage reader enables `agent_hub_claude_auto_refresh` by default. If
a renewable local OAuth access token is expired, it performs at most one
safe-mode CLI startup every 20 hours, stops it as soon as the credential
changes, and never sends a model prompt.

See `defaults/main.yml` for the full list.

Harness updates are enabled with the Agent Hub role and run nightly at 03:20
(with a randomized delay). Codex and OpenCode follow the stable npm `latest`
tag in their global installation; Claude Code installs the explicit `stable`
channel independently for the PROJECT and SERVER accounts. Update steps have a
five-minute timeout and continue independently so one failure cannot hide the
other results.

No LLM runs when every installed version remains unchanged and all updaters
succeed. A change or error starts `codex exec --ephemeral` as the PROJECT
account with read-only sandbox, no approval prompts and a bounded runtime. Its
prompt permits only official release-note/CLI compatibility checks: no file or
settings changes, tests/builds, package installs, TUI sessions, login, push,
deploy, restart, rollback or escalation. The process exits after its one turn,
so it never appears among Agent Hub sessions and cannot wait for daily manual
attention. An identical successful rerun on the same day is skipped.

The root oneshot writes one atomic, group-readable report to
`/var/lib/agent-hub/harness-update.json`. The Status page shows versions,
per-updater output, audit summary, omitted checks, errors and timer state. No
Telegram or Web Push notification is emitted. Updater or audit failures make
the systemd unit fail after the report has been saved; there is deliberately no
automatic rollback or repository fix. Override or disable this policy with:

```yaml
agent_hub_harness_updates_enabled: false
agent_hub_harness_update_calendar: "*-*-* 03:20:00"
agent_hub_harness_audit_project: agent-hub
agent_hub_harness_audit_model: gpt-5.6-sol
agent_hub_harness_audit_effort: high
agent_hub_harness_audit_timeout: 1200
```

## Blocking states and automatic resume

`controller.json` carries the controller thresholds and the patterns that let a
live session be recognised as *blocked* rather than *working*. They are matched
against the pane the TUI is showing right now, never the PTY scrollback, so
they must be text that stays on screen for as long as the obstacle lasts — and
an agent that merely quotes the sentence in its own output does not create a
false state. The file is re-read whenever it changes, so tuning a threshold
needs no service restart.

This matters more than it looks: with no `USAGE_LIMIT` pattern the harness
spinner keeps the PTY log moving, so a session parked on a usage limit stays
`RUNNING` forever, its wait is billed as work time, and no notification is ever
raised. `AUTH_REQUIRED` ships empty on purpose — those screens were not
verified for every harness, and a wrong pattern would freeze a healthy session.

Once a session is recognised as `USAGE_LIMIT`, Agent Hub keeps it alive instead
of preparing a replacement: the harness process still holds the whole
conversation in memory, and rebuilding that from a transcript would spend the
budget that has just come back. It waits for the reset instant published in
`provider_usage`, leaves `resume_retry_seconds` of grace so a harness that
resumes on its own goes first (Claude Code does, with
`autoContinueAtUsageLimit`), re-reads the pane to confirm both that the
obstacle is still there and that the previous attempt is not still sitting
unaccepted in the composer, and only then hands the session one more turn -
otherwise repeated attempts would stack up and all go out on the first human
Enter. The attempt travels as a
`control` message: a failed one can never block the human messages queued
behind it, and every attempt is recorded in `usage_resume_attempts`. After
`resume_max_attempts` the session stays visibly parked and the decision goes
back to a person; set that to `0` to disable the resume altogether.

A Codex goal parked with `Goal hit usage limits` stays parked: the resumed turn
carries the work forward, but re-arming the goal itself still needs `/goal
resume` typed in the session.

## Exposure

systemd creates `/run/agent-hub/agent-hub.sock` with no group/other access and
passes the open listener to Uvicorn. This deliberately avoids Uvicorn's direct
Unix-socket mode, which changes its socket to `0666`. No TCP port is opened.
Put the socket behind Tailscale Serve:

```bash
sudo tailscale serve --bg unix:/run/agent-hub/agent-hub.sock
```

The role applies that root handler automatically when
`agent_hub_manage_tailscale_serve` is true (the default), while preserving
other paths already served by the node.

Tailscale terminates TLS and injects the `Tailscale-User-Login` header, which
the service checks against `agent_hub_allowed_users`. With
`agent_hub_require_tailscale: true` (the default) a request without that
header is refused.

Tailscale strips spoofed identity headers from remote requests, but processes
on the same host must not be able to reach the trusted backend directly. The
runtime directory is owned by the Agent Hub service account, the systemd-owned
socket stays at `0600`, and only root-owned Tailscale Serve can cross that
boundary. The health controller verifies both permissions and a real HTTPS
request through Tailscale instead of trusting process/configuration state.

Unattended host services may receive a single repository deploy key through
`agent_hub_service_deploy_profiles`. Each private-inventory entry binds a
profile name to the expected sudo caller, real systemd unit and project. The
root-owned wrapper verifies the caller and process cgroup before selecting the
key; the public defaults contain no profiles.

## Canonical application sources

`app/`, `libexec/` and `bin/` at the repository root are the source of truth.
The role installs them directly, so there is no second vendored copy to drift.
The sync helper exists only to import a deliberate emergency hotfix made on a
running host:

```bash
./scripts/sync-agent-hub.sh
```

The script refuses to import anything that looks like a credential, personal
email or `*.ts.net` host. Instance configuration is never copied into Git.

`app/static/vendor/` contains xterm.js under the MIT licence; see the
`NOTICE` file next to it.

## Limits

- Debian 13 and Ubuntu 24.04+ only; it assumes `systemd` and user lingering.
- The role configures only Agent Hub's root Tailscale Serve handler. It does
  not install or enroll Tailscale: use the `tailscale` role, or bring your own
  enrolled node.
- `profiles.json` (which agent CLIs are offered) is not managed here: it is
  installation-specific and belongs to your private inventory.

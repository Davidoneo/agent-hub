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
| `agent-hub-telegram.service` | Optional notifier, the only component reaching the internet |
| `agent-hub-health.timer` | Deterministic host health snapshot every 30 minutes |
| `/usr/local/libexec/agent-hub/*-ctl` | Root-owned wrappers, the privilege boundary |
| `/usr/local/bin/agent-*` | Stable report, meeting and document commands for sessions |
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

# Role: agent_hub

Installs **Agent Hub**: persistent agent sessions (Claude Code, Codex,
OpenCode) driven from a private web UI, reachable only over Tailscale.

Sessions run inside per-user `tmux` servers, so closing the browser or
restarting the backend does not stop the work in progress.

Opt-in, like every role here. It stays inert until you set:

```yaml
agent_hub_install: true
```

## What it sets up

| Component | Purpose |
|---|---|
| `agent-hub.service` | FastAPI/uvicorn on loopback, serves the UI and the API |
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

See `defaults/main.yml` for the full list.

## Exposure

The service binds `127.0.0.1` and is never published by opening a port. Put
it behind Tailscale Serve:

```bash
tailscale serve --bg 127.0.0.1:8787
```

Tailscale terminates TLS and injects the `Tailscale-User-Login` header, which
the service checks against `agent_hub_allowed_users`. With
`agent_hub_require_tailscale: true` (the default) a request without that
header is refused. The loopback guard below protects the separate local
caller boundary.

Tailscale strips spoofed identity headers from remote requests, but processes
on the same host could otherwise connect straight to loopback and provide
their own value. `agent_hub_loopback_guard_enabled: true` installs an nftables
output rule that allows only root-owned local proxies, including `tailscaled`,
to reach the backend port. Keep it enabled whenever identity headers are used.

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
- The role installs the service but does not configure Tailscale itself: use
  the `tailscale` role, or bring your own tunnel.
- `profiles.json` (which agent CLIs are offered) is not managed here: it is
  installation-specific and belongs to your private inventory.

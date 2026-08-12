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
| `/usr/local/libexec/agent-hub/*-ctl` | Root-owned wrappers, the privilege boundary |
| Three Unix accounts | Service account plus two agent accounts, deliberately separated |

## The privilege split

This is the point of the role, so it is worth stating plainly.

The web service runs as an unprivileged **service account**. It never runs
agent commands itself: it may only call the wrappers under
`/usr/local/libexec/agent-hub/`, through a narrow `sudoers` rule, as one of
two **agent accounts**:

- **project account** - runs agent sessions against your repositories, owns
  a rootless Docker daemon. It cannot reach host privileges.
- **server account** - runs sessions that administer the host.

A compromised project session therefore cannot escalate to the host, and the
web service cannot run arbitrary commands even if it is compromised.

## Required variables

The role refuses to run without these, because defaults would be unsafe:

```yaml
agent_hub_install: true
agent_hub_origin: "https://myhost.tailXXXX.ts.net"   # for CSRF/WebSocket
agent_hub_allowed_users:                             # Tailscale-User-Login
  - you@example.com
```

Accounts default to `agenthub`, `projectagent` and `serveragent`. Override
them if those names are taken:

```yaml
agent_hub_service_user: agenthub
agent_hub_project_user: projectagent
agent_hub_server_user: serveragent
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
header is refused, so a local process cannot bypass the identity check.

## The vendored application

`files/app/` and `files/libexec/` hold a copy of the application. Do not edit
those files here: change them in the Agent Hub source repository, deploy, and
then refresh the copy with

```bash
./scripts/sync-agent-hub.sh
```

The script also refuses to sync anything that looks like a credential, a
personal email or a `*.ts.net` host. Instance configuration is never
vendored: `config.env` is rendered from your Ansible variables.

`files/app/static/vendor/` contains xterm.js under the MIT licence; see the
`NOTICE` file next to it.

## Limits

- Debian 13 and Ubuntu 24.04+ only; it assumes `systemd` and user lingering.
- The role installs the service but does not configure Tailscale itself: use
  the `tailscale` role, or bring your own tunnel.
- `profiles.json` (which agent CLIs are offered) is not managed here: it is
  installation-specific and belongs to your private inventory.

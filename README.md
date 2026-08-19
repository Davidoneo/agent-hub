# Agent Hub

Agent Hub is a private web interface for persistent Claude Code, Codex and
OpenCode sessions on a Debian or Ubuntu host. Sessions run in per-user `tmux`
servers, survive browser and backend restarts, and are separated into:

- **PROJECT** — unprivileged project work with per-repository deploy keys;
- **SERVER** — optional host administration, disabled by default.

The repository is the single source of truth for the application, its
root-owned privilege wrappers and the Ansible installer. Instance inventory,
credentials, recovery snapshots and application-specific services do not
belong here.

## Install

Supported targets are Debian 13+ and Ubuntu 24.04+.

```bash
git clone https://github.com/Davidoneo/agent-hub.git
cd agent-hub
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ansible-galaxy collection install -r requirements.yml
cp inventory/hosts.example inventory/hosts
cp inventory/group_vars/all.example.yml inventory/group_vars/all.yml
$EDITOR inventory/hosts inventory/group_vars/all.yml
ansible-playbook -i inventory/hosts site.yml --check --diff
ansible-playbook -i inventory/hosts site.yml
```

Every state-changing role is opt-in. Agent Hub additionally requires an HTTPS
origin and an explicit allow-list of Tailscale identities. Full SERVER sudo
requires a second confirmation variable.

See [installation](docs/INSTALL.md), [the Agent Hub role](roles/agent_hub/README.md),
[SSH access](docs/SSH.md) and [security](SECURITY.md).

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | FastAPI backend and browser UI |
| `bin/` | Commands exposed to agent sessions |
| `libexec/` | Root-owned privilege and health wrappers |
| `roles/agent_hub/` | Ansible deployment of the canonical sources above |
| `roles/{common,docker,tailscale,...}/` | Optional Debian host preparation |
| `packaging/` | Optional host integration such as GitHub audit wrappers |

## Security model

The backend binds only to loopback and is intended to sit behind Tailscale
Serve. A separate nftables output guard prevents non-root local processes from
connecting directly and forging Tailscale identity headers. The PROJECT user
never receives an account-wide GitHub credential; root selects a deploy key
from the verified Agent Hub session and project.

SERVER administration and GitHub command auditing are deliberately disabled
by default. Enabling passwordless root requires both:

```yaml
agent_hub_server_admin: true
agent_hub_server_admin_confirm: true
```

MIT licensed. Review `--check --diff` before every first apply and keep an
alternate console available while changing SSH, firewall or networking.

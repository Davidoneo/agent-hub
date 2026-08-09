# Home Server Agent Setup

Ansible playbook for preparing a Debian or Ubuntu home server to run agents
and manage them remotely over SSH or Tailscale. It can also configure a laptop
or a local development machine.

Supported targets: Debian 13+ and Ubuntu 24.04+.

## Install

```bash
git clone https://github.com/Davidoneo/debian-server-blueprint.git
cd debian-server-blueprint
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ansible-galaxy collection install -r requirements.yml
cp inventory/group_vars/all.example.yml inventory/group_vars/all.yml
```

For the local machine:

```bash
cp inventory/local.example inventory/hosts
ansible-playbook -i inventory/hosts site.yml --check --diff -K
ansible-playbook -i inventory/hosts site.yml -K
```

For a remote server:

```bash
cp inventory/hosts.example inventory/hosts
$EDITOR inventory/hosts inventory/group_vars/all.yml
ansible-playbook -i inventory/hosts site.yml --check --diff
ansible-playbook -i inventory/hosts site.yml
```

Set `blueprint_profile` to `laptop` or `server`. Real inventory files are
ignored by Git.

## Remote access

SSH setup is opt-in. Add public keys from trusted clients, test access in a
second terminal, then enable SSH hardening and UFW. SSH can be limited to
trusted CIDRs or the Tailscale interface. The playbook checks the admin key,
`AllowUsers`, and firewall port before disabling password access.

## Roles

| Role | Default |
|---|---|
| Base packages and system settings | enabled |
| OpenSSH server and trusted keys | disabled |
| Docker Engine | disabled |
| Tailscale installation | disabled |
| SSH hardening | disabled, confirmation required |
| UFW firewall | disabled, confirmation required |

See [installation](docs/INSTALL.md), [SSH setup and recovery](docs/SSH.md),
and [security](SECURITY.md). Other distributions require a fork with adjusted
packages, services, paths, and tests.

MIT licensed. Run `--check --diff` before applying changes and keep another
console open when changing SSH or firewall rules.

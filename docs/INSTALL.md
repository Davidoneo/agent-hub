# Installation and first use

Practical steps for preparing a **Debian 13** or **Ubuntu 24.04** machine with
this blueprint, either a server (managed remotely over SSH) or a laptop (run
locally). The steps are identical; only the inventory differs.

> Golden rule: always run `--check --diff` first, and for the first real
> apply keep an alternate console open (second terminal, physical console or
> IPMI). A misconfigured SSH or firewall can cut off remote access.

## 1. Prerequisites

On the control machine (the one you run Ansible from):

- Linux or macOS, `git`, and an SSH client.
- Python 3.11+ and the versions declared by the project. Install in an
  isolated environment:

  ```bash
  python3 -m venv .venv
  . .venv/bin/activate
  python3 -m pip install -r requirements-dev.txt
  ```

On the target host:

- Debian 13 (trixie) or Ubuntu 24.04.
- An admin user with `sudo` and, for remote use, SSH access.

## 2. Clone the repository

```bash
git clone https://github.com/Davidoneo/debian-server-blueprint.git
cd debian-server-blueprint
```

## 3. Dependencies (collections)

The playbook uses `community.general` and `ansible.posix`. Install the
declared versions:

```bash
ansible-galaxy collection install -r requirements.yml
```

To update/recreate from scratch: `ansible-galaxy collection install -r
requirements.yml --force`.

## 4. Copy inventory and group_vars

The example files are safe and contain no real data. Copy and customize them
(the real files are in `.gitignore`, they never reach Git):

```bash
cp inventory/hosts.example inventory/hosts
cp inventory/group_vars/all.example.yml inventory/group_vars/all.yml
$EDITOR inventory/hosts
$EDITOR inventory/group_vars/all.yml
```

- `inventory/hosts`: lists your hosts, e.g.
  `server1.example.invalid ansible_host=192.0.2.10 ansible_user=deploy`.
- `inventory/group_vars/all.yml`: all variables, shipped in "safe" form
  (SSH server, keys, firewall, hardening, and Tailscale are opt-in).

## 5. Laptop or server profile

Set `blueprint_profile: laptop` or `server` first in the variables file.

**Remote server or laptop**: add the host to `[managed]` with `ansible_host`
and `ansible_user`, then use the real inventory:

```bash
ansible-playbook -i inventory/hosts site.yml --check --diff
```

**Local laptop or server**: use the provided local inventory:

```bash
cp inventory/local.example inventory/hosts
ansible-playbook -i inventory/hosts site.yml --check --diff -K
```

Practical differences to keep in mind on a laptop:

- `sudo` may ask for a password: use `-K`/`--ask-become-pass`.
- The `firewall` role restricts incoming traffic: leave it disabled on a
  laptop unless you need to expose services, or allow only trusted networks.
- `common_hostname`, timezone, and unattended-upgrades apply to both
  profiles; on a laptop consider `common_unattended_upgrades_reboot: false`.

## 6. Check mode (dry run)

Before any real change, and always on the first run:

```bash
ansible-playbook -i inventory/hosts site.yml --check --diff
```

No change is applied; `--diff` shows what would change. Make sure the
preflight checks do not fail and the "changed" items are the expected ones.

## 7. Applying by tag

By default the playbook applies only the `common` role (safe baseline);
Docker, firewall, SSH hardening, and Tailscale are explicit opt-ins. To apply
a single module:

```bash
ansible-playbook -i inventory/hosts site.yml --tags common
```

Available tags:

| Tag | Role | Effect |
|---|---|---|
| `common` | common | base packages, timezone, locale, hostname, unattended-upgrades (opt-in) |
| `ssh_access` | ssh_access | OpenSSH server and trusted public keys (opt-in) |
| `docker` | docker | Docker Engine from the official repo + `docker` group (opt-in) |
| `ssh_hardening` | ssh_hardening | SSH hardening drop-in (double opt-in, see `docs/SSH.md`) |
| `firewall` | firewall | ufw with deny policy (double opt-in) |
| `tailscale` | tailscale | verified install; manual auth |
| `baseline` | all | all roles, with their opt-ins |

You can restrict the run to one host with `--limit server1.example.invalid`.
After a partial run you can re-run the same command: the playbook is
idempotent.

## 8. Rollback and manual safety

The blueprint is designed not to lock the machine:

- **No system files overwritten at random**: SSH hardening generates a
  *drop-in* in `/etc/ssh/sshd_config.d/` (the main file is untouched); the
  firewall uses ufw; Docker and Tailscale install standard packages.
- **Mandatory double confirmation**: firewall and SSH hardening require
  `*_enabled=true` **and** `*_confirm=true`; the other features stay opt-in.
- **Manual rollback** (with access to the machine):
  - SSH: remove `/etc/ssh/sshd_config.d/99-baseline-hardening.conf` and run
    `systemctl reload ssh`.
  - Firewall: `sudo ufw disable`.
  - Tailscale: `sudo tailscale down` if you authenticated it manually.
- Always keep an out-of-band access channel and a second session open while
  applying SSH/firewall changes.
- Never commit real inventory, `.env`, keys, or tokens: use Ansible Vault or
  a secret manager with the decryption key outside the repository.

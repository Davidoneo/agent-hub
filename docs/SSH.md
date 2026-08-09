# SSH access: keys, bootstrap, and hardening

Recommended procedure for using the blueprint without ever locking yourself
out of the machine. All addresses, hostnames, and comments are placeholders
(`example.invalid`, TEST-NET network `192.0.2.0/24`): replace them with
yours, but never commit real data or private keys.

## 1. Generate an ed25519 key on each trusted machine

On the control machine **and on every laptop/PC that must access** the server
(never share the private key, and never generate the key directly on the
server):

```bash
ssh-keygen -t ed25519 -a 100 -C "laptop-1-key@example.invalid"
```

Tips:

- Accept the default path `~/.ssh/id_ed25519`; protect it with a passphrase
  (optional but recommended).
- Check the permissions: `chmod 600 ~/.ssh/id_ed25519`.
- The **public** key is `~/.ssh/id_ed25519.pub` (one line starting with
  `ssh-ed25519 AAAA...`). It is the only thing that should leave the machine.

## 2. Put only public keys in `ssh_access_public_keys`

In `inventory/group_vars/all.yml` (never committed) define the admin user and
the **list of public keys only**:

```yaml
ssh_access_enabled: true
ssh_access_user: deploy
ssh_access_install_server: true
ssh_access_public_keys:
  - "ssh-ed25519 AAAA... laptop-1-key@example.invalid"
  - "ssh-ed25519 AAAA... laptop-2-key@example.invalid"
```

Never put a private key here. First bootstrap uses the dedicated role:

```bash
ansible-playbook -i inventory/hosts site.yml --tags ssh_access --check --diff
ansible-playbook -i inventory/hosts site.yml --tags ssh_access
```

The command is repeatable: adding a key to the list and re-running is safe
and idempotent.

## 3. First bootstrap without disabling the password

SSH hardening is **disabled by default** (password login stays active). This
is why the first access is safe:

```bash
ssh deploy@server1.example.invalid
```

- Complete step 2 (key installation) **before** any hardening.
- Verify that key authentication already works at this stage, while the
  password is still active as a "safety net".

## 4. Test from a second terminal

Never close the working session before verifying everything:

1. Keep the session from step 3 open.
2. From a **second terminal** test key login:

   ```bash
   ssh -i ~/.ssh/id_ed25519 deploy@server1.example.invalid
   ```

3. Only if key login works, proceed with hardening. If something goes wrong,
   the first session is still open for rollback.

## 5. Then hardening and firewall

Enable SSH hardening only now, with the double confirmation required by the
preflight checks and the `AllowUsers` list filled in (mandatory when passwords
are disabled):

```yaml
ssh_hardening_enabled: true
ssh_hardening_confirm: true
ssh_hardening_admin_user: deploy
ssh_hardening_port: 22
ssh_hardening_password_auth: false
ssh_hardening_allow_users:
  - deploy
```

Apply by tag:

```bash
ansible-playbook -i inventory/hosts site.yml --tags ssh_hardening --check --diff
ansible-playbook -i inventory/hosts site.yml --tags ssh_hardening
```

The role generates a drop-in validated with `sshd -t` and reloads without
interrupting active sessions (`state: reloaded`).

**Firewall with trusted CIDR** (example TEST-NET network — replace with
yours): allow SSH only from your network:

```yaml
firewall_enabled: true
firewall_confirm: true
firewall_allow_ssh_from_anywhere: false
firewall_ssh_port: 22
firewall_ssh_sources:
  - "192.0.2.0/24"
firewall_allow_tailscale_ssh: false
```

Apply the firewall by tag only after verifying that the client source address
really belongs to the given CIDR:

```bash
ansible-playbook -i inventory/hosts site.yml --tags firewall --check --diff
ansible-playbook -i inventory/hosts site.yml --tags firewall
```

**Alternatively: Tailscale** — if you prefer not to open ports to the
Internet, use the Tailscale network (reserved range `100.64.0.0/10`):

1. Install Tailscale, then authenticate the node interactively:
   ```bash
   ansible-playbook -i inventory/hosts site.yml --tags tailscale
   ssh deploy@server1.example.invalid
   sudo tailscale up
   ```
2. Set `firewall_allow_tailscale_ssh: true` and keep
   `firewall_allow_ssh_from_anywhere` disabled; then apply UFW.
3. Connect via MagicDNS name or Tailscale address. SSH hardening stays valid
   without exposing port 22 globally.

Always align the SSH port (`ssh_hardening_port`/`firewall_ssh_port`) between
the two roles: the preflight checks enforce it.

## 6. Example client-side `~/.ssh/config`

For convenience, on your control machine:

```
Host server1
    HostName server1.example.invalid
    User deploy
    Port 22
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

Then just `ssh server1`. Add `IdentitiesOnly yes` if you have multiple keys in
the agent.

## 7. Lockout recovery

If you lose SSH access (misconfiguration, lost keys, firewall):

1. Use the out-of-band channel: physical console, IPMI, serial, or (if
   enabled) Tailscale.
2. Log in as a sudo user or as root from the console.
3. Restore SSH: remove the drop-in and reload the service:
   ```bash
   sudo mv /etc/ssh/sshd_config.d/99-baseline-hardening.conf \
     /etc/ssh/sshd_config.d/99-baseline-hardening.conf.disabled
   sudo systemctl reload ssh
   ```
4. If the firewall is the blocker: `sudo ufw disable` (then re-apply the
   blueprint with a correct CIDR).
5. Verify from a new terminal with `ssh -v server1` before closing the
   console.

**Prevention** (the best way): at least two valid public keys in
`ssh_access_public_keys` (e.g. two laptops or a hardware key), a test from a
second terminal before every hardening, a backup of the config file drop-in,
and working sessions left open during changes.

# Role `ssh_hardening` - STRICT OPT-IN

OpenSSH hardening via a drop-in in `/etc/ssh/sshd_config.d/`.

> **WARNING**: a wrong SSH configuration can lock you out of the machine.
> The role is strictly opt-in: by default it touches nothing.

## Enabling (requires TWO variables)

```yaml
ssh_hardening_enabled: true
ssh_hardening_confirm: true
ssh_hardening_admin_user: your_user
ssh_hardening_allow_users: [your_user]   # required if password_auth=false
```

The preflight checks refuse to proceed if:
- `ssh_hardening_enabled=true` without `ssh_hardening_confirm=true`;
- `ssh_hardening_password_auth=false` with empty `ssh_hardening_allow_users`
  (lock-out protection);
- the admin user does not exist or has no non-empty `authorized_keys`;
- the chosen SSH port is not allowed by the `firewall` role (if active).

## How the safety works

- **Drop-in with precedence**: on Debian/Ubuntu `/etc/ssh/sshd_config`
  starts with `Include /etc/ssh/sshd_config.d/*.conf`; the first value read
  wins, so the drop-in overrides the main file without touching it.
- **Validation**: the template uses `validate: sshd -t -f %s` and, after
  applying, `sshd -t` runs on the full configuration before any reload.
- **Non-destructive reload**: the handler uses `state: reloaded` (HUP) by
  default: it does not interrupt active SSH sessions. Set
  `ssh_hardening_service_action: restarted` only if you really need it.

## Main variables

| Variable | Default | Notes |
|---|---|---|
| `ssh_hardening_enabled` | `false` | main **opt-in** |
| `ssh_hardening_confirm` | `false` | explicit confirmation |
| `ssh_hardening_admin_user` | `""` | user with a verified key |
| `ssh_hardening_port` | `22` | SSH port |
| `ssh_hardening_permit_root_login` | `"no"` | root login |
| `ssh_hardening_password_auth` | `false` | password authentication |
| `ssh_hardening_pubkey_auth` | `true` | key authentication |
| `ssh_hardening_allow_users` | `[]` | `AllowUsers` (mandatory without passwords) |
| `ssh_hardening_service_action` | `reloaded` | `reloaded` or `restarted` |

## Recommendations

- Before enabling, make sure you can log in with a public key and test with
  `ansible-playbook site.yml --check`.
- If you change the SSH port, align `firewall_ssh_port` and reconnect with
  `-p <port>`.

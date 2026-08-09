# Role `firewall` (ufw) - STRICT OPT-IN

Firewall via **ufw** for Debian 13 / Ubuntu.

> **WARNING**: enabling a firewall can break SSH and networking.
> For this reason the role is strictly opt-in: by default it does nothing.

## Enabling (requires TWO variables)

```yaml
firewall_enabled: true
firewall_confirm: true
```

The preflight checks refuse to proceed if:
- `firewall_enabled=true` without `firewall_confirm=true`;
- there is no SSH path via trusted CIDR, Tailscale, global access, or an
  explicit extra port.

## Main variables

| Variable | Default | Notes |
|---|---|---|
| `firewall_enabled` | `false` | main **opt-in** |
| `firewall_confirm` | `false` | explicit confirmation (required) |
| `firewall_allow_ssh_from_anywhere` | `false` | allow SSH globally |
| `firewall_ssh_port` | `22` | SSH port to allow |
| `firewall_ssh_sources` | `[]` | trusted source CIDRs |
| `firewall_allow_tailscale_ssh` | `false` | allow SSH on `tailscale0` |
| `firewall_allow_ports` | `[]` | extra ports, format `"port/proto"` |
| `firewall_default_incoming` | `deny` | default incoming policy |
| `firewall_default_outgoing` | `allow` | default outgoing policy |

## Recommendations

- Before enabling the firewall always run
  `ansible-playbook site.yml --check` and make sure you have out-of-band
  access (console, IPMI, Tailscale already active).
- Interaction with Docker: ports published by containers (`-p` flag) go
  through the iptables `DOCKER` chains and **bypass ufw** by default. See
  `roles/docker/README.md`.

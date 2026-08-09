# Role `tailscale`

Installs Tailscale from the official repository (`pkgs.tailscale.com`) after
verifying the key fingerprint. It does not join the node to the network.

Authentication is deliberately kept out of Ansible to avoid auth keys in
argv, logs, or shell history. After the playbook, run interactively:

```bash
sudo tailscale up
```

## Main variables

| Variable | Default | Notes |
|---|---|---|
| `tailscale_install` | `false` | opt-in: install package and service |

## Notes

- Configure routes, exit nodes, and Tailscale SSH only after verifying the
  base access and your network's policies.

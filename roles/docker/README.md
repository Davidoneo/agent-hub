# Role `docker`

Installs Docker Engine from the official repository
(`download.docker.com`), without relying on `curl | sh`: the GPG key is
downloaded via `get_url` and the apt repository is added with
`deb822_repository`. The key fingerprint is verified before trusting the
repository.

## Main variables

| Variable | Default | Notes |
|---|---|---|
| `docker_install` | `false` | opt-in: install Docker Engine |
| `docker_repo_channel` | `stable` | repository channel |
| `docker_packages` | docker-ce, cli, containerd, buildx, compose | packages installed |
| `docker_users` | `[]` | users to add to the `docker` group (**opt-in**) |
| `docker_daemon_config` | `{}` | dict -> `/etc/docker/daemon.json` (**opt-in**) |

## Security and notes

- The daemon listens on the Unix socket `/var/run/docker.sock` (safe default).
  Never expose the TCP socket without authentication.
- Adding a user to the `docker` group is equivalent to giving them root:
  leave `docker_users` empty if not needed.
- If you use the firewall (ufw), note that ports published by containers
  (`-p` flag) go through the iptables `DOCKER` chains and **bypass ufw** in
  default configurations. For real isolation consider `iptables: false` in
  `docker_daemon_config` or an nftables backend.

- If the host also loads a custom nftables file containing `flush ruleset`,
  load that ruleset before Docker starts. Reloading it while Docker is running
  removes Docker's NAT and masquerade chains; restart `docker.service`
  immediately afterward and verify container egress.
- An empty `docker_daemon_config` does not touch an existing
  `/etc/docker/daemon.json`: if one was generated in a previous run, manage
  it separately.

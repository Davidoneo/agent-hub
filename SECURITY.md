# Security

Do not open public issues with real addresses, inventory, access logs, or
credentials. For code-related reports, use a private GitHub security advisory.

Before opening a pull request, verify that the diff and history contain no
`.env`, real inventory, keys, tokens, passwords, hostnames, or personal
domains.

The playbook modifies systems with root privileges. Always use
`--check --diff` first, review dependencies, and keep an alternate console
open while changing SSH or the firewall. Review external repositories and
their signing keys periodically.

Agent Hub treats PROJECT sessions as hostile local processes. Tailscale
identity headers are trusted only on the private Unix socket under
`/run/agent-hub`. Its directory and socket permissions prevent the PROJECT and
SERVER accounts from reaching the backend directly. systemd creates the socket
at mode `0600` and passes its open file descriptor to Uvicorn, so Uvicorn never
widens those permissions; root-owned Tailscale Serve is the only HTTP proxy
allowed through that boundary.

Passwordless SERVER administration is disabled by default and requires two
explicit variables. Keep private inventory, per-host overrides, recovery
snapshots and service-specific deploy profiles in a separate private
operations repository. Do not add them here merely because they contain no
credential: network topology and service names are still sensitive metadata.

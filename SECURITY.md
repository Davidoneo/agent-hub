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

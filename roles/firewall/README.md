# Ruolo `firewall` (ufw) - STRICT OPT-IN

Firewall tramite **ufw** per Debian 13 / Ubuntu.

> **ATTENZIONE**: attivare un firewall può interrompere SSH e la rete.
> Per questo il ruolo è rigorosamente opt-in: di default non fa nulla.

## Attivazione (richiede DUE variabili)

```yaml
firewall_enabled: true
firewall_confirm: true
```

Le preflight si rifiutano di procedere se:
- `firewall_enabled=true` senza `firewall_confirm=true`;
- la porta SSH non è consentita (`firewall_allow_ssh=false` senza la porta
  in `firewall_allow_ports`).

## Variabili principali

| Variabile | Default | Note |
|---|---|---|
| `firewall_enabled` | `false` | **opt-in** principale |
| `firewall_confirm` | `false` | conferma esplicita (richiesta) |
| `firewall_allow_ssh` | `true` | consente la porta SSH |
| `firewall_ssh_port` | `22` | porta SSH da consentire |
| `firewall_allow_ports` | `[]` | porte extra, formato `"porta/proto"` |
| `firewall_default_incoming` | `deny` | policy di default in ingresso |
| `firewall_default_outgoing` | `allow` | policy di default in uscita |

## Raccomandazioni

- Prima di attivare il firewall esegui sempre
  `ansible-playbook site.yml --check` e verifica di avere un accesso fuori
  banda (console, IPMI, Tailscale già attivo).
- Interazione con Docker: le porte pubblicate dai container (flag `-p`)
  passano dalle chain `DOCKER` di iptables e **bypassano ufw** di default.
  Vedi `roles/docker/README.md`.

# Ruolo `tailscale`

Installa Tailscale dal repository ufficiale (`pkgs.tailscale.com`) e, solo
se richiesto esplicitamente, unisce il nodo alla rete.

> **ATTENZIONE**: `tailscale up` modifica il routing di rete del nodo e
> l'aggiunta alla rete. È **STRICT OPT-IN**: di default il ruolo prepara
> soltanto l'installazione.

## Attivazione di `tailscale up`

```bash
ansible-playbook site.yml --tags tailscale \
  -e tailscale_up=true \
  -e tailscale_confirm=true \
  -e tailscale_authkey="$(cat ~/.ssh/tailscale-authkey)"
```

Oppure usa un vault:
`-e tailscale_authkey="{{ vault_tailscale_authkey }}" -e @secrets.yml`.

Requisiti preflight: `tailscale_install=true`, `tailscale_confirm=true`,
`tailscale_authkey` non vuota. L'authkey è un **segreto**: il task usa
`no_log=true` e non deve MAI comparire nel repository o nei file versionati.

## Variabili principali

| Variabile | Default | Note |
|---|---|---|
| `tailscale_install` | `true` | installa il pacchetto |
| `tailscale_up` | `false` | **opt-in** unione alla rete |
| `tailscale_confirm` | `false` | conferma esplicita |
| `tailscale_authkey` | `""` | **segreto**, passato a runtime |
| `tailscale_hostname` | `""` | nome nodo (vuoto = default) |
| `tailscale_ssh` | `false` | abilita SSH via tailscale |
| `tailscale_accept_routes` | `false` | accetta rotte annunciate |
| `tailscale_advertise_routes` | `[]` | subnet router |
| `tailscale_exit_node` | `false` | usa un exit node |

## Note

- `tailscale up` viene rieseguito a ogni run con `changed_when: true`:
  con lo stesso authkey il nodo viene riautenticato senza duplicarsi.
- Le opzioni di rete avanzate (`advertise_routes`, exit node) richiedono
  sempre `tailscale_confirm=true` e possono richiedere privilegi di
  approvazione nella admin console Tailscale.

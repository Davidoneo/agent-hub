# Ruolo `tailscale`

Installa Tailscale dal repository ufficiale (`pkgs.tailscale.com`) dopo avere
verificato il fingerprint della chiave. Non unisce il nodo alla rete.

L'autenticazione resta deliberatamente fuori da Ansible per evitare auth key
in argv, log o shell history. Dopo il playbook eseguire interattivamente:

```bash
sudo tailscale up
```

## Variabili principali

| Variabile | Default | Note |
|---|---|---|
| `tailscale_install` | `false` | opt-in: installa pacchetto e servizio |

## Note

- Configurare rotte, exit node e Tailscale SSH soltanto dopo aver verificato
  l'accesso base e le policy della propria rete.

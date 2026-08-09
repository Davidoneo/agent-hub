# Debian Machine Blueprint

Baseline Ansible prudente e forkabile per preparare un **laptop** o un
**server** Debian 13+ / Ubuntu 24.04+. Può essere eseguita localmente oppure da
un computer di controllo via SSH.

Il repository non contiene dati del server da cui è nato. Il ruolo `common`
applica la baseline scelta; Docker, OpenSSH server, chiavi fidate, hardening,
UFW e Tailscale sono tutti opt-in.

## Avvio rapido

```bash
git clone https://github.com/Davidoneo/debian-server-blueprint.git
cd debian-server-blueprint
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ansible-galaxy collection install -r requirements.yml
cp inventory/group_vars/all.example.yml inventory/group_vars/all.yml
```

Per configurare lo stesso laptop:

```bash
cp inventory/local.example inventory/hosts
ansible-playbook -i inventory/hosts site.yml --check --diff -K
ansible-playbook -i inventory/hosts site.yml -K
```

Per un host remoto:

```bash
cp inventory/hosts.example inventory/hosts
$EDITOR inventory/hosts
ansible-playbook -i inventory/hosts site.yml --check --diff
ansible-playbook -i inventory/hosts site.yml
```

Personalizzare prima `inventory/group_vars/all.yml`, incluso
`blueprint_profile: laptop` oppure `server`. Inventory e variabili reali sono
ignorati da Git.

## Accesso remoto da macchine fidate

Il flusso sicuro è deliberatamente a due fasi:

1. generare una chiave ed25519 distinta su ogni client fidato;
2. abilitare `ssh_access` e installare soltanto le chiavi pubbliche;
3. provare il nuovo login da un secondo terminale;
4. soltanto dopo abilitare hardening e firewall;
5. limitare UFW ai CIDR fidati oppure all'interfaccia Tailscale.

Il playbook rifiuta di disabilitare le password se l'utente amministrativo non
esiste, non è incluso in `AllowUsers` o non possiede un `authorized_keys` non
vuoto. Rifiuta anche porte SSH non allineate al firewall.

## Guide

- [Installazione locale e remota](docs/INSTALL.md)
- [Bootstrap SSH, client fidati e recovery](docs/SSH.md)
- [Policy di sicurezza](SECURITY.md)
- Documentazione specifica sotto `roles/*/README.md`

## Moduli

| Tag | Funzione | Default |
|---|---|---|
| `common` | pacchetti, timezone, locale, hostname | attivo |
| `ssh_access` | OpenSSH server e chiavi pubbliche fidate | disattivo |
| `docker` | Docker Engine dal repository ufficiale | disattivo |
| `tailscale` | installazione verificata; autenticazione manuale | disattivo |
| `ssh_hardening` | drop-in OpenSSH validato | disattivo + conferma |
| `firewall` | UFW applicato per ultimo | disattivo + conferma |

Eseguire sempre prima `--check --diff` e mantenere una console o una sessione
SSH alternativa durante modifiche a rete e accesso remoto.

## Altre distribuzioni

Le altre distro richiedono una fork: adattare package manager, nomi dei
pacchetti, servizi e percorsi, quindi aggiungere test CI specifici. Il
playbook principale blocca intenzionalmente release fuori dal supporto
dichiarato.

## Licenza

MIT. Revisionare codice, dipendenze e diff prima di eseguirli con privilegi
root; il progetto è una base personalizzabile, non una garanzia universale.

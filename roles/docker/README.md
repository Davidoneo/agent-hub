# Ruolo `docker`

Installa Docker Engine dal repository ufficiale (`download.docker.com`),
senza ricorrere a `curl | sh`: la chiave GPG viene scaricata via `get_url`
e il repository apt viene aggiunto con `deb822_repository`. Il fingerprint
della chiave viene verificato prima di fidarsi del repository.

## Variabili principali

| Variabile | Default | Note |
|---|---|---|
| `docker_install` | `false` | opt-in: installa Docker Engine |
| `docker_repo_channel` | `stable` | canale del repository |
| `docker_packages` | docker-ce, cli, containerd, buildx, compose | pacchetti installati |
| `docker_users` | `[]` | utenti da aggiungere al gruppo `docker` (**opt-in**) |
| `docker_daemon_config` | `{}` | dict -> `/etc/docker/daemon.json` (**opt-in**) |

## Sicurezza e note

- Il demone ascolta sul socket Unix `/var/run/docker.sock` (default sicuro).
  Non esporre mai il socket TCP senza autenticazione.
- Aggiungere un utente al gruppo `docker` equivale a dargli root: lascia
  `docker_users` vuoto se non serve.
- Se usi il firewall (ufw), tieni presente che le porte pubblicate dai
  container (flag `-p`) passano dalle chain iptables `DOCKER` e **bypassano
  ufw** nelle configurazioni predefinite. Per un isolamento reale valuta
  `iptables: false` nel `docker_daemon_config` oppure backend nftables.
- `docker_daemon_config` vuoto non tocca `/etc/docker/daemon.json` esistente:
  se in un run precedente era stato generato, gestiscilo a parte.

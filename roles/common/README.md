# Ruolo `common`

Baseline di base per server Debian 13 / Ubuntu:

- preflight: verifica che la distro target sia supportata (Debian/Ubuntu);
- pacchetti di base (lista in `common_packages`);
- fuso orario (`common_timezone`);
- locale di sistema (`common_locale`);
- hostname opzionale (`common_hostname`, vuoto = non toccato);
- **opt-in**: aggiornamenti di sicurezza automatici con unattended-upgrades.

## Variabili principali

| Variabile | Default | Note |
|---|---|---|
| `common_packages` | elenco base | pacchetti da installare |
| `common_apt_update_cache` | `true` | aggiorna cache apt |
| `common_apt_autoremove` | `false` | rimuove pacchetti orfani |
| `common_timezone` | `Etc/UTC` | fuso orario |
| `common_locale` | `C.UTF-8` | locale di sistema |
| `common_hostname` | `""` | vuoto = nessuna modifica |
| `common_unattended_upgrades` | `false` | **opt-in** aggiornamenti automatici |
| `common_unattended_upgrades_reboot` | `false` | reboot automatico dopo gli aggiornamenti |

## Sicurezza

`common_unattended_upgrades` è opt-in: di default non viene installato
nulla di automatico. Attivandolo, gli aggiornamenti di sicurezza vengono
applicati da unattended-upgrades; il reboot automatico resta disattivo
salvo esplicita richiesta.

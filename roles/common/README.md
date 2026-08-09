# Ruolo `common`

Baseline di base per laptop o server Debian 13 / Ubuntu 24.04+:

- preflight: verifica che la distro target sia supportata (Debian/Ubuntu);
- pacchetti di base (lista in `common_packages`);
- fuso orario opzionale (`common_manage_timezone`);
- locale di sistema opzionale (`common_manage_locale`);
- hostname opzionale (`common_hostname`, vuoto = non toccato);
- **opt-in**: aggiornamenti di sicurezza automatici con unattended-upgrades.

## Variabili principali

| Variabile | Default | Note |
|---|---|---|
| `common_packages` | elenco base | pacchetti da installare |
| `common_apt_update_cache` | `true` | aggiorna cache apt |
| `common_apt_autoremove` | `false` | rimuove pacchetti orfani |
| `common_manage_timezone` | `false` | abilita la modifica del fuso orario |
| `common_timezone` | `Etc/UTC` | fuso orario |
| `common_manage_locale` | `false` | abilita la generazione del locale |
| `common_locale` | `C.UTF-8` | locale di sistema |
| `common_hostname` | `""` | vuoto = nessuna modifica |
| `common_unattended_upgrades` | `false` | **opt-in** aggiornamenti automatici |
| `common_unattended_upgrades_reboot` | `false` | reboot automatico dopo gli aggiornamenti |

## Sicurezza

Fuso orario, locale e `common_unattended_upgrades` sono opt-in: il profilo
predefinito non sostituisce le preferenze locali di un laptop. Attivando gli
aggiornamenti automatici, il reboot resta disattivo salvo esplicita richiesta.

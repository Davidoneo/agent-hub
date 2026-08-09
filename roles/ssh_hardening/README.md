# Ruolo `ssh_hardening` - STRICT OPT-IN

Hardening di OpenSSH tramite drop-in in `/etc/ssh/sshd_config.d/`.

> **ATTENZIONE**: una configurazione SSH errata può chiuderti fuori dalla
> macchina. Il ruolo è rigorosamente opt-in: di default non tocca nulla.

## Attivazione (richiede DUE variabili)

```yaml
ssh_hardening_enabled: true
ssh_hardening_confirm: true
ssh_hardening_admin_user: tuo_utente
ssh_hardening_allow_users: [tuo_utente]   # richiesto se password_auth=false
```

Le preflight si rifiutano di procedere se:
- `ssh_hardening_enabled=true` senza `ssh_hardening_confirm=true`;
- `ssh_hardening_password_auth=false` con `ssh_hardening_allow_users` vuota
  (protezione contro il lock-out);
- l'utente amministrativo non esiste o non ha un `authorized_keys` non vuoto;
- la porta SSH scelta non è consentita dal ruolo `firewall` (se attivo).

## Come funziona la sicurezza

- **Drop-in con precedenza**: su Debian/Ubuntu `/etc/ssh/sshd_config` inizia
  con `Include /etc/ssh/sshd_config.d/*.conf`; il primo valore letto vince,
  quindi il drop-in prevale sul file principale senza toccarlo.
- **Validazione**: il template usa `validate: sshd -t -f %s` e, dopo
  l'applicazione, viene eseguito `sshd -t` sulla configurazione completa
  prima di qualsiasi ricarica.
- **Ricarica non distruttiva**: l'handler usa `state: reloaded` (HUP) di
  default: non interrompe le sessioni SSH attive. Imposta
  `ssh_hardening_service_action: restarted` solo se serve davvero.

## Variabili principali

| Variabile | Default | Note |
|---|---|---|
| `ssh_hardening_enabled` | `false` | **opt-in** principale |
| `ssh_hardening_confirm` | `false` | conferma esplicita |
| `ssh_hardening_admin_user` | `""` | utente con chiave già verificata |
| `ssh_hardening_port` | `22` | porta SSH |
| `ssh_hardening_permit_root_login` | `"no"` | login root |
| `ssh_hardening_password_auth` | `false` | autenticazione a password |
| `ssh_hardening_pubkey_auth` | `true` | autenticazione a chiave |
| `ssh_hardening_allow_users` | `[]` | `AllowUsers` (obbligatoria senza password) |
| `ssh_hardening_service_action` | `reloaded` | `reloaded` o `restarted` |

## Raccomandazioni

- Prima dell'attivazione assicurati di poter accedere con chiave pubblica e
  testa con `ansible-playbook site.yml --check`.
- Se cambi la porta SSH, allinea `firewall_ssh_port` e riavvia la connessione
  con `-p <porta>`.

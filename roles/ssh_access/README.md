# Ruolo `ssh_access` — opt-in

Installa opzionalmente OpenSSH server e aggiunge le chiavi pubbliche ed25519 o
FIDO delle macchine fidate a un utente **già esistente**. Non crea utenti, non
accetta chiavi private e non disabilita l'autenticazione a password.

Impostare `ssh_access_exclusive=true` soltanto dopo avere verificato tutte le
chiavi da un secondo terminale: rimuove da `authorized_keys` le chiavi non
elencate.

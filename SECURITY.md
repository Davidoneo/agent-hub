# Sicurezza

Non aprire issue pubbliche con indirizzi reali, inventory, log di accesso o
credenziali. Per segnalazioni relative al codice usare una security advisory
privata di GitHub.

Prima di una pull request verificare che diff e history non contengano `.env`,
inventory reale, chiavi, token, password, hostname o domini personali.

Il playbook modifica sistemi con privilegi root. Usare prima `--check --diff`,
revisionare le dipendenze e mantenere una console alternativa durante modifiche
a SSH o firewall. Le versioni scaricate da repository esterni vanno rivalutate
periodicamente; questo progetto non sostituisce gli aggiornamenti di sicurezza.

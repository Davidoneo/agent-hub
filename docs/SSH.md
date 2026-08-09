# Accesso SSH: chiavi, bootstrap e hardening

Procedura consigliata per usare il blueprint senza mai chiudersi fuori dalla
macchina. Tutti gli indirizzi, gli hostname e i commenti sono segnaposto
(`example.invalid`, rete TEST-NET `192.0.2.0/24`): sostituiscili con i tuoi,
ma non committare dati reali o chiavi private.

## 1. Genera una chiave ed25519 su ciascuna macchina fidata

Sul computer di controllo **e su ogni laptop/PC che deve accedere** al
server (mai condividere la chiave privata, e mai generare la chiave
direttamente sul server):

```bash
ssh-keygen -t ed25519 -a 100 -C "chiave-laptop-1@example.invalid"
```

Consigli:

- Accetta il percorso di default `~/.ssh/id_ed25519`; proteggila con una
  passphrase (opzionale ma raccomandata).
- Verifica i permessi: `chmod 600 ~/.ssh/id_ed25519`.
- La chiave **pubblica** è `~/.ssh/id_ed25519.pub` (una riga che inizia con
  `ssh-ed25519 AAAA...`). È l'unica cosa che deve uscire dalla macchina.

## 2. Inserisci le sole chiavi pubbliche in `ssh_access_public_keys`

In `inventory/group_vars/all.yml` (che non viene mai committato) definisci
l'utente amministrativo e la **lista delle sole chiavi pubbliche**:

```yaml
ssh_access_enabled: true
ssh_access_user: deploy
ssh_access_install_server: true
ssh_access_public_keys:
  - "ssh-ed25519 AAAA... chiave-laptop-1@example.invalid"
  - "ssh-ed25519 AAAA... chiave-laptop-2@example.invalid"
```

Mai mettere qui una chiave privata. Il primo bootstrap usa il ruolo dedicato:

```bash
ansible-playbook -i inventory/hosts site.yml --tags ssh_access --check --diff
ansible-playbook -i inventory/hosts site.yml --tags ssh_access
```

Il comando è ripetibile: aggiungere una chiave alla lista e rilanciare è
sicuro e idempotente.

## 3. Primo bootstrap senza disabilitare la password

L'hardening SSH è **disabilitato di default** (il login a password resta
attivo). Ecco perché il primo accesso è sicuro:

```bash
ssh deploy@server1.example.invalid
```

- Completa il passo 2 (installazione chiavi) **prima** di qualsiasi
  hardening.
- Verifica che l'autenticazione a chiave funzioni già in questa fase, mentre
  la password è ancora attiva come "rete di sicurezza".

## 4. Test da secondo terminale

Mai chiudere la sessione di lavoro prima di aver verificato tutto:

1. Lascia aperta la sessione del passo 3.
2. Da un **secondo terminale** testa il login a chiave:

   ```bash
   ssh -i ~/.ssh/id_ed25519 deploy@server1.example.invalid
   ```

3. Solo se il login a chiave funziona, procedi con l'hardening. Se qualcosa
   va storto, la prima sessione è ancora aperta per il rollback.

## 5. Poi hardening e firewall

Attiva l'hardening SSH solo ora, con la doppia conferma richiesta dalle
preflight e la lista `AllowUsers` compilata (obbligatoria quando le password
vengono disabilitate):

```yaml
ssh_hardening_enabled: true
ssh_hardening_confirm: true
ssh_hardening_admin_user: deploy
ssh_hardening_port: 22
ssh_hardening_password_auth: false
ssh_hardening_allow_users:
  - deploy
```

Applica per tag:

```bash
ansible-playbook -i inventory/hosts site.yml --tags ssh_hardening --check --diff
ansible-playbook -i inventory/hosts site.yml --tags ssh_hardening
```

Il ruolo genera un drop-in convalidato con `sshd -t` e ricarica senza
interrompere le sessioni attive (`state: reloaded`).

**Firewall con CIDR fidato** (rete TEST-NET di esempio — sostituisci con la
tua): consenti SSH soltanto dalla tua rete:

```yaml
firewall_enabled: true
firewall_confirm: true
firewall_allow_ssh_from_anywhere: false
firewall_ssh_port: 22
firewall_ssh_sources:
  - "192.0.2.0/24"
firewall_allow_tailscale_ssh: false
```

Applica il firewall per tag soltanto dopo avere verificato che l'indirizzo
sorgente del client appartenga davvero al CIDR indicato:

```bash
ansible-playbook -i inventory/hosts site.yml --tags firewall --check --diff
ansible-playbook -i inventory/hosts site.yml --tags firewall
```

**In alternativa: Tailscale** — se preferisci non aprire porte verso
Internet, usa la rete Tailscale (range riservato `100.64.0.0/10`):

1. Installa Tailscale e poi autentica interattivamente il nodo:
   ```bash
   ansible-playbook -i inventory/hosts site.yml --tags tailscale
   ssh deploy@server1.example.invalid
   sudo tailscale up
   ```
2. Imposta `firewall_allow_tailscale_ssh: true` e lascia disabilitato
   `firewall_allow_ssh_from_anywhere`; quindi applica UFW.
3. Connettiti tramite nome MagicDNS o indirizzo Tailscale. L'hardening SSH
   resta valido senza esporre globalmente la porta 22.

Allinea sempre la porta SSH (`ssh_hardening_port`/`firewall_ssh_port`) tra i
due ruoli: le preflight lo impongono.

## 6. Esempio `~/.ssh/config` lato client

Per comodità, sul tuo computer di controllo:

```
Host server1
    HostName server1.example.invalid
    User deploy
    Port 22
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

Poi basta `ssh server1`. Aggiungi `IdentitiesOnly yes` se hai più chiavi in
agente.

## 7. Recovery da lockout

Se perdi l'accesso SSH (configurazione errata, chiavi perse, firewall):

1. Usa il canale fuori banda: console fisica, IPMI, seriale o (se attivo)
   Tailscale.
2. Accedi come utente con sudo o come root dalla console.
3. Ripristina SSH: rimuovi il drop-in e ricarica il servizio:
   ```bash
   sudo mv /etc/ssh/sshd_config.d/99-baseline-hardening.conf \
     /etc/ssh/sshd_config.d/99-baseline-hardening.conf.disabled
   sudo systemctl reload ssh
   ```
4. Se il blocco è il firewall: `sudo ufw disable` (poi riapplica il
   blueprint con un CIDR corretto).
5. Verifica da un nuovo terminale con `ssh -v server1` prima di chiudere la
   console.

**Prevenzione** (il modo migliore): almeno due chiavi pubbliche valide in
`ssh_access_public_keys` (es. due laptop o una chiave hardware), test da secondo terminale
prima di ogni hardening, drop-in di backup del file di configurazione, e
sessioni di lavoro aperte durante le modifiche.

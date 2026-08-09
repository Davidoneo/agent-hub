# Installazione e primo utilizzo

Istruzioni pratiche per preparare con questo blueprint una macchina
**Debian 13** o **Ubuntu 24.04**, sia un server (gestito da remoto via SSH)
sia un laptop (eseguito in locale). I passi sono identici: cambia solo
l'inventario.

> Regola d'oro: prima sempre `--check --diff`, e per la prima applicazione
> reale tieni aperta una console alternativa (secondo terminale, console
> fisica/IPMI). Una configurazione errata di SSH o firewall può tagliare
> l'accesso remoto.

## 1. Prerequisiti

Sul computer di controllo (quello da cui lanci Ansible):

- Linux o macOS, `git` e un client SSH.
- Python 3.11+ e le versioni dichiarate dal progetto. Installazione in un
  ambiente isolato:

  ```bash
  python3 -m venv .venv
  . .venv/bin/activate
  python3 -m pip install -r requirements-dev.txt
  ```

Sull'host target:

- Debian 13 (trixie) o Ubuntu 24.04.
- Un utente amministrativo con `sudo` e, per l'uso remoto, accesso SSH.

## 2. Clone del repository

```bash
git clone https://github.com/Davidoneo/debian-server-blueprint.git
cd debian-server-blueprint
```

## 3. Dipendenze (collections)

Il playbook usa `community.general` e `ansible.posix`. Installa le versioni
dichiarate:

```bash
ansible-galaxy collection install -r requirements.yml
```

Per aggiornare/ricreare da zero: `ansible-galaxy collection install -r
requirements.yml --force`.

## 4. Copia di inventory e group_vars

I file di esempio sono sicuri e non contengono dati reali. Copiali e
personalizzali (i file reali sono in `.gitignore`, non finiscono in Git):

```bash
cp inventory/hosts.example inventory/hosts
cp inventory/group_vars/all.example.yml inventory/group_vars/all.yml
$EDITOR inventory/hosts
$EDITOR inventory/group_vars/all.yml
```

- `inventory/hosts`: elenca i tuoi host, es.
  `server1.example.invalid ansible_host=192.0.2.10 ansible_user=deploy`.
- `inventory/group_vars/all.yml`: tutte le variabili, già in versione
  "safe" (server SSH, chiavi, firewall, hardening e Tailscale sono opt-in).

## 5. Profilo laptop o server

Imposta prima `blueprint_profile: laptop` oppure `server` nel file delle
variabili.

**Server o laptop remoto**: aggiungi l'host a `[managed]` con `ansible_host` e
`ansible_user`, poi usa l'inventario reale:

```bash
ansible-playbook -i inventory/hosts site.yml --check --diff
```

**Laptop o server locale**: usa l'inventario locale fornito:

```bash
cp inventory/local.example inventory/hosts
ansible-playbook -i inventory/hosts site.yml --check --diff -K
```

Differenze pratiche da tenere presenti sul laptop:

- `sudo` potrebbe chiedere la password: usa `-K`/`--ask-become-pass`.
- Il ruolo `firewall` limita il traffico in ingresso: su un laptop lascialo
  disabilitato se non devi esporre servizi, oppure autorizza solo reti fidate.
- `common_hostname`, timezone e unattended-upgrades valgono per entrambi i
  profili; per un laptop valuta `common_unattended_upgrades_reboot: false`.

## 6. Check mode (prova a secco)

Prima di ogni modifica reale, e comunque sempre alla prima run:

```bash
ansible-playbook -i inventory/hosts site.yml --check --diff
```

Nessuna modifica viene applicata; `--diff` mostra cosa cambierebbe.
Controlla che le preflight non falliscano e che i "changed" siano quelli
attesi.

## 7. Applicazione per tag

Il playbook applica di default solo il ruolo `common` (baseline sicura);
Docker, firewall, hardening SSH e Tailscale sono opt-in espliciti. Per
applicare un solo modulo:

```bash
ansible-playbook -i inventory/hosts site.yml --tags common
```

Tag disponibili:

| Tag | Ruolo | Effetto |
|---|---|---|
| `common` | common | pacchetti base, timezone, locale, hostname, unattended-upgrades (opt-in) |
| `ssh_access` | ssh_access | OpenSSH server e chiavi pubbliche fidate (opt-in) |
| `docker` | docker | Docker Engine dal repo ufficiale + gruppo `docker` (opt-in) |
| `ssh_hardening` | ssh_hardening | drop-in di hardening SSH (opt-in doppio, vedi `docs/SSH.md`) |
| `firewall` | firewall | ufw con policy deny (opt-in doppio) |
| `tailscale` | tailscale | installazione verificata; autenticazione manuale |
| `baseline` | tutti | tutti i ruoli, con i loro opt-in |

Puoi limitare l'esecuzione a un host con `--limit server1.example.invalid`.
Dopo un intervento parziale è possibile rieseguire lo stesso comando: il
playbook è idempotente.

## 8. Rollback e sicurezza manuale

Il blueprint è pensato per non lasciare la macchina bloccata:

- **Nessun file di sistema sovrascritto a caso**: l'hardening SSH genera un
  *drop-in* in `/etc/ssh/sshd_config.d/` (il file principale non viene
  toccato); il firewall usa ufw; Docker e Tailscale installano pacchetti
  standard.
- **Doppia conferma obbligatoria**: firewall e hardening SSH richiedono
  `*_enabled=true` **e** `*_confirm=true`; le altre funzioni restano opt-in.
- **Rollback manuale** (con accesso alla macchina):
  - SSH: rimuovi `/etc/ssh/sshd_config.d/99-baseline-hardening.conf` e fai
    `systemctl reload ssh`.
  - Firewall: `sudo ufw disable`.
  - Tailscale: `sudo tailscale down` se lo avevi autenticato manualmente.
- Mantieni sempre un canale di accesso fuori banda e una seconda sessione
  aperta mentre applichi modifiche a SSH/firewall.
- Non committare mai inventory reale, `.env`, chiavi o token: usa Ansible
  Vault o un secret manager con chiave di decifratura fuori dal repository.

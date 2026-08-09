# Debian Server Blueprint

Baseline Ansible prudente e forkabile per preparare un server personale. Il
target principale e Debian 13; Ubuntu recente e supportato quando usa gli
stessi pacchetti. Le altre distribuzioni richiedono l'adattamento dei ruoli.

Il playbook non contiene dati del server originale. Per default applica solo
il ruolo `common`; Docker, Tailscale, SSH e firewall richiedono opt-in.

## Avvio rapido

Sul computer di controllo servono Python 3, Ansible Core 2.16+ e accesso SSH
con `sudo` al server.

```bash
git clone https://github.com/Davidoneo/debian-server-blueprint.git
cd debian-server-blueprint
cp inventory/hosts.example inventory/hosts
cp inventory/group_vars/all.example.yml inventory/group_vars/all.yml
$EDITOR inventory/hosts
$EDITOR inventory/group_vars/all.yml
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventory/hosts site.yml --check --diff
ansible-playbook -i inventory/hosts site.yml
```

Prima eseguire sempre `--check --diff`. Il primo passaggio reale va fatto con
una console alternativa disponibile: una configurazione SSH o firewall errata
puo interrompere l'accesso remoto.

## Moduli

- `common`: pacchetti di base, timezone e aggiornamenti automatici.
- `docker`: Docker dal repository ufficiale e gruppo `docker` opt-in.
- `ssh_hardening`: drop-in convalidato con `sshd -t`.
- `firewall`: UFW, rigorosamente opt-in con conferma distinta.
- `tailscale`: repository ufficiale e join della rete rigorosamente opt-in.

I valori e le doppie conferme sono documentati in
[`inventory/group_vars/all.example.yml`](inventory/group_vars/all.example.yml).

È possibile eseguire un solo modulo:

```bash
ansible-playbook -i inventory/hosts site.yml --tags common
```

## Fork per un'altra distribuzione

1. Creare un branch dalla propria fork.
2. Aggiungere variabili specifiche in `vars/<famiglia>.yml` o separare i task
   con `include_tasks` in base ad `ansible_os_family`.
3. Sostituire nomi dei pacchetti, servizi e percorsi senza rimuovere gli assert.
4. Aggiungere la distribuzione alla matrice CI solo dopo un test reale.

Il preflight accetta intenzionalmente soltanto Debian 13+ e Ubuntu 24.04+.

## Segreti

Non committare inventory reale, `.env`, chiavi o token. Usare Ansible Vault,
SOPS o un secret manager e conservare la chiave di decifratura fuori dal repo.
Le auth key Tailscale vanno passate e usate fuori da questo playbook.
Consultare anche [`SECURITY.md`](SECURITY.md) prima di pubblicare una fork.

## Licenza

MIT. Il software e fornito senza garanzie: revisionare sempre le modifiche
prima di applicarle a una macchina raggiungibile soltanto da remoto.

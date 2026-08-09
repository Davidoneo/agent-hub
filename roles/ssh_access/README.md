# Role `ssh_access` — opt-in

Optionally installs OpenSSH server and adds the ed25519 or FIDO public keys
of trusted machines to an **already existing** user. It does not create
users, does not accept private keys, and does not disable password
authentication.

Set `ssh_access_exclusive=true` only after verifying all keys from a second
terminal: it removes from `authorized_keys` any key not listed.

# Role `common`

Base system setup for Debian 13 / Ubuntu 24.04+ laptops and servers:

- preflight: verifies the target distro is supported (Debian/Ubuntu);
- base packages (list in `common_packages`);
- optional timezone (`common_manage_timezone`);
- optional system locale (`common_manage_locale`);
- optional hostname (`common_hostname`, empty = untouched);
- **opt-in**: automatic security updates with unattended-upgrades.

## Main variables

| Variable | Default | Notes |
|---|---|---|
| `common_packages` | base list | packages to install |
| `common_apt_update_cache` | `true` | refresh apt cache |
| `common_apt_autoremove` | `false` | remove orphan packages |
| `common_manage_timezone` | `false` | enable timezone change |
| `common_timezone` | `Etc/UTC` | timezone |
| `common_manage_locale` | `false` | enable locale generation |
| `common_locale` | `C.UTF-8` | system locale |
| `common_hostname` | `""` | empty = no change |
| `common_unattended_upgrades` | `false` | **opt-in** automatic updates |
| `common_unattended_upgrades_reboot` | `false` | automatic reboot after updates |

## Security

Timezone, locale, and `common_unattended_upgrades` are opt-in: the default
profile does not override a laptop's local preferences. When enabling
automatic updates, reboot stays off unless explicitly requested.

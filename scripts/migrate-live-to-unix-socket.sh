#!/usr/bin/env bash
# One-time, rollback-capable migration of a default Agent Hub installation
# from the retired TCP+nftables boundary to a systemd-owned Unix socket.
set -Eeuo pipefail

SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
INSTALL_ROOT=${AGENT_HUB_INSTALL_ROOT:-/opt/agent-hub}
LIBEXEC_ROOT=${AGENT_HUB_LIBEXEC_ROOT:-/usr/local/libexec/agent-hub}
CONFIG_FILE=${AGENT_HUB_CONFIG_FILE:-/etc/agent-hub/config.env}
UNIT_FILE=/etc/systemd/system/agent-hub.service
SOCKET_UNIT_FILE=/etc/systemd/system/agent-hub.socket
TMPFILES_FILE=/etc/tmpfiles.d/agent-hub.conf
GUARD_UNIT_FILE=/etc/systemd/system/agent-hub-loopback-guard.service
GUARD_DROPIN_FILE=/etc/systemd/system/agent-hub.service.d/20-loopback-guard.conf
GUARD_CTL_FILE=${LIBEXEC_ROOT}/loopback-guard-ctl
SOCKET_PATH=/run/agent-hub/agent-hub.sock

die() {
  echo "migrate-live-to-unix-socket: $*" >&2
  return 1
}

config_value() {
  local key=$1
  awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "$CONFIG_FILE"
}

[[ ${EUID} -eq 0 ]] || die "eseguire come root (sudo $0)"

for command in awk cp curl date grep id install nft ps python3 runuser ss stat systemctl systemd-tmpfiles tailscale; do
  command -v "$command" >/dev/null || die "comando richiesto non trovato: $command"
done

declare -a SOURCE_FILES=(
  "$SOURCE_ROOT/app/main.py"
  "$SOURCE_ROOT/app/static/app.js"
  "$SOURCE_ROOT/libexec/health-ctl"
  "$SOURCE_ROOT/libexec/session-ctl"
)
for path in "${SOURCE_FILES[@]}" "$CONFIG_FILE" "$UNIT_FILE"; do
  [[ -f "$path" ]] || die "file richiesto non trovato: $path"
done

SERVICE_USER=$(config_value AGENT_HUB_SERVICE_USER)
PROJECT_USER=$(config_value AGENT_HUB_PROJECT_USER)
SERVER_USER=$(config_value AGENT_HUB_SERVER_USER)
ORIGIN=$(config_value AGENT_HUB_ORIGIN)
SERVICE_USER=${SERVICE_USER:-agenthub}
PROJECT_USER=${PROJECT_USER:-devagent}
SERVER_USER=${SERVER_USER:-hostagent}
SERVICE_GROUP=$(id -gn "$SERVICE_USER")
AGENT_GROUP=$(awk -F= '$1 == "SupplementaryGroups" { print $2; exit }' "$UNIT_FILE")
AGENT_GROUP=${AGENT_GROUP:-agentprojects}
[[ "$ORIGIN" == https://* ]] || die "AGENT_HUB_ORIGIN deve essere HTTPS"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=/var/backups/agent-hub/${timestamp}-unix-socket-migration
ROOTFS_BACKUP=$BACKUP_DIR/rootfs
install -d -o root -g root -m 0700 "$BACKUP_DIR" "$ROOTFS_BACKUP"

declare -a MANAGED_PATHS=(
  "$INSTALL_ROOT/app/main.py"
  "$INSTALL_ROOT/app/static/app.js"
  "$LIBEXEC_ROOT/health-ctl"
  "$LIBEXEC_ROOT/session-ctl"
  "$CONFIG_FILE"
  "$UNIT_FILE"
  "$SOCKET_UNIT_FILE"
  "$TMPFILES_FILE"
  "$GUARD_UNIT_FILE"
  "$GUARD_DROPIN_FILE"
  "$GUARD_CTL_FILE"
)

backup_path() {
  local path=$1
  if [[ -e "$path" || -L "$path" ]]; then
    install -d -o root -g root -m 0700 "$ROOTFS_BACKUP$(dirname -- "$path")"
    cp -a -- "$path" "$ROOTFS_BACKUP$path"
  fi
}

restore_path() {
  local path=$1
  if [[ -e "$ROOTFS_BACKUP$path" || -L "$ROOTFS_BACKUP$path" ]]; then
    install -d "$(dirname -- "$path")"
    cp -a -- "$ROOTFS_BACKUP$path" "$path"
  else
    rm -f -- "$path"
  fi
}

for path in "${MANAGED_PATHS[@]}"; do
  backup_path "$path"
done

tailscale serve get-config "$BACKUP_DIR/tailscale-serve.hujson" --all
tailscale serve status --json >"$BACKUP_DIR/tailscale-serve-before.json"
if systemctl is-enabled --quiet agent-hub-loopback-guard.service 2>/dev/null; then
  echo enabled >"$BACKUP_DIR/guard-state"
else
  echo disabled >"$BACKUP_DIR/guard-state"
fi

CONFIG_NEW=$BACKUP_DIR/config.env.new
awk -v socket="$SOCKET_PATH" '
  BEGIN { emitted = 0 }
  /^AGENT_HUB_(BIND|PORT|LOOPBACK_GUARD_ENABLED)=/ {
    if (!emitted) { print "AGENT_HUB_SOCKET=" socket; emitted = 1 }
    next
  }
  /^AGENT_HUB_SOCKET=/ {
    if (!emitted) { print "AGENT_HUB_SOCKET=" socket; emitted = 1 }
    next
  }
  { print }
  END { if (!emitted) print "AGENT_HUB_SOCKET=" socket }
' "$CONFIG_FILE" >"$CONFIG_NEW"

UNIT_NEW=$BACKUP_DIR/agent-hub.service.new
cat >"$UNIT_NEW" <<UNIT
# Managed by migrate-live-to-unix-socket.sh; source of truth: agent_hub role
[Unit]
Description=Agent Hub - persistent agent sessions behind a private web UI
Documentation=file://${INSTALL_ROOT}/README.md
Requires=agent-hub.socket
After=network-online.target agent-hub.socket tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
SupplementaryGroups=${AGENT_GROUP}
WorkingDirectory=${INSTALL_ROOT}/app
EnvironmentFile=${CONFIG_FILE}
ExecStart=${INSTALL_ROOT}/.venv/bin/uvicorn main:app \\
  --fd 3 \\
  --no-server-header --proxy-headers --forwarded-allow-ips '*'
ExecStartPost=${LIBEXEC_ROOT}/health-ctl --backend-only
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
UMask=0077
LimitNOFILE=8192:524288
StandardOutput=journal
StandardError=journal
SyslogIdentifier=agent-hub
KillMode=control-group

[Install]
WantedBy=multi-user.target
UNIT

SOCKET_UNIT_NEW=$BACKUP_DIR/agent-hub.socket.new
cat >"$SOCKET_UNIT_NEW" <<UNIT
# Managed by migrate-live-to-unix-socket.sh; source of truth: agent_hub role
[Unit]
Description=Agent Hub - private systemd-owned Unix socket
Documentation=file://${INSTALL_ROOT}/README.md
Before=agent-hub.service
After=systemd-tmpfiles-setup.service
Requires=systemd-tmpfiles-setup.service

[Socket]
ListenStream=${SOCKET_PATH}
SocketUser=${SERVICE_USER}
SocketGroup=${SERVICE_GROUP}
SocketMode=0600
DirectoryMode=0750
RemoveOnStop=true
Service=agent-hub.service

[Install]
WantedBy=sockets.target
UNIT

TMPFILES_NEW=$BACKUP_DIR/agent-hub.conf.new
cat >"$TMPFILES_NEW" <<EOF
# Managed by the Agent Hub role.
d $(dirname -- "$SOCKET_PATH") 0750 ${SERVICE_USER} ${SERVICE_GROUP} -
EOF

rollback() {
  local original_rc=${1:-1}
  trap - ERR
  set +e
  echo "Migrazione fallita: ripristino dal backup $BACKUP_DIR" >&2
  systemctl stop agent-hub.service agent-hub.socket
  systemctl disable agent-hub.socket
  for path in "${MANAGED_PATHS[@]}"; do
    restore_path "$path"
  done
  systemctl daemon-reload
  if [[ $(<"$BACKUP_DIR/guard-state") == enabled ]]; then
    systemctl enable --now agent-hub-loopback-guard.service
  fi
  systemctl enable agent-hub.service
  systemctl restart agent-hub.service
  tailscale serve set-config "$BACKUP_DIR/tailscale-serve.hujson" --all
  echo "Rollback completato; backup conservato in $BACKUP_DIR" >&2
  exit "$original_rc"
}
trap 'rollback $?' ERR

install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
  "$SOURCE_ROOT/app/main.py" "$INSTALL_ROOT/app/main.py"
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
  "$SOURCE_ROOT/app/static/app.js" "$INSTALL_ROOT/app/static/app.js"
install -o root -g root -m 0755 "$SOURCE_ROOT/libexec/health-ctl" "$LIBEXEC_ROOT/health-ctl"
install -o root -g root -m 0755 "$SOURCE_ROOT/libexec/session-ctl" "$LIBEXEC_ROOT/session-ctl"
install -o root -g root -m 0644 "$CONFIG_NEW" "$CONFIG_FILE"
install -o root -g root -m 0644 "$UNIT_NEW" "$UNIT_FILE"
install -o root -g root -m 0644 "$SOCKET_UNIT_NEW" "$SOCKET_UNIT_FILE"
install -o root -g root -m 0644 "$TMPFILES_NEW" "$TMPFILES_FILE"

# This stops only the web process. The actual agent sessions are owned by the
# users' persistent tmux managers and deliberately survive this restart.
systemctl stop agent-hub.service
systemctl disable --now agent-hub-loopback-guard.service 2>/dev/null || true
nft delete table inet agent_hub_guard 2>/dev/null || true
rm -f -- "$GUARD_UNIT_FILE" "$GUARD_DROPIN_FILE" "$GUARD_CTL_FILE"
systemd-tmpfiles --create "$TMPFILES_FILE"
systemctl daemon-reload
systemctl enable --now agent-hub.socket
systemctl enable agent-hub.service
systemctl restart agent-hub.service
systemctl is-active --quiet agent-hub.socket
systemctl is-active --quiet agent-hub.service

[[ $(stat -c '%U:%G:%a' "$SOCKET_PATH") == "${SERVICE_USER}:${SERVICE_GROUP}:600" ]] ||
  die "proprieta' o mode inattesi sul socket: $(stat -c '%U:%G:%a' "$SOCKET_PATH")"
[[ $(stat -c '%U:%G:%a' "$(dirname -- "$SOCKET_PATH")") == "${SERVICE_USER}:${SERVICE_GROUP}:750" ]] ||
  die "proprieta' o mode inattesi sulla directory del socket"
curl --silent --show-error --max-time 5 --unix-socket "$SOCKET_PATH" \
  --fail http://localhost/static/index.html >/dev/null

for local_user in "$PROJECT_USER" "$SERVER_USER"; do
  [[ "$local_user" == "$SERVICE_USER" ]] && continue
  if runuser -u "$local_user" -- curl --silent --show-error --max-time 2 \
       --unix-socket "$SOCKET_PATH" http://localhost/static/index.html >/dev/null 2>&1; then
    die "l'account locale $local_user riesce a bypassare il proxy"
  fi
done

if ss -ltnH 'sport = :8787' | grep -q .; then
  die "il vecchio listener TCP 127.0.0.1:8787 e' ancora attivo"
fi

tailscale serve --bg --yes --set-path / "unix:${SOCKET_PATH}"
tailscale serve status --json >"$BACKUP_DIR/tailscale-serve-after.json"

python3 - "$BACKUP_DIR/tailscale-serve-before.json" \
  "$BACKUP_DIR/tailscale-serve-after.json" "unix:${SOCKET_PATH}" <<'PY'
import copy
import json
import sys

before = json.load(open(sys.argv[1], encoding="utf-8"))
after = json.load(open(sys.argv[2], encoding="utf-8"))
expected = sys.argv[3]

def handlers(document):
    return {host: copy.deepcopy(spec.get("Handlers", {}))
            for host, spec in document.get("Web", {}).items()}

old_handlers = handlers(before)
new_handlers = handlers(after)
old_non_root = {host: {path: value for path, value in paths.items() if path != "/"}
                for host, paths in old_handlers.items()}
new_non_root = {host: {path: value for path, value in paths.items() if path != "/"}
                for host, paths in new_handlers.items()}
if old_non_root != new_non_root:
    raise SystemExit("la migrazione ha modificato handler Tailscale diversi da /")
root_targets = [paths.get("/", {}).get("Proxy") for paths in new_handlers.values()]
if expected not in root_targets:
    raise SystemExit(f"handler root inatteso: {root_targets!r}")
PY

mapfile -t ORIGIN_PARTS < <(python3 - "$ORIGIN" <<'PY'
import sys
from urllib.parse import urlsplit

parsed = urlsplit(sys.argv[1])
if parsed.scheme != "https" or not parsed.hostname:
    raise SystemExit(1)
print(parsed.hostname)
print(parsed.port or 443)
PY
)
ORIGIN_HOST=${ORIGIN_PARTS[0]}
ORIGIN_PORT=${ORIGIN_PARTS[1]}
TAILSCALE_IP=$(tailscale ip -4 | awk 'NF { print; exit }')
[[ -n "$TAILSCALE_IP" ]] || die "IP Tailscale non disponibile"

public_code=000
for _attempt in {1..20}; do
  public_code=$(curl --silent --show-error --max-time 5 \
    --resolve "${ORIGIN_HOST}:${ORIGIN_PORT}:${TAILSCALE_IP}" \
    --output /dev/null --write-out '%{http_code}' "${ORIGIN%/}/" 2>/dev/null || true)
  [[ "$public_code" == 200 ]] && break
  sleep 0.25
done
[[ "$public_code" == 200 ]] || die "probe HTTPS end-to-end fallito: HTTP $public_code"

MAIN_PID=$(systemctl show agent-hub.service -p MainPID --value)
[[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]] || die "PID Agent Hub non valido"
if ps -o stat= --ppid "$MAIN_PID" | grep -q '^Z'; then
  die "il nuovo backend ha gia' figli zombie"
fi
SOFT_NOFILE=$(awk '$1 == "Max" && $2 == "open" && $3 == "files" { print $4 }' "/proc/$MAIN_PID/limits")
[[ "$SOFT_NOFILE" -ge 8192 ]] || die "limite file descriptor inatteso: $SOFT_NOFILE"

set +e
"$LIBEXEC_ROOT/health-ctl" --json --save >"$BACKUP_DIR/post-health.json"
HEALTH_RC=$?
set -e
[[ $HEALTH_RC -ne 3 ]] || die "il controllo completo di salute ha avuto un errore interno"
python3 - "$BACKUP_DIR/post-health.json" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
required = {
    "agent-hub", "agent-hub.socket", "backend_resources", "tailscaled",
    "tailscale_serve", "socket_boundary", "backend_http", "agent_hub_end_to_end",
}
checks = {item["name"]: item for item in result.get("checks", [])}
bad = [f"{name}: {checks.get(name, {}).get('detail', 'controllo assente')}"
       for name in sorted(required)
       if checks.get(name, {}).get("status") != "OK"]
if bad:
    raise SystemExit("controlli Agent Hub non superati: " + "; ".join(bad))
PY

trap - ERR
echo "Migrazione completata: Agent Hub risponde HTTPS 200 tramite ${SOCKET_PATH}."
echo "Backup e diagnostica: ${BACKUP_DIR}"

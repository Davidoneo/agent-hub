#!/usr/bin/env bash
# Refresh the canonical sources from a running Agent Hub installation.
#
# The Ansible role installs these root-level sources directly. This command is
# only for importing a deliberate live hotfix back into the source of truth.
#
#   ./scripts/sync-agent-hub.sh [/opt/agent-hub] [/usr/local/libexec/agent-hub] [/usr/local/bin]
#
# Afterwards review `git diff` before committing: this repository is public
# and the check below is a safety net, not a substitute for reading.
set -Eeuo pipefail

APP_ROOT=${1:-/opt/agent-hub}
LIBEXEC=${2:-/usr/local/libexec/agent-hub}
BIN_ROOT=${3:-/usr/local/bin}
DEST=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)

[[ -d "$APP_ROOT/app" ]] || { echo "not found: $APP_ROOT/app" >&2; exit 1; }
[[ -d "$LIBEXEC" ]] || { echo "not found: $LIBEXEC" >&2; exit 1; }

mkdir -p "$DEST/app/static" "$DEST/libexec" "$DEST/bin"
cp -a "$APP_ROOT/app/main.py" "$APP_ROOT/app/transcript.py" "$DEST/app/"
cp -a "$APP_ROOT/app/static/." "$DEST/app/static/"
cp -a "$LIBEXEC/." "$DEST/libexec/"
for command in agent-report agent-meeting-report agent-document; do
  [[ -f "$BIN_ROOT/$command" ]] || { echo "not found: $BIN_ROOT/$command" >&2; exit 1; }
  cp -a "$BIN_ROOT/$command" "$DEST/bin/$command"
done
find "$DEST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name '*.pyc' -delete

# Never import the instance configuration: config.env and profiles.json hold
# identities and tokens. The role templates them from Ansible variables.
rm -f "$DEST/libexec/config.env" "$DEST/libexec/profiles.json"

echo "== leaked identifiers check =="
# git@github.com and friends are generic placeholders, not personal data.
if grep -rnIE '([0-9]{9,10}:AA[A-Za-z0-9_-]{30,})|(gh[pousr]_[A-Za-z0-9]{20,})|(sk-[A-Za-z0-9]{20,})|[a-zA-Z0-9._%-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|[a-z0-9-]+\.ts\.net' \
     "$DEST" --exclude-dir=vendor 2>/dev/null \
   | grep -vE 'git@(github|gitlab|bitbucket)\.com|@(example|localhost)' ; then
  echo "REFUSING: the lines above look like credentials or personal hosts." >&2
  exit 2
fi
echo "clean"
echo "Synced into $DEST - now review: git diff --stat"

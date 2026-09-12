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

# The installed directories also contain private instance integrations. Import
# only versioned public paths, including every Python module and agent CLI.
paths=()
while IFS= read -r -d '' path; do
  case "$path" in
    app/*) source="$APP_ROOT/$path" ;;
    libexec/*) source="$LIBEXEC/${path#libexec/}" ;;
    bin/*) source="$BIN_ROOT/${path#bin/}" ;;
    *) continue ;;
  esac
  [[ -f "$source" ]] || { echo "not found: $source" >&2; exit 1; }
  paths+=("$path")
done < <(git -C "$DEST" ls-files -z -- app libexec bin)
(( ${#paths[@]} > 0 )) || { echo "no tracked public sources" >&2; exit 1; }

for path in "${paths[@]}"; do
  case "$path" in
    app/*) source="$APP_ROOT/$path" ;;
    libexec/*) source="$LIBEXEC/${path#libexec/}" ;;
    bin/*) source="$BIN_ROOT/${path#bin/}" ;;
  esac
  cp -- "$source" "$DEST/$path"
done

# Never import the instance configuration: config.env and profiles.json hold
# identities and tokens. The role templates them from Ansible variables.
cd "$DEST"
echo "== leaked identifiers check =="
# git@github.com and friends are generic placeholders, not personal data.
if grep -nIE '([0-9]{9,10}:AA[A-Za-z0-9_-]{30,})|(gh[pousr]_[A-Za-z0-9]{20,})|(sk-[A-Za-z0-9]{20,})|[a-zA-Z0-9._%-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|[a-z0-9-]+\.ts\.net' \
     "${paths[@]}" --exclude=xterm.js 2>/dev/null \
   | grep -vE 'git@(github|gitlab|bitbucket)\.com|@(example|localhost)' ; then
  echo "REFUSING: the lines above look like credentials or personal hosts." >&2
  exit 2
fi
echo "clean"
echo "Synced into $DEST - now review: git diff --stat"

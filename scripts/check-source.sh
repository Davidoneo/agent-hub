#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"

python3 - <<'PY'
from pathlib import Path

paths = list(Path("app").rglob("*.py"))
for folder in (Path("bin"), Path("libexec")):
    for path in folder.iterdir():
        if path.is_file() and "python" in path.read_text(
                encoding="utf-8", errors="ignore").splitlines()[0]:
            paths.append(path)
for path in paths:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print(f"Python syntax: {len(paths)} files OK")
PY

while IFS= read -r script; do
  bash -n "$script"
done < <(find scripts libexec packaging/github-audit -type f \
  -exec grep -Il '^#!/usr/bin/env bash' {} +)

node --check app/static/app.js
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  tests/test_report_ctl.py tests/test_meeting_report_ctl.py tests/test_static_ui.py \
  tests/test_health_ctl.py tests/test_tui_state.py tests/test_telegram_ctl.py \
  tests/test_telegram_outbox_ctl.py \
  tests/test_claude_refresh.py tests/test_session_ctl_delivery.py \
  tests/test_delivery_queue.py tests/test_harness_update_ctl.py \
  tests/test_session_ctl_conversation.py tests/test_usage_limit.py \
  tests/test_backlog_ctl.py tests/test_backlog_telegram_ctl.py
git diff --check
echo "Source checks: OK"

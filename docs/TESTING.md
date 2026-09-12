# Application regression tests

Install test dependencies in a dedicated Python environment:

```bash
python3 -m venv /tmp/agent-hub-test-tools
/tmp/agent-hub-test-tools/bin/pip install -r requirements-test.txt
/tmp/agent-hub-test-tools/bin/python -m playwright install --with-deps chromium
export PATH="/tmp/agent-hub-test-tools/bin:$PATH"
export CHROMIUM_PATH="$(python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
./scripts/check-source.sh
```

The source check discovers all `test_*.py` files. Browser and HTTP dependencies
are installed explicitly in CI, so the optional existing checks run there too.
Tests exercise synthetic data and extracted functions without starting the live
application. `AGENT_HUB_BASELINE=/path/to/older/source` additionally compares the
changed producers with an older source tree. Keep generated files outside Git.

## Differential audit

The standalone `refactor_*.py` / `refactor_*.cjs` audits accept the older source
tree as their first argument. They are intended for a disposable Linux filesystem
and network namespace, with synthetic users and no live Hub directories mounted.
Do not start `audit_backend_fixture.py` on the deployed host outside that boundary:
it exposes test-only fault injection endpoints. The fixture imports the real app
with temporary SQLite/files, while replacing host/provider/background adapters.
It models an existing installation, not a fresh-schema installation check.

From the candidate source tree inside that isolated environment:

```bash
python tests/refactor_api_compare.py /path/to/baseline
node tests/refactor_properties.cjs /path/to/baseline
node tests/refactor_ui_browser.cjs /path/to/baseline
node tests/refactor_fullstack_browser.cjs /path/to/baseline
```

Node scripts require Playwright 1.63.0, Chromium and Firefox. Node and Python
Playwright need their own compatible browser versions. Point `NODE_PATH` at the
Node tool installation and `PLAYWRIGHT_BROWSERS_PATH` at its readable browser
cache; `CHROMIUM` selects a system Chromium for the synthetic UI audit. The full
HTTP audit uses the Playwright browsers and writes observations, screenshots and
traces to `AUDIT_EVIDENCE` (default: `../evidence/fullstack`). Create evidence
output outside the source tree and close only browser sessions owned by the test.

The HTTP audit covers authorization, validation, upload limits, persistence and
concurrent messages. The browser audit covers draft recovery, network/SQLite
failure, CSRF refresh, attachment-only messages, escaping, downloads, Plan/Goal
and failed launches on six Chromium/Firefox configurations. Terminal transport
is real WebSocket; its producer, tmux operations, providers and inference are
synthetic. Firefox disables only its nested content sandbox in this isolated
harness because some Linux user-namespace configurations reject that extra layer.

These are equivalence tests: they preserve existing behavior, including known
quirks. They do not claim universal equivalence or pixel identity. Inspect the
screenshots and traces alongside the exact DOM, HTTP and database comparisons.

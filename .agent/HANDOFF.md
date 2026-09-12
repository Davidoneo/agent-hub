# Handoff

## 2026-09-12 — Reconcile the deployed Agent Hub changes

The project checkout is the canonical application source. The application,
static assets and generic installed wrappers were compared byte for byte with
the running installation before publication.

Included changes:

- Compact navigation and Settings, with accessible attention badges; session
  reports move under advanced options and escalation close actions stay explicit.
- Mobile transcript navigation stays inside the Hub. Downloads retain navigation
  and report errors; active raw logs stream only their initially opened size.
- Pending message delivery is distinct from confirmed failure across counters,
  badges and message history. Backlog ideas are consumed only after launch succeeds.
- Codex capacity stops require attention. Automated input stops at usage-reset
  consent menus; explicit terminal keys remain available. Both model usage
  windows are retained, while Spark details are hidden in the sidebar.
- Nightly stable harness updates use bounded processes and launch an ephemeral
  read-only audit only after a version change or error. Status exposes the report.
- Generic application notification queue and delivery support are public;
  application-specific notification wrappers and tests live in private ops.

Private operational handoffs, review artifacts and imported documents were
preserved outside public Git. The local review branch history remains available;
intermediate review commits are not a deployment source. Runtime caches and
private document imports are ignored. Local source checks discover every Python
test, including optional browser checks when their dependencies are installed.

Publication validation and the exact public revision are recorded in the private
operations handoff and its `desired/agent-hub/PUBLIC_VERSION`. No service restart
or runtime behavior change is needed for this source reconciliation.

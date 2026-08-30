"""Small SQLite primitives for Agent Hub's durable per-session delivery queue."""

PENDING = "pending"
LAUNCHING = "launching"
SENT = "sent"
FAILED = "delivery_failed"


def recover_pending(conn) -> None:
    """Recover interrupted jobs conservatively after a backend restart."""
    conn.execute(
        "UPDATE messages SET status=?, last_error=?, note=? "
        "WHERE status=? AND method='cli-arg'",
        (SENT, "", "prompt passato nella riga di comando; conferma TUI "
         "interrotta dal riavvio del backend", LAUNCHING))
    conn.execute(
        "UPDATE messages SET status=?, last_error=?, note=? "
        "WHERE status=? AND method NOT IN ('cli-arg','goal','setup')",
        (PENDING, "", "consegna ripresa automaticamente dopo il riavvio "
         "del backend", LAUNCHING))
    conn.execute(
        "UPDATE messages SET status=?, last_error=? "
        "WHERE status=? AND method IN ('goal','setup')",
        (FAILED, "backend riavviato durante la preparazione iniziale: "
         "usa «Invia ora» dopo aver verificato modalità e obiettivo", LAUNCHING))


def claim_next(conn, sid: str):
    """Claim the oldest deliverable message, never overtaking prior work."""
    conn.execute("BEGIN IMMEDIATE")
    message = conn.execute(
        "SELECT rowid,* FROM messages WHERE session_id=? AND kind!='control' "
        "AND status IN (?,?,?) ORDER BY created_at,rowid LIMIT 1",
        (sid, PENDING, LAUNCHING, FAILED)).fetchone()
    if not message or message["status"] != PENDING:
        return None, None
    if message["method"] in ("cli-arg", "goal", "setup"):
        return None, None
    changed = conn.execute(
        "UPDATE messages SET status=?,attempts=attempts+1,last_error='' "
        "WHERE id=? AND status=?", (LAUNCHING, message["id"], PENDING)).rowcount
    if changed != 1:
        return None, None
    session = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not session:
        conn.execute("UPDATE messages SET status=?,last_error=? WHERE id=?",
                     (FAILED, "sessione eliminata prima della consegna", message["id"]))
        return None, None
    return dict(session), dict(message)


def pending_sessions(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT m.session_id FROM messages m WHERE m.status=? "
        "AND m.method NOT IN ('cli-arg','goal','setup') AND m.rowid=("
        "SELECT x.rowid FROM messages x WHERE x.session_id=m.session_id "
        "AND x.kind!='control' AND x.status IN (?,?,?) "
        "ORDER BY x.created_at,x.rowid LIMIT 1) GROUP BY m.session_id",
        (PENDING, PENDING, LAUNCHING, FAILED))]

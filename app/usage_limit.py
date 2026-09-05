"""Decisioni sul limite d'uso di una harness: riconoscerlo e sapere quando riprovare.

Modulo senza dipendenze esterne, come `tui_state` e `delivery_queue`: la logica
che *decide* deve poter essere provata senza montare il backend.

Due decisioni distinte vivono qui.

`blocked_state` risponde alla domanda «questa schermata sta mostrando un
ostacolo noto?». La sorgente e' sempre lo schermo *visibile* del pane, mai lo
scrollback: un limite d'uso e' uno stato che la TUI continua a mostrare finche'
dura, mentre nel log resterebbe per sempre anche la frase citata da un agente
che di quel limite stava soltanto parlando.

`resume_due` risponde alla domanda «conviene ridare un turno a questa sessione
adesso?». L'istante di reset dichiarato dal provider e' un indizio, non una
prova: il valore letto dall'API e quello applicato davvero dalla TUI possono
divergere, e alcune finestre non dichiarano alcun istante. Quindi si attende il
momento atteso, si lascia un margine e poi si prova; la verifica vera e' il
tentativo stesso, che riesce solo se la TUI apre davvero un turno.
"""

# Ordine di valutazione: un login scaduto e' una condizione piu' specifica di
# un limite d'uso e va riconosciuto per primo.
STATES = ("AUTH_REQUIRED", "USAGE_LIMIT")


def blocked_state(compiled: dict, screen: str) -> str:
    """Stato riconosciuto sullo schermo corrente, o '' se nessuno.

    `compiled` mappa lo stato alla lista di espressioni regolari gia'
    compilate. Senza pattern configurati non si deduce nulla: meglio nessuno
    stato che uno inventato.
    """
    if not screen:
        return ""
    for state in STATES:
        for pattern in compiled.get(state) or []:
            if pattern.search(screen):
                return state
    return ""


def next_reset(windows, blocked_at: float) -> int:
    """Primo istante di reset atteso dopo l'inizio del blocco, 0 se sconosciuto.

    Una finestra puo' non dichiarare l'istante (Claude non lo espone finche' la
    finestra da 5 ore non e' stata consumata) e una finestra gia' scaduta prima
    del blocco descrive il passato: entrambe non dicono nulla su quando questa
    sessione tornera' a poter lavorare.
    """
    future = []
    for window in windows or []:
        try:
            moment = int((window or {}).get("resets_at") or 0)
        except (TypeError, ValueError):
            continue
        if moment > blocked_at:
            future.append(moment)
    return min(future) if future else 0


def resume_due(*, now: float, blocked_at: float, windows, attempts: int,
               last_attempt_at: float, retry_seconds: int, blind_seconds: int,
               max_attempts: int) -> bool:
    """Vero quando va tentata adesso la ripresa di una sessione bloccata.

    Il margine dopo l'istante dichiarato e' lo stesso `retry_seconds` usato fra
    un tentativo e l'altro, e serve a una cosa precisa: se la harness sa
    riprendere da sola — Claude Code lo fa quando `autoContinueAtUsageLimit` e'
    attivo — deve muoversi lei per prima, e noi non dobbiamo infilarci un turno
    in mezzo. I tentativi sono un numero chiuso: oltre quello la sessione resta
    visibilmente ferma e la decisione torna a chi la sta seguendo.
    """
    if max_attempts <= 0 or attempts >= max_attempts:
        return False
    if last_attempt_at and now - last_attempt_at < retry_seconds:
        return False
    reset = next_reset(windows, blocked_at)
    expected = reset if reset else blocked_at + blind_seconds
    return now >= expected + retry_seconds

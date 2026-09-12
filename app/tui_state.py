"""Riconoscimento conservativo dei dialoghi che bloccano una harness TUI.

Il chiamante deve passare soltanto lo schermo *corrente* del pane tmux, mai il
log storico: una domanda gia' risposta non deve mantenere NEEDS_INPUT attivo.
Le firme combinano piu' elementi stabili del footer per evitare di scambiare
una normale risposta dell'agente, che cita una domanda, per un dialogo vivo.
"""
import re


def _flat(text: str) -> str:
    return " ".join((text or "").lower().split())


def capacity_stopped(profile_id: str, screen: str) -> bool:
    """Il warning Codex chiude il turno e precede direttamente il composer.

    Un errore citato da un tool, seguito da altro output o da uno spinner di
    retry non dimostra un arresto. Non cercare questa firma nello scrollback.
    """
    if "codex" not in (profile_id or "").lower():
        return False
    warnings = list(re.finditer(
        r"(?m)^[ \t]*⚠ Selected model is at capacity\. Please\s+"
        r"try a different model\.[ \t]*$", screen or ""))
    if not warnings:
        return False
    tail = screen[warnings[-1].end():]
    # La prima riga successiva deve essere il prompt. Nel resto della
    # schermata non devono esserci nuovi messaggi o un turno in elaborazione.
    return bool(
        re.match(r"\s*›[^\n]*\n", tail)
        and not re.search(r"(?m)^[ \t]*[•■⚠]", tail)
        and len(re.findall(r"(?m)^[ \t]*›", tail)) == 1
        and "esc to interrupt" not in _flat(tail)
        and re.search(r"(?m)^[ \t]*gpt-[\w.-]+\s+[^\n]* · ", tail)
    )


def needs_input(profile_id: str, screen: str) -> bool:
    """True quando lo schermo corrente mostra una richiesta interattiva nota."""
    text = _flat(screen)
    profile = (profile_id or "").lower()
    if not text:
        return False

    if "codex" in profile:
        if capacity_stopped(profile_id, screen):
            return True
        question = re.search(r"question\s+\d+/\d+\s+\(\d+\s+unanswered\)", text)
        controls = (
            "tab to add notes" in text
            and "enter to submit" in text
            and ("navigate questions" in text or "esc to interrupt" in text)
        )
        return bool(question and controls)

    if "claude" in profile:
        return (
            "type something." in text
            and "enter to select" in text
            and "to navigate" in text
            and "esc to" in text
        )

    if "opencode" in profile:
        return (
            "type your own answer" in text
            and "↑↓ select" in text
            and bool(re.search(r"enter\s+(submit|toggle|confirm)", text))
            and "esc dismiss" in text
        )

    return False

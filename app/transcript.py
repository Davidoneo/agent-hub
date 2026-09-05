"""Conversione del log PTY grezzo in una trascrizione testuale leggibile.

Il log prodotto da `tmux pipe-pane` contiene il flusso PTY integrale: sequenze
ANSI/CSI, OSC (compresi gli hyperlink OSC 8), posizionamenti assoluti del
cursore e i ridisegni incrementali della TUI. Mostrarlo come testo nel browser
e' illeggibile, e un semplice strip delle sequenze non basta: le TUI come
Claude Code ridisegnano solo le celle cambiate e saltano le altre con CHA/CUP,
quindi il testo completo esiste soltanto nel buffer dello schermo.

`sanitize` esegue quindi una vera emulazione di schermo (griglia righe x
colonne, regione di scroll, scrollback) e restituisce le righe uscite dallo
schermo piu' il contenuto finale. Il risultato e' testo semplice: niente ANSI,
OSC, movimenti del cursore o altri caratteri di controllo.

Per una sessione viva la sorgente preferita resta comunque
`tmux capture-pane -p -J -S -`, che usa l'emulatore di tmux; `clean_capture`
si limita a normalizzarne l'output.
"""
from __future__ import annotations

import re

# Sequenze di escape riconosciute. L'ordine conta: le alternative piu' lunghe
# (OSC, DCS, CSI) devono precedere l'ESC a singolo carattere.
_ESC = (
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"      # OSC ... BEL | ST
    r"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"          # DCS / PM / APC / SOS
    r"|\x1b\[[0-?]*[ -/]*[@-~]"                # CSI
    r"|\x1b[()*+%][\x20-\x2f]*[0-9A-Za-z]"     # selezione charset
    r"|\x1b#[0-9]"                             # DEC line size
    r"|\x1b[0-9A-Za-z<=>\\\]^_`|}~]"           # escape a singolo carattere
    r"|\x1b"                                   # ESC spaiato a fine buffer
)
TOKEN_RE = re.compile(_ESC + r"|[\x00-\x1f\x7f]")
CSI_RE = re.compile(r"^\x1b\[([0-?]*)([ -/]*)([@-~])$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

DEFAULT_COLS, DEFAULT_ROWS = 100, 30
MAX_COLS, MAX_ROWS = 400, 200


class Screen:
    """Emulatore di schermo minimale, sufficiente per una trascrizione."""

    def __init__(self, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> None:
        self.w = max(20, min(MAX_COLS, cols))
        self.h = max(4, min(MAX_ROWS, rows))
        self.grid: list[list[str]] = [[] for _ in range(self.h)]
        self.r = self.c = 0
        self.top, self.bot = 0, self.h - 1
        self.saved = (0, 0)
        self.out: list[str] = []

    # ------------------------------------------------------------ scrollback
    def _emit(self, row: list[str]) -> None:
        self.out.append("".join(row).rstrip())

    def scroll_up(self, n: int = 1) -> None:
        for _ in range(min(n, self.h)):
            if self.top == 0:
                self._emit(self.grid[0])
            del self.grid[self.top]
            self.grid.insert(self.bot, [])

    def scroll_down(self, n: int = 1) -> None:
        for _ in range(min(n, self.h)):
            del self.grid[self.bot]
            self.grid.insert(self.top, [])

    def flush_screen(self, move_home: bool = True) -> None:
        """Salva il contenuto visibile nello scrollback e pulisce la griglia.

        Serve quando la TUI cancella lo schermo (ED 2) o passa al buffer
        alternativo: senza questo passaggio la trascrizione perdere il testo
        gia' mostrato. Il blocco viene scartato se ripete l'ultimo emesso,
        cosi' i ridisegni a schermo pieno non duplicano nulla.
        """
        rows = ["".join(r).rstrip() for r in self.grid]
        while rows and not rows[-1]:
            rows.pop()
        if rows and self.out[-len(rows):] != rows:
            self.out.extend(rows)
        self.grid = [[] for _ in range(self.h)]
        if move_home:
            self.r = self.c = 0

    # ---------------------------------------------------------------- output
    def write(self, s: str) -> None:
        while s:
            row = self.grid[self.r]
            space = self.w - self.c
            chunk, s = s[:space], s[space:]
            if self.c > len(row):
                row.extend(" " * (self.c - len(row)))
            row[self.c:self.c + len(chunk)] = list(chunk)
            self.c += len(chunk)
            if s:  # autowrap
                self.c = 0
                self.line_feed()

    def line_feed(self) -> None:
        if self.r >= self.bot:
            self.scroll_up()
            self.r = self.bot
        else:
            self.r += 1

    def reverse_index(self) -> None:
        if self.r <= self.top:
            self.scroll_down()
            self.r = self.top
        else:
            self.r -= 1

    def control(self, ch: str) -> None:
        if ch == "\n" or ch == "\v" or ch == "\f":
            self.line_feed()
        elif ch == "\r":
            self.c = 0
        elif ch == "\b":
            self.c = max(0, self.c - 1)
        elif ch == "\t":
            self.c = min(self.w - 1, (self.c // 8 + 1) * 8)
        # BEL, SO/SI e gli altri controlli non producono testo

    # ------------------------------------------------------------- sequenze
    def escape(self, tok: str) -> None:
        if len(tok) < 2:
            return
        kind = tok[1]
        if kind == "7":
            self.saved = (self.r, self.c)
        elif kind == "8":
            self.r, self.c = self.saved
            self.r = min(self.r, self.h - 1)
            self.c = min(self.c, self.w - 1)
        elif kind == "M":
            self.reverse_index()
        elif kind == "D":
            self.line_feed()
        elif kind == "E":
            self.c = 0
            self.line_feed()
        elif kind == "c":
            self.flush_screen()
            self.top, self.bot = 0, self.h - 1

    def csi(self, params: str, final: str) -> None:
        private = params[:1] if params[:1] in "?><=" else ""
        if private:
            if private == "?" and final in ("h", "l"):
                for p in _ints(params[1:]):
                    # buffer alternativo: il contenuto corrente va nello
                    # scrollback, cosi' la trascrizione non perde nulla
                    if p in (47, 1047, 1049):
                        self.flush_screen()
            return
        n = _ints(params)

        def arg(i: int, default: int = 1) -> int:
            return n[i] if i < len(n) and n[i] else default

        row = self.grid[self.r]
        if final in ("G", "`"):                       # CHA / HPA
            self.c = _clamp(arg(0) - 1, self.w - 1)
        elif final == "d":                            # VPA
            self.r = _clamp(arg(0) - 1, self.h - 1)
        elif final in ("H", "f"):                     # CUP
            self.r = _clamp(arg(0) - 1, self.h - 1)
            self.c = _clamp(arg(1) - 1, self.w - 1)
        elif final == "A":
            self.r = max(self.top if self.r >= self.top else 0, self.r - arg(0))
        elif final == "B":
            self.r = min(self.bot if self.r <= self.bot else self.h - 1, self.r + arg(0))
        elif final == "C":
            self.c = min(self.w - 1, self.c + arg(0))
        elif final == "D":
            self.c = max(0, self.c - arg(0))
        elif final == "E":
            self.c = 0
            self.r = min(self.bot, self.r + arg(0))
        elif final == "F":
            self.c = 0
            self.r = max(self.top, self.r - arg(0))
        elif final == "K":                            # EL
            mode = n[0] if n else 0
            if mode == 0:
                del row[self.c:]
            elif mode == 1:
                for i in range(min(self.c + 1, len(row))):
                    row[i] = " "
            else:
                row.clear()
        elif final == "J":                            # ED
            mode = n[0] if n else 0
            if mode == 0:
                del row[self.c:]
                for i in range(self.r + 1, self.h):
                    self.grid[i] = []
            elif mode == 1:
                for i in range(min(self.c + 1, len(row))):
                    row[i] = " "
                for i in range(0, self.r):
                    self.grid[i] = []
            else:
                self.flush_screen(move_home=False)
        elif final == "L":                            # IL
            k = min(arg(0), self.bot - self.r + 1)
            del self.grid[self.bot - k + 1:self.bot + 1]
            for _ in range(k):
                self.grid.insert(self.r, [])
        elif final == "M":                            # DL
            k = min(arg(0), self.bot - self.r + 1)
            del self.grid[self.r:self.r + k]
            for _ in range(k):
                self.grid.insert(self.bot, [])
        elif final == "P":                            # DCH
            del row[self.c:self.c + arg(0)]
        elif final == "X":                            # ECH
            for i in range(self.c, min(self.c + arg(0), len(row))):
                row[i] = " "
        elif final == "@":                            # ICH
            if self.c <= len(row):
                row[self.c:self.c] = [" "] * min(arg(0), self.w)
                del row[self.w:]
        elif final == "S":
            self.scroll_up(arg(0))
        elif final == "T":
            self.scroll_down(arg(0))
        elif final == "r":                            # DECSTBM
            self.top = _clamp(arg(0) - 1, self.h - 1)
            self.bot = _clamp(arg(1, self.h) - 1, self.h - 1)
            if self.bot <= self.top:
                self.top, self.bot = 0, self.h - 1
            self.r = self.c = 0
        # SGR (m), DSR (n), DA (c), modalita' non private: nessun effetto

    def result(self) -> list[str]:
        lines = list(self.out)
        rows = ["".join(r).rstrip() for r in self.grid]
        while rows and not rows[-1]:
            rows.pop()
        return lines + rows


def _ints(raw: str) -> list[int]:
    out = []
    for part in raw.split(";"):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return out


def _clamp(v: int, hi: int) -> int:
    return 0 if v < 0 else (hi if v > hi else v)


def _decode(data: bytes | str) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data


def probe_size(text: str, cols: int, rows: int) -> tuple[int, int]:
    """Ricava la geometria del pane dai posizionamenti presenti nel log.

    Il log puo' coprire piu' resize: si prende il massimo osservato, cosi'
    l'emulazione non tronca ne' sbaglia le righe assolute.
    """
    for m in re.finditer(r"\x1b\[([0-9;]*)[Hf]", text):
        p = _ints(m.group(1) or "1")
        if p and p[0] > rows:
            rows = p[0]
        if len(p) > 1 and p[1] > cols:
            cols = p[1]
    for m in re.finditer(r"\x1b\[([0-9]*)G", text):
        v = int(m.group(1) or 1)
        if v > cols:
            cols = v
    for m in re.finditer(r"\x1b\[([0-9;]*)r", text):
        p = _ints(m.group(1) or "")
        if len(p) > 1 and p[1] > rows:
            rows = p[1]
    return min(cols, MAX_COLS), min(rows, MAX_ROWS)


# --------------------------------------------------------------- cornice TUI
#
# La trascrizione e' il rendering dello schermo nel tempo, non un log di
# messaggi: ogni ridisegno della TUI lascia nel log la propria cornice, quindi
# separatori, riga di input vuota, spinner e footer di stato ricompaiono a
# ripetizione e spezzano i blocchi lunghi che si vogliono copiare.
#
# Il filtro qui sotto e' deliberatamente mirato, non una deduplicazione delle
# righe uguali: righe identiche possono essere contenuto legittimo (output
# ripetuto, righe di tabella, separatori scritti dall'agente) e comprimerle
# cancellerebbe informazione vera. Ogni regola riconosce un pattern specifico
# di cornice e nient'altro.
#
# Regola di prudenza: una riga di input con del testo NON viene mai rimossa,
# perche' in Claude Code `❯ ...` e' l'input reale dell'utente. Si tolgono solo
# i marcatori vuoti.

_BOX = "─│┌┐└┘├┤┬┴┼━┃╭╮╰╯═║╔╗╚╝╠╣╦╩╬"

_CHROME_RULES = (
    # separatore o residuo di riquadro: solo caratteri di disegno e spazi
    re.compile(r"^[\s" + _BOX + r"]+$"),
    # riga-righello con etichetta incorporata: "─ Worked for 3m 37s ─────────"
    re.compile(r"^[─━═]+\s.*\s[─━═]{3,}$"),
    # marcatore di input vuoto, senza testo dell'utente
    re.compile(r"^\s*[❯›»▌]\s*$"),
    # spinner di lavoro: "✻ Crunched for 2m 56s · …", "✽ Misting… (25s · …)"
    re.compile(r"^\s*[✻✽✢✳✶✷✸✹✺✱✲]\s+\S.*\d+\s*[ms]\b"),
    # stesso spinner quando il glifo degrada ad asterisco ASCII:
    # contatore di token fra parentesi, oppure "* <verbo> for <durata>"
    re.compile(r"\(\d+[hms][^)]*tokens\)\s*$"),
    re.compile(r"^\s*\*\s+\S+\s+for\s+\d+[hms]\b"),
    # spinner senza contatore, subito dopo l'invio: "* Frolicking…"
    re.compile(r"^\s*[✻✽✢✳✶✷✸✹✺✱✲*]\s+\w[\w' -]*…\s*$"),
    # footer di stato Claude Code
    re.compile(r"⏵⏵"),
    # coda destra del footer: "● high · /effort", "/rc active"
    re.compile(r"^\s*[●○]\s+\S+\s+·\s+/\w[\w-]*\s*$"),
    re.compile(r"^\s*/\w[\w-]*\s+active\s*$"),
    # suggerimento che Claude Code stampa mentre lavora
    re.compile(r"^\s*Still working\.\s+Check in from your phone\s*$"),
    # footer di stato Codex: "gpt-5.6-sol xhigh · ~"
    re.compile(r"^\s*\S+\s+(?:minimal|low|medium|high|xhigh)\s*·\s*~\s*$"),
)


def is_tui_chrome(line: str) -> bool:
    """Vero se la riga e' decorazione della TUI e non contenuto."""
    if not line.strip():
        return False
    return any(rule.search(line) for rule in _CHROME_RULES)


def strip_chrome(text: str) -> str:
    """Toglie la cornice TUI da una trascrizione gia' sanificata."""
    return _tidy([ln for ln in text.split("\n") if not is_tui_chrome(ln)])


def _tidy(lines: list[str]) -> str:
    """Comprime le righe vuote di riempimento della TUI."""
    out: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            out.append(line)
            continue
        blank += 1
        if blank <= 1:
            out.append("")
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return "\n".join(out)


def sanitize(data: bytes | str, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> str:
    """Rende leggibile un log PTY grezzo. Decodifica UTF-8 con sostituzione."""
    text = _decode(data)
    cols, rows = probe_size(text, cols, rows)
    sc = Screen(cols, rows)
    pos = 0
    for m in TOKEN_RE.finditer(text):
        if m.start() > pos:
            sc.write(text[pos:m.start()])
        pos = m.end()
        tok = m.group(0)
        if len(tok) == 1:
            if tok != "\x1b":
                sc.control(tok)
            continue
        csi = CSI_RE.match(tok)
        if csi:
            if not csi.group(2):
                sc.csi(csi.group(1), csi.group(3))
        elif tok[1] not in "]P^_X()*+%#":
            sc.escape(tok)
        # OSC / DCS / charset: scartati, il testo che contengono non e' output
    if pos < len(text):
        sc.write(text[pos:])
    return _tidy(sc.result())


def clean_capture(data: bytes | str) -> str:
    """Normalizza l'output di `tmux capture-pane -p -J`.

    E' gia' privo di sequenze ANSI, ma puo' contenere caratteri di controllo
    residui provenienti dal contenuto stesso del pane.
    """
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(_ESC, "", text)
    lines = [CTRL_RE.sub("", ln.replace("\t", "    ")).rstrip() for ln in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def tail(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool]:
    """Restituisce la coda della trascrizione entro i limiti dati."""
    truncated = False
    lines = text.split("\n")
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    out = "\n".join(lines)
    if max_bytes and len(out.encode("utf-8")) > max_bytes:
        chopped = out.encode("utf-8")[-max_bytes:].decode("utf-8", "ignore")
        nl = chopped.find("\n")
        out = chopped[nl + 1:] if nl >= 0 else chopped
        truncated = True
    return out, truncated

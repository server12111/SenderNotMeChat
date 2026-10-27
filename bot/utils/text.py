from __future__ import annotations

import random
import re
from datetime import datetime, timedelta

SEP = "━━━━━━━━━━━━━━"

STATUS = {
    "draft": "📝 Черновик",
    "scheduled": "⏳ По расписанию",
    "queued": "⏳ В очереди",
    "running": "🟢 Активна",
    "paused": "⏸ Пауза",
    "done": "✅ Завершена",
    "cancelled": "🔴 Остановлена",
    "error": "⚠️ Ошибка",
}
STATUS_EMOJI = {k: v.split()[0] for k, v in STATUS.items()}
ACTIVE_STATUSES = ("queued", "running", "paused", "scheduled")


def progress_bar(done: int, total: int, width: int = 10) -> str:
    pct = int(done * 100 / total) if total else 0
    filled = round(width * pct / 100)
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct}% ({done}/{total})"


def fmt_dt(dt: datetime | None, tz: int = 0) -> str:
    if not dt:
        return "—"
    return (dt + timedelta(hours=tz)).strftime("%d.%m.%Y %H:%M")


def tz_label(tz: int) -> str:
    return f"UTC{tz:+d}" if tz else "UTC"


# ───────────── спинтакс ─────────────
_SPIN = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def has_spintax(text: str) -> bool:
    return bool(_SPIN.search(text))


def render_spintax(text: str) -> str:
    """{а|б|{в|г}} — раскрываем от внутренних скобок к внешним."""
    while True:
        new = _SPIN.sub(lambda m: random.choice(m.group(1).split("|")), text)
        if new == text:
            return text
        text = new


# ───────────── разбор баз чатов ─────────────
_USER = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")
_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(.+)$", re.I)


def normalize_target(tok: str) -> str | None:
    """@username | числовой ID | +inviteHash. Всё остальное — мусор."""
    tok = tok.strip().strip(",;")
    if not tok:
        return None
    m = _LINK.match(tok)
    if m:
        path = m.group(1).split("?")[0].strip("/")
        if path.startswith("+"):
            return "+" + path[1:]
        if path.lower().startswith("joinchat/"):
            return "+" + path.split("/", 1)[1]
        name = path.split("/")[0]
        return "@" + name.lower() if _USER.match(name) else None
    if re.fullmatch(r"-?\d{5,}", tok):
        return tok
    m = _USER.match(tok)
    return "@" + m.group(1).lower() if m else None


def parse_targets(raw: str) -> tuple[list[str], int, int]:
    """-> (уникальные цели, дубликаты, невалидные)"""
    seen: dict[str, None] = {}
    dups = bad = 0
    for tok in re.split(r"[\s,;]+", raw):
        if not tok:
            continue
        t = normalize_target(tok)
        if t is None:
            bad += 1
        elif t in seen:
            dups += 1
        else:
            seen[t] = None
    return list(seen), dups, bad


def parse_delay(raw: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,5})\s*[-–—: ]\s*(\d{1,5})\s*", raw)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (a, b) if a <= b else (b, a)


# ───────────── длительности и часы активности ─────────────
_DUR = re.compile(r"^\s*(\d+)\s*(с|сек|s|м|мин|m|ч|час|h|д|дн|d)?\w*\s*$", re.I)
_UNIT = {"с": 1, "сек": 1, "s": 1, "м": 60, "мин": 60, "m": 60, "ч": 3600, "час": 3600, "h": 3600, "д": 86400, "дн": 86400, "d": 86400}


def parse_duration(raw: str) -> int | None:
    """'300' | '5м' | '2ч' | '1д' -> секунды (число без единицы = секунды)."""
    m = _DUR.match(raw or "")
    if not m:
        return None
    return int(m.group(1)) * _UNIT.get((m.group(2) or "с").lower(), 1)


def fmt_duration(sec: int) -> str:
    if sec % 86400 == 0 and sec >= 86400:
        return f"{sec // 86400} д"
    if sec % 3600 == 0 and sec >= 3600:
        return f"{sec // 3600} ч"
    if sec % 60 == 0 and sec >= 60:
        return f"{sec // 60} мин"
    return f"{sec} сек"


_RANGE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\s*$")


def parse_hours(raw: str, tz: int) -> list[list[int]] | None:
    """'10:00-13:00, 18:00-22:00' (время пользователя) -> [[start, end], ...] в минутах UTC. None — ошибка формата."""
    out = []
    for part in raw.split(","):
        m = _RANGE.match(part)
        if not m:
            return None
        h1, m1, h2, m2 = map(int, m.groups())
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
            return None
        a, b = (h1 * 60 + m1 - tz * 60) % 1440, (h2 * 60 + m2 - tz * 60) % 1440
        if a == b:
            return None
        out.append([a, b])
    return out or None


def fmt_hours(hours: list | None, tz: int = 0) -> str:
    if not hours:
        return "24/7"

    def f(x: int) -> str:
        x = (x + tz * 60) % 1440
        return f"{x // 60:02d}:{x % 60:02d}"

    return ", ".join(f"{f(a)}–{f(b)}" for a, b in hours)


def hours_wait(hours: list | None, now: datetime) -> float:
    """Секунд до начала ближайшего окна активности; 0 — сейчас активно (или 24/7)."""
    if not hours:
        return 0
    cur = now.hour * 60 + now.minute
    best = None
    for a, b in hours:
        inside = (a <= cur < b) if a < b else (cur >= a or cur < b)  # окно может переходить через полночь
        if inside:
            return 0
        d = (a - cur) % 1440
        best = d if best is None else min(best, d)
    return best * 60 - now.second

"""Recurring tasks: the rule format, the next occurrence and a phrase parser (pure, no I/O).

A rule is a small JSON object stored in ``app_tasks.recurrence``:

* ``{"freq": "daily"}``
* ``{"freq": "weekly", "days": [0, 3]}`` (0 = Monday ... 6 = Sunday)
* ``{"freq": "monthly", "day": 1}`` (1-31; a short month uses its last day)
* ``{"freq": "interval", "interval": 3}`` (every N days, counted from the task's due date)
* ``{"freq": "yearly", "month": 3, "day": 12}``

plus an optional ``"until": "YYYY-MM-DD"`` (the last day an occurrence may fall on). Completing an occurrence
creates the next one (see :func:`next_due`); the phrase parser turns "chaque lundi", "tous les jours",
"le 1er de chaque mois", "every 3 days" into a rule for the voice notes and the MCP tool.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

__all__ = ["FREQS", "normalize_rule", "occurs_on", "next_after", "first_on_or_after", "next_due", "parse_recurrence",
           "describe"]

FREQS = ("daily", "weekly", "monthly", "interval", "yearly")
MAX_INTERVAL = 366
_SCAN_DAYS = 400  # a weekly/monthly rule always has an occurrence within this many days
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_rule(raw: Any, anchor: date) -> dict[str, Any] | None:
    """A validated rule from a dict or a phrase; defaults (week day, day of month) come from ``anchor``.

    ``None`` / ``""`` / ``{}`` mean "no recurrence"; anything unusable raises ValueError.
    """
    if raw is None or raw == "" or raw == {}:
        return None
    if isinstance(raw, str):
        rule, _ = parse_recurrence(raw)
        if rule is None:
            raise ValueError(f"could not understand the recurrence {raw!r}")
        raw = rule
    if not isinstance(raw, dict):
        raise ValueError("recurrence must be an object or a phrase")
    freq = str(raw.get("freq") or "").strip().lower()
    if freq not in FREQS:
        raise ValueError(f"recurrence freq must be one of {', '.join(FREQS)}")
    out: dict[str, Any] = {"freq": freq}
    if freq == "weekly":
        days = raw.get("days")
        if days in (None, []):
            days = [anchor.weekday()]
        if not isinstance(days, list) or any(_int(d) is None or not 0 <= int(d) <= 6 for d in days):
            raise ValueError("weekly recurrence days must be 0 (Monday) to 6 (Sunday)")
        out["days"] = sorted({int(d) for d in days})
    elif freq == "monthly":
        day = _int(raw.get("day")) if raw.get("day") is not None else anchor.day
        if day is None or not 1 <= day <= 31:
            raise ValueError("monthly recurrence day must be 1 to 31")
        out["day"] = day
    elif freq == "interval":
        n = _int(raw.get("interval"))
        if n is None or not 1 <= n <= MAX_INTERVAL:
            raise ValueError(f"recurrence interval must be 1 to {MAX_INTERVAL} days")
        if n == 1:
            out = {"freq": "daily"}
        else:
            out["interval"] = n
    elif freq == "yearly":
        month = _int(raw.get("month")) if raw.get("month") is not None else anchor.month
        day = _int(raw.get("day")) if raw.get("day") is not None else anchor.day
        if month is None or not 1 <= month <= 12 or day is None or not 1 <= day <= 31:
            raise ValueError("yearly recurrence needs a valid month (1-12) and day (1-31)")
        out["month"], out["day"] = month, day
    until = raw.get("until")
    if until not in (None, ""):
        try:
            out["until"] = date.fromisoformat(str(until)[:10]).isoformat()
        except ValueError as exc:
            raise ValueError("recurrence until must be YYYY-MM-DD") from exc
    return out


def _clamped(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def occurs_on(rule: dict[str, Any], d: date, start: date | None = None) -> bool:
    """Does the rule fall on ``d``? ``start`` (the series' reference due date) anchors every-N-days rules."""
    freq = rule["freq"]
    if freq == "daily":
        return True
    if freq == "weekly":
        return d.weekday() in rule["days"]
    if freq == "monthly":
        return d == _clamped(d.year, d.month, rule["day"])
    if freq == "yearly":
        return d.month == rule["month"] and d == _clamped(d.year, d.month, rule["day"])
    ref = start or d
    return (d - ref).days % int(rule["interval"]) == 0


def next_after(rule: dict[str, Any], after: date, start: date | None = None) -> date | None:
    """The first occurrence strictly after ``after`` (None past ``until``)."""
    until = date.fromisoformat(rule["until"]) if rule.get("until") else None
    freq = rule["freq"]
    found: date | None = None
    if freq == "daily":
        found = after + timedelta(days=1)
    elif freq == "interval":
        n = int(rule["interval"])
        ref = start or after
        steps = (after - ref).days // n + 1 if after >= ref else 0
        found = ref + timedelta(days=steps * n)
        if found <= after:
            found += timedelta(days=n)
    elif freq == "monthly":
        y, m = after.year, after.month
        for _ in range(14):
            cand = _clamped(y, m, rule["day"])
            if cand > after:
                found = cand
                break
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    elif freq == "yearly":
        for y in range(after.year, after.year + 3):
            cand = _clamped(y, rule["month"], rule["day"])
            if cand > after:
                found = cand
                break
    else:
        for i in range(1, _SCAN_DAYS):
            cand = after + timedelta(days=i)
            if occurs_on(rule, cand, start):
                found = cand
                break
    if found is None or (until is not None and found > until):
        return None
    return found


def first_on_or_after(rule: dict[str, Any], day: date, start: date | None = None) -> date | None:
    """The first occurrence on or after ``day``."""
    return next_after(rule, day - timedelta(days=1), start or day)


def next_due(rule: dict[str, Any], due: date | None, today: date) -> date | None:
    """Due date of the occurrence that follows one completed ``today``.

    The next occurrence comes after the later of its due date and today: finishing a daily task three days late
    does not queue three overdue copies, and finishing a weekly one early still moves to the following week.
    Every-N-days rules keep their cadence from the due date.
    """
    ref = due or today
    return next_after(rule, max(ref, today), ref)


# --------------------------------------------------------------------------- phrases

_WEEKDAY_WORDS: dict[str, int] = {}
for _idx, _names in enumerate((
    ("monday", "mondays", "mon", "lundi", "lundis", "tnin", "letnin", "lethnin"),
    ("tuesday", "tuesdays", "tue", "mardi", "mardis", "tlat", "tlata"),
    ("wednesday", "wednesdays", "wed", "mercredi", "mercredis", "larb3", "larbaa", "arbe3"),
    ("thursday", "thursdays", "thu", "jeudi", "jeudis", "lkhmis", "khmis", "khamis"),
    ("friday", "fridays", "fri", "vendredi", "vendredis", "jem3a", "jm3a", "jemaa", "jmaa"),
    ("saturday", "saturdays", "sat", "samedi", "samedis", "sebt", "sabt"),
    ("sunday", "sundays", "sun", "dimanche", "dimanches", "l7ad", "had"),
)):
    for _n in _names:
        _WEEKDAY_WORDS[_n] = _idx

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "other": 2,
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7, "dix": 10, "quinze": 15,
    "jouj": 2, "zouj": 2, "tlata": 3, "rb3a": 4,
}
_NUM = r"(\d{1,3}|" + "|".join(sorted((w.strip() for w in _NUMBER_WORDS), key=len, reverse=True)) + r")"
_DAY_ALT = "|".join(sorted(_WEEKDAY_WORDS, key=len, reverse=True))
_PLURAL_ALT = "|".join(sorted((w for w in _WEEKDAY_WORDS if len(w) > 4 and w.endswith("s")), key=len, reverse=True))
_EVERY = r"(?:every|each|chaque|tous\s+les|toutes\s+les|kol|koll|kull)"
_ORD = r"(\d{1,2})\s*(?:er|ere|ère|e|eme|ème|st|nd|rd|th)?"
_UNTIL_RE = re.compile(r"\s*(?:,\s*)?\b(?:until|till|jusqu'?(?:au|à|a)|jusqu'au|htal?)\s+(\d{4}-\d{2}-\d{2})\b")


def _num(word: str) -> int | None:
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word)


def _clean(text: str) -> str:
    t = text.lower().replace("’", "'").replace(" ", " ")
    return re.sub(r"\s+", " ", t).strip()


def _tidy(text: str) -> str:
    rest = re.sub(r"\s+", " ", text).strip(" ,;.-")
    return re.sub(r"^(?:and|et|w)\s+|\s+(?:and|et|w)$", "", rest).strip()


_DAY_LIST = rf"((?:{_DAY_ALT})(?:\s*(?:,|and|et|w|&)\s*(?:{_DAY_ALT}))*)"
_PLURAL_LIST = rf"((?:{_PLURAL_ALT})(?:\s*(?:,|and|et|&)\s*(?:les\s+)?(?:{_PLURAL_ALT}))*)"
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # every 3 days / tous les 3 jours / every other day / un jour sur deux / kol 3 ayam
    (re.compile(rf"\b{_EVERY}\s+{_NUM}\s+(?:days?|jours?|ayam|iyam|nhar)\b"), "ndays"),
    (re.compile(rf"\bun\s+jour\s+sur\s+{_NUM}\b"), "ndays"),
    # every 2 weeks / toutes les 2 semaines
    (re.compile(rf"\b{_EVERY}\s+{_NUM}\s+(?:weeks?|semaines?|simanat)\b"), "nweeks"),
    # weekdays / weekends
    (re.compile(r"\b(?:every\s+weekday|on\s+weekdays|weekdays|en\s+semaine|du\s+lundi\s+au\s+vendredi|"
                r"monday\s+to\s+friday|chaque\s+jour\s+de\s+(?:la\s+)?semaine)\b"), "weekdays"),
    (re.compile(r"\b(?:every\s+weekend|on\s+weekends|chaque\s+week-?end|tous\s+les\s+week-?ends|le\s+week-?end)\b"),
     "weekends"),
    # every monday (and thursday) / chaque lundi / tous les lundis et jeudis / kol tnin / on mondays / les mardis
    (re.compile(rf"\b{_EVERY}\s+{_DAY_LIST}\b"), "days"),
    (re.compile(rf"\b(?:on\s+|les\s+)?{_PLURAL_LIST}\b"), "days"),
    # daily
    (re.compile(r"\b(?:every\s*day|each\s+day|daily|tous\s+les\s+jours|chaque\s+jour|quotidien(?:ne)?(?:ment)?|"
                r"kol\s+nhar|koll\s+nhar|kul\s+yum|kol\s+yom)\b"), "daily"),
    # the 1st of every month / le 1er de chaque mois / every month on the 15th / le 15 du mois
    (re.compile(rf"\b(?:the\s+|le\s+)?{_ORD}\s+(?:of\s+)?(?:every|each|de\s+chaque|du|de\s+tous\s+les)\s+(?:month|mois)\b"
                r"(?!\s+(?:prochain|dernier|next|last))"), "monthday"),
    (re.compile(rf"\b(?:every|each)\s+month\s+on\s+the\s+{_ORD}\b"), "monthday"),
    (re.compile(rf"\b(?:chaque|tous\s+les)\s+mois\s+le\s+{_ORD}\b"), "monthday"),
    (re.compile(r"\b(?:every\s+month|each\s+month|monthly|chaque\s+mois|tous\s+les\s+mois|mensuel(?:le)?(?:ment)?|"
                r"kol\s+(?:chher|chhar|shhar))\b"), "monthly"),
    (re.compile(r"\b(?:every\s+week|each\s+week|weekly|chaque\s+semaine|toutes\s+les\s+semaines|"
                r"hebdomadaire(?:ment)?|kol\s+simana)\b"), "weekly"),
    (re.compile(r"\b(?:every\s+year|each\s+year|yearly|annually|chaque\s+ann[ée]e|tous\s+les\s+ans|"
                r"annuel(?:le)?(?:ment)?|kol\s+3am)\b"), "yearly"),
]


def parse_recurrence(text: str | None) -> tuple[dict[str, Any] | None, str]:
    """``(rule, rest)`` from a phrase: "chaque lundi à 9h" -> ``({"freq": "weekly", "days": [0]}, "à 9h")``.

    The rule is not normalized (weekly/monthly defaults are filled by :func:`normalize_rule`); ``rest`` is the
    phrase without the recurrence words (to resolve a time or a start day from). ``(None, text)`` when nothing
    recurring is said.
    """
    if not text or not text.strip():
        return None, text or ""
    t = _clean(text)
    until: str | None = None
    m = _UNTIL_RE.search(t)
    if m:
        until = m.group(1)
        t = t[: m.start()] + " " + t[m.end():]
    for rx, kind in _PATTERNS:
        m = rx.search(t)
        if not m:
            continue
        rule: dict[str, Any] | None = None
        if kind in ("ndays", "nweeks"):
            n = _num(m.group(1))
            if n and 1 <= n <= MAX_INTERVAL // (7 if kind == "nweeks" else 1):
                n = n * 7 if kind == "nweeks" else n
                rule = {"freq": "daily"} if n == 1 else {"freq": "interval", "interval": n}
        elif kind == "weekdays":
            rule = {"freq": "weekly", "days": [0, 1, 2, 3, 4]}
        elif kind == "weekends":
            rule = {"freq": "weekly", "days": [5, 6]}
        elif kind == "days":
            words = re.findall(_DAY_ALT, m.group(1))
            rule = {"freq": "weekly", "days": sorted({_WEEKDAY_WORDS[w] for w in words})}
        elif kind == "monthday":
            day = int(m.group(1))
            if 1 <= day <= 31:
                rule = {"freq": "monthly", "day": day}
        else:
            rule = {"freq": kind}
        if rule is None:
            continue
        if until:
            rule["until"] = until
        return rule, _tidy(t[: m.start()] + " " + t[m.end():])
    return None, text


def describe(rule: dict[str, Any] | None) -> str:
    """Short English label: "Daily", "Every Mon, Thu", "Monthly on the 1st", "Every 3 days", "Yearly on Mar 12"."""
    if not rule:
        return ""
    freq = rule.get("freq")
    if freq == "daily":
        label = "Daily"
    elif freq == "weekly":
        days = rule.get("days") or []
        if days == [0, 1, 2, 3, 4]:
            label = "Weekdays"
        elif days == [5, 6]:
            label = "Weekends"
        else:
            label = "Every " + ", ".join(_DAY_NAMES[d][:3] for d in days)
    elif freq == "monthly":
        d = int(rule.get("day") or 1)
        suffix = "th" if 10 <= d % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
        label = f"Monthly on the {d}{suffix}"
    elif freq == "interval":
        label = f"Every {rule.get('interval')} days"
    elif freq == "yearly":
        label = f"Yearly on {calendar.month_abbr[int(rule.get('month') or 1)]} {rule.get('day')}"
    else:
        return ""
    return f"{label} until {rule['until']}" if rule.get("until") else label

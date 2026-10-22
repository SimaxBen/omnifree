"""Pure helpers of the AI assistant (``/assistant``): routing, memory relevance, tool plumbing, PDF text.

Nothing here does I/O, so every rule is unit-tested directly.

Routing ("choose when and when not")
    :func:`classify` is a deterministic pre-classifier over French, English and Darija (Latin and
    Arabic script). It answers ``data`` (the message is about the owner's money, tasks, agenda,
    notes, files or asks to create/remember something), ``general`` (coding, translation, knowledge,
    chit-chat) or ``ambiguous``; only the last one costs a tiny LLM routing call. A general message
    never reaches a tool and never carries personal data (only the owner's answer-style
    *preferences*, see below). "add" / "create" / "remember" alone are not enough for ``data``: a
    write needs a data object ("ajoute une tâche", "add an expense") or a first-person statement
    ("j'ai payé"), so "how do I add an item to a list in python" stays general.
    :func:`classify_turn` also looks at the previous user message: a short follow-up of a data turn
    ("et le mois dernier ?", "mark it done") is ``ambiguous`` instead of plain general.

Memory relevance
    Facts are of two kinds: ``preference`` (how the owner wants answers: language, tone, length)
    and ``fact`` (everything else). :func:`select_memories` sends preferences on every turn and
    facts on data turns only (pinned first, then by keyword overlap).
    :func:`rank_memories` scores saved facts by keyword overlap with the conversation (accents
    folded, stop words dropped, long words cut to a common stem); pinned facts always come first.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "Route",
    "Decision",
    "classify",
    "classify_turn",
    "parse_route_answer",
    "detect_language",
    "fold",
    "keywords",
    "rank_memories",
    "memory_kind",
    "select_memories",
    "openai_tool",
    "parse_tool_args",
    "parse_plan",
    "compact_json",
    "result_summary",
    "write_summary",
    "fallback_answer",
    "pdf_context",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "REMEMBER_TOOL",
    "CONTENT_TOOLS",
    "REMEMBER_SPEC",
]

Route = Literal["data", "general", "ambiguous"]

# The MCP tools the assistant may read with, and the ones that create something (confirmed by the
# owner in the PWA unless auto-confirm is on). Deletes are never exposed, as on /mcp.
READ_TOOLS: tuple[str, ...] = (
    "expenses_month", "money_overview", "tasks_list", "notes_search", "notes_get", "ask_drive",
    "calendar_events", "budget_status", "drive_search",
)
WRITE_TOOLS: tuple[str, ...] = ("tasks_add", "expense_add", "notes_create", "income_add", "calendar_add_event")
REMEMBER_TOOL = "remember"
# Reads that return third-party text (notes and receipts from mail/SMS, Drive files, invites): once one
# ran (or a file is attached) the turn is "untrusted" and every write, `remember` included, waits for a tap.
CONTENT_TOOLS: frozenset[str] = frozenset({"notes_search", "notes_get", "ask_drive", "drive_search", "calendar_events"})

REMEMBER_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REMEMBER_TOOL,
        "description": (
            "Save a durable fact or preference the owner just stated about themselves (e.g. 'my salary arrives "
            "on the 25th', 'I prefer answers in Darija'), in one short sentence in the owner's language. "
            "Not for one-off requests, tasks or expenses."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "The fact, one sentence (max 300 chars)."},
                "kind": {
                    "type": "string", "enum": ["fact", "preference"],
                    "description": "preference = how the owner wants answers (language, tone, length); fact = anything else.",
                },
            },
            "required": ["fact"],
        },
    },
}


# --------------------------------------------------------------------------- text folding


def fold(text: str) -> str:
    """Lowercase, accents and Arabic diacritics removed, apostrophes unified, spaces collapsed."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("`", "'").lower()
    return re.sub(r"\s+", " ", text).strip()


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(fold(p) for p in parts), re.I)  # folded like the text (Arabic hamza, accents)


# Domain keywords, matched on folded text (no accents). \b does not work next to Arabic letters
# in every engine the same way, so Arabic words are matched as plain substrings.
_DOMAINS: dict[str, re.Pattern[str]] = {
    "money": _rx(
        r"\bdepens\w*", r"\bspen[dt]\w*", r"\bexpens\w*", r"\bbudget\w*", r"\bargent\b", r"\bmoney\b",
        r"\bflou?s+\b", r"\bfloss\b", r"\bsalai?r\w*", r"\bsalary\b", r"\brevenus?\b", r"\bincomes?\b",
        r"\bsolde\b", r"\bbalance\b", r"\bepargne\b", r"\bsavings?\b", r"\bpaye[rsz]?\b", r"\bpaid\b",
        r"\bachats?\b", r"\bachete\w*", r"\bbought\b", r"\bfactures?\b", r"\bbills?\b", r"\bloyer\b",
        r"\brent\b", r"\bdirhams?\b", r"\b\d+\s?(dh|dhs|mad|eur|euros?|usd)\b", r"\bcash\b", r"\btransactions?\b",
        r"\babonnements?\b", r"\bsubscriptions?\b", r"\bcompte (bancaire|en banque)\b", r"\bbank\b", r"\bbanque\b",
        r"\bkhl[ae]?st\b", r"\bkhless?t\b", r"\bsr[ae]?ft\b", r"\bchhal\b", r"\bch?hal (khl|sr)",
        "مصاريف", "مصروف", "ميزانية", "فلوس", "راتب", "حساب", "خلصت", "صرفت", "شحال", "دراهم", "درهم",
    ),
    "tasks": _rx(
        r"\btaches?\b", r"\btasks?\b", r"\bto-?dos?\b", r"\ba faire\b", r"\brappel\w*", r"\bremind\w*",
        r"\bdeadlines?\b", r"\becheances?\b", r"\bkhass?ni\b", "مهمة", "مهام", "تذكير", "فكرني",
    ),
    "calendar": _rx(
        r"\bagenda\b", r"\bcalendri?e?r\b", r"\brendez-?vous\b", r"\brdv\b", r"\bmeetings?\b", r"\breunions?\b",
        r"\bappointments?\b", r"\bevents?\b", r"\bevenements?\b", r"\bplanning\b", r"\bemploi du temps\b",
        r"\bmaw[ae]?3id\b", r"\bmaw3ed\b", "موعد", "مواعيد", "اجتماع",
    ),
    "notes": _rx(
        r"\bmes notes\b", r"\bmy notes\b", r"\bnotebooks?\b", r"\bcarnets?\b", r"\bdans mes notes\b",
        r"\bnotes? (sur|about|on)\b", r"\bnote (que|ca|this|that)\b", "ملاحظات", "ملاحظة",
    ),
    "drive": _rx(
        r"\bdrive\b", r"\bmes (fichiers|documents|docs|pdfs?)\b", r"\bmy (files|documents|docs|pdfs?)\b",
        r"\bfichiers?\b", "ملفات", "ملف",
    ),
    # First-person statements only: a bare "add" / "create" / "remember" is anchored in classify().
    "write": _rx(r"\bj'ai (paye|depense|achete|recu)\b", r"\bi (paid|spent|bought|received)\b"),
    "memory": _rx(
        r"\bje prefere\b", r"\bi prefer\b", r"\bmy name is\b", r"\bje m'appelle\b", r"\bje suis ne\b",
        r"\bmon (salaire|anniversaire|loyer)\b", r"\bmy (salary|birthday|rent)\b",
    ),
    "quota": _rx(r"\bquotas?\b", r"\bfree[- ]tier\b"),
}

# "add" / "create" count as data only next to a data object ("ajoute une tâche", "add an expense");
# "remember" only when the message has no general keyword ("remember the difference between...").
_WRITE_VERB = _rx(
    r"\bajout\w*", r"\badd\b", r"\bcree[rz]?\b", r"\bcreate\b", r"\bplanifie\w*", r"\benregistre\w*",
    r"\bzid\b", r"\bktb\b", r"\bsjl\b", "زيد", "سجل", "اكتب",
)
_WRITE_OBJECT = _rx(
    r"\btaches?\b", r"\btasks?\b", r"\bto-?dos?\b", r"\bdepenses?\b", r"\bexpenses?\b", r"\bnotes?\b",
    r"\brdv\b", r"\brendez-?vous\b", r"\bevents?\b", r"\bevenements?\b", r"\brappels?\b", r"\breminders?\b",
    r"\bmeetings?\b", r"\breunions?\b", r"\brevenus?\b", r"\bincomes?\b", r"\bsalaire\b", r"\bagenda\b",
    r"\bcalendri?e?r\b", r"\bachats?\b", r"\bcourses\b", r"\b\d+\s?(dh|dhs|mad|eur|euros?|usd)\b",
    "مهمة", "مصروف", "موعد", "ملاحظة", "تذكير",
)
_REMEMBER_VERB = _rx(
    r"\bremember\b", r"\bsouviens\b", r"\bretiens\b", r"\bn'oublie pas\b", r"\bdon'?t forget\b", r"\bt?fakar\b",
    "تذكر", "عقل",
)

# General topics (a data word next to one goes to the LLM) and knowledge-question framings (a data
# word next to one is data only when the owner speaks about themselves: "c'est quoi mon solde").
_GENERAL_TOPIC = _rx(
    r"\bcode\b", r"\bcoding\b", r"\bpython\b", r"\bjavascript\b", r"\btypescript\b", r"\bsql\b", r"\bregex\b",
    r"\bfunction\b", r"\bfonction\b", r"\bbug\b", r"\bstack ?trace\b", r"\bcompil\w*", r"\bapi\b",
    r"\breact\b", r"\bcomponent\b", r"\bcomposant\b", r"\bcss\b", r"\bhtml\b",
    r"\btranslat\w*", r"\btradui\w*", r"\btraduction\b", r"\btarjem\w*", "ترجم",
    r"\bpoe?me?\b", r"\bstory\b", r"\bhistoire\b", r"\bjoke\b", r"\bblague\b", r"\brecipe\b", r"\brecette\b",
    r"\bmath\w*", r"\bequation\b", r"\bwrite an? (email|letter|essay|poem)\b", r"\bredige\w*",
    r"\bdifference between\b", r"\bdifference entre\b",
)
_GENERAL_ASK = _rx(
    r"\bexplain\b", r"\bexplique\w*", r"\bc'est quoi\b", r"\bwhat is an?\b", r"\bwhat are\b",
    r"\bqu'est-ce qu\w*", r"\bwho (was|is)\b", r"\bqui (est|etait)\b", r"\bdefin\w*",
    r"\bsummari[sz]e this\b", r"\bresume ce\b",
)
_PERSONAL = _rx(
    r"\bmy\b", r"\bmine\b", r"\bi\b", r"\bi'm\b", r"\bme\b", r"\bmon\b", r"\bma\b", r"\bmes\b", r"\bmoi\b",
    r"\bje\b", r"\bj'", r"\bdyal(i|y)\b", r"\b3ndi\b", r"\bana\b", "ديالي", "عندي", "أنا",
)
_QUESTION = _rx(
    r"\bcombien\b", r"\bhow (much|many)\b", r"\bquand\b", r"\bwhen\b", r"\bimta\b", r"\bwach\b", r"\bfin\b",
    r"\bwhat did\b", r"\bqu'est-ce que j\b", "امتى", "واش", "فين",
)
_GREETING = _rx(
    r"^(hi|hello|hey|salut|bonjour|bonsoir|salam|slm|cc|coucou|merci|thanks|thank you|ok|okay|cool|"
    r"labas|la bas|ca va|cava|how are you|good morning|good night)\b",
)


@dataclass(frozen=True)
class Decision:
    route: Route
    reason: str
    domains: tuple[str, ...] = field(default_factory=tuple)


def classify(text: str) -> Decision:
    """Cheap routing of one user message: ``data``, ``general`` or ``ambiguous`` (ask the LLM)."""
    folded = fold(text)
    if not folded:
        return Decision("general", "empty")
    topic = bool(_GENERAL_TOPIC.search(folded))
    general = topic or bool(_GENERAL_ASK.search(folded))
    found = [name for name, rx in _DOMAINS.items() if rx.search(folded)]
    if "write" not in found and _WRITE_VERB.search(folded) and _WRITE_OBJECT.search(folded):
        found.append("write")
    if "memory" not in found and not general and _REMEMBER_VERB.search(folded):
        found.append("memory")
    domains = tuple(found)
    personal = bool(_PERSONAL.search(folded))
    question = bool(_QUESTION.search(folded))
    words = len(folded.split())
    if domains:
        # "a bread recipe, I bought flour" or "explain what a budget is" name a data word inside a
        # general request: let the LLM decide ("c'est quoi mon solde" stays data).
        if topic or (general and not personal):
            return Decision("ambiguous", "keyword+general", domains)
        return Decision("data", "keyword:" + ",".join(domains), domains)
    if general:
        return Decision("general", "general-keyword")
    if words <= 4 and _GREETING.search(folded):
        return Decision("general", "greeting")
    if personal and (question or words >= 3):
        return Decision("ambiguous", "personal")
    return Decision("general", "default")


FOLLOW_UP_WORDS = 8


def classify_turn(text: str, previous: str | None = None, previous_route: str | None = None) -> Decision:
    """:func:`classify` plus follow-ups: a short message after a data turn ("et le mois dernier ?",
    "mark it done") that would default to general is ``ambiguous`` (the routing LLM sees both)."""
    decision = classify(text)
    if decision.route != "general" or decision.reason != "default":
        return decision
    if previous_route is not None:
        after_data = previous_route == "data"
    else:
        after_data = bool(previous) and classify(previous or "").route == "data"
    if after_data and len(fold(text).split()) <= FOLLOW_UP_WORDS:
        return Decision("ambiguous", "follow-up")
    return decision


def parse_route_answer(answer: str | None) -> Route | None:
    """The routing LLM's one-word answer (``DATA`` / ``GENERAL``), or None when unreadable."""
    word = fold(answer or "")
    if re.search(r"\bdata\b", word):
        return "data"
    if re.search(r"\bgeneral\b", word):
        return "general"
    return None


# --------------------------------------------------------------------------- language

_FR_WORDS = frozenset(
    "le la les des est je tu vous mon ma mes quoi combien pour avec une un du et pas que qui sur dans ce cette "
    "ajoute j'ai bonjour merci oui non quand comment pourquoi wach chhal dyal bghit 3ndi khassni kifach daba".split()
)
_EN_WORDS = frozenset(
    "the is are what my how you and to of for with a an in on it this that i me do did when why add please "
    "hello thanks yes no".split()
)


def detect_language(text: str) -> Literal["ar", "fr", "en"]:
    """Arabic script -> ar; otherwise French (or Latin-script Darija) vs English by common words."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return "en"
    arabic = sum(1 for c in letters if "؀" <= c <= "ۿ")
    if arabic / len(letters) > 0.3:
        return "ar"
    folded = fold(text)
    if re.search(r"[éèêàçùûôîœ]", (text or "").lower()):
        return "fr"
    words = re.findall(r"[a-z0-9']+", folded)
    fr = sum(1 for w in words if w in _FR_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    return "fr" if fr > en else "en"


# --------------------------------------------------------------------------- memory relevance

_STOP = frozenset(
    "the and for with that this from have has are was were you your mine about into what when where which "
    "les des une est pas que qui pour avec dans sur par mon mes ton tes son ses nos vos leur aux ces cette "
    "elle ils nous vous mais donc car tres plus moins tout tous fait faire etre avoir suis sont comme".split()
)


def keywords(text: str) -> set[str]:
    """Folded content words (3+ letters, stop words dropped), long words cut to a 6-letter stem."""
    out: set[str] = set()
    for word in re.findall(r"[\w']+", fold(text)):
        word = word.strip("'")
        if "'" in word:
            word = word.split("'")[-1]
        if len(word) < 3 or word in _STOP or word.isdigit():
            continue
        out.add(word[:6])
    return out


# Only how to answer (language, tone, length) is a preference: it goes with general turns too, so
# anything about the owner's life ("je préfère Marjane", a name) stays a plain fact.
_LANG = r"(darija|francais|anglais|arabe|english|french|arabic|spanish|espagnol)"
_PREFERENCE = _rx(
    r"\b(je prefere|i prefer|i'd prefer)\b.{0,40}\b(reponses?|repondre|reponde|answers?|repl(y|ies)|langue|language)\b",
    rf"\b(je prefere|i prefer|i'd prefer) (le |l'|en |in )?{_LANG}\b", rf"\b(en|in) {_LANG}\b",
    r"\breponds?(-moi)? (toujours )?(en|in)\b", r"\b(answer|reply|respond|talk)( to me)? (always )?in\b",
    r"\btutoie\w*", r"\bvouvoie\w*", r"\b(short|brief|concise|longer|detailed) (answers|replies)\b",
    r"\breponses? (courtes?|breves?|detaillees?)\b", "بالدارجة", "جاوبني",
)


def memory_kind(fact: str) -> Literal["fact", "preference"]:
    """``preference`` when the fact says how the owner wants answers (language, tone, length, name)."""
    return "preference" if _PREFERENCE.search(fold(fact)) else "fact"


def select_memories(query: str, memories: Sequence[dict[str, Any]], *, data: bool, limit: int = 10) -> list[dict[str, Any]]:
    """What one turn may see: answer-style preferences always; other facts (pinned or not) only on
    data turns, pinned first then by keyword overlap with ``query``."""
    prefs = [m for m in memories if m.get("kind") == "preference"]
    if not data:
        return prefs[:limit]
    others = rank_memories(query, [m for m in memories if m.get("kind") != "preference"], limit)
    return [*prefs, *others][:limit]


def rank_memories(query: str, memories: Sequence[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """Pinned facts first, then facts sharing keywords with ``query`` (most overlap, newest first)."""
    words = keywords(query)
    scored: list[tuple[int, int, str, dict[str, Any]]] = []
    for m in memories:
        overlap = len(words & keywords(str(m.get("fact") or "")))
        pinned = 1 if m.get("pinned") else 0
        if not pinned and not overlap:
            continue
        scored.append((pinned, overlap, str(m.get("created_at") or ""), m))
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [m for *_rest, m in scored[:limit]]


# --------------------------------------------------------------------------- tools


def _strip_titles(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def openai_tool(name: str, description: str | None, input_schema: dict[str, Any]) -> dict[str, Any]:
    """An MCP tool as an OpenAI ``tools`` entry (schema titles dropped: they only cost tokens)."""
    params = _strip_titles(input_schema or {"type": "object", "properties": {}})
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return {"type": "function", "function": {"name": name, "description": (description or "")[:900], "parameters": params}}


def parse_tool_args(raw: Any) -> dict[str, Any]:
    """Tool-call arguments (a JSON string or an object) as a dict; anything else is ``{}``."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def parse_plan(plan: dict[str, Any] | None, allowed: Iterable[str], max_calls: int) -> list[tuple[str, dict[str, Any]]]:
    """The ``{"tools": [{"name", "args"}]}`` answer of the plan protocol, filtered and capped."""
    names = set(allowed)
    out: list[tuple[str, dict[str, Any]]] = []
    items = (plan or {}).get("tools") or (plan or {}).get("calls") or []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("tool") or "")
        if name not in names:
            continue
        out.append((name, parse_tool_args(item.get("args") or item.get("arguments"))))
        if len(out) >= max_calls:
            break
    return out


def compact_json(value: Any, max_chars: int = 6000) -> str:
    """Compact JSON for the model's context, cut at ``max_chars`` with a visible marker."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 15] + "…(truncated)"


def result_summary(name: str, result: Any) -> str:
    """A few words for the PWA chip, e.g. ``5 tasks`` or ``12 expenses · 2026-10``."""
    if isinstance(result, dict):
        if result.get("error"):
            return f"error: {str(result['error'])[:80]}"
        if name == "ask_drive":
            sources = result.get("sources") or []
            return f"{len(sources)} source{'s' if len(sources) != 1 else ''}"
        parts = [f"{len(v)} {k}" for k, v in result.items() if isinstance(v, list)][:2]
        if isinstance(result.get("month"), str):
            parts.append(result["month"])
        if parts:
            return " · ".join(parts)
        if result.get("title"):
            return str(result["title"])[:80]
        return "ok"
    if isinstance(result, list):
        return f"{len(result)} items"
    return "ok"


def _fmt_amount(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def write_summary(name: str, args: dict[str, Any]) -> str:
    """What a write tool will create, in one line for the confirmation card."""
    a = args or {}
    if name == "tasks_add":
        line = f'Task "{a.get("title") or "?"}"'
        when = " ".join(str(a[k]) for k in ("due", "due_time") if a.get(k))
        if when:
            line += f" · {when}"
        if a.get("priority") and a["priority"] != "normal":
            line += f" · {a['priority']} priority"
        return line
    if name == "expense_add":
        line = f"Expense {_fmt_amount(a.get('total'))} {a.get('currency') or 'MAD'}"
        extras = [str(a[k]) for k in ("merchant", "category", "date") if a.get(k)]
        return line + (" · " + " · ".join(extras) if extras else "")
    if name == "income_add":
        line = f"Income {_fmt_amount(a.get('amount'))} {a.get('currency') or 'MAD'}"
        extras = [str(a[k]) for k in ("source", "date") if a.get(k)]
        if a.get("recurring"):
            extras.append("monthly")
        return line + (" · " + " · ".join(extras) if extras else "")
    if name == "notes_create":
        title = a.get("title") or next((ln.strip("# ").strip() for ln in str(a.get("body") or "").splitlines() if ln.strip()), "")
        line = f'Note "{(title or "untitled")[:80]}"'
        return line + (f" · {a['notebook']}" if a.get("notebook") else "")
    if name == "calendar_add_event":
        line = f'Event "{a.get("title") or "?"}"'
        return line + (f" · {a['start']}" if a.get("start") else "")
    if name == REMEMBER_TOOL:
        return f"Remember: {str(a.get('fact') or '')[:120]}"
    return name


def fallback_answer(used: Sequence[dict[str, Any]]) -> str:
    """The reply when the final LLM call fails after tools ran: what was created, proposed, saved
    and read, so the owner does not resend (and duplicate) a write that already happened."""
    created = [u["summary"] for u in used if u.get("name") in WRITE_TOOLS and not u.get("pending") and u.get("ok")]
    proposed = [u["summary"] for u in used if u.get("pending")]
    saved = [u["summary"] for u in used if u.get("name") == REMEMBER_TOOL and u.get("ok") and not u.get("pending")]
    read = [f"{u['name']} ({u['summary']})" for u in used if u.get("name") in READ_TOOLS]
    lines = ["I ran out of time writing the full answer."]
    if created:
        lines.append("Created: " + "; ".join(created) + " (do not send it again).")
    if proposed:
        lines.append("Confirm to create: " + "; ".join(proposed) + ".")
    if saved:
        lines.append("; ".join(saved) + ".")
    if read:
        lines.append("Looked at: " + "; ".join(read) + ". Ask again for the details.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- PDF text


def pdf_context(pages: Sequence[tuple[int | None, str]], budget: int = 24_000) -> tuple[str, bool]:
    """Page-marked text of a document within ``budget`` characters; ``(text, truncated)``.

    Short documents go in whole. Long ones keep the beginning of every page (each page gets an
    equal share, at least 400 characters); when even that does not fit, the first pages and the
    last two are kept and the middle is replaced by an ``[... pages a-b omitted ...]`` marker.
    """
    items = [(n if n is not None else i + 1, (t or "").strip()) for i, (n, t) in enumerate(pages)]
    items = [(n, t) for n, t in items if t]
    if not items:
        return "", False
    full = "\n\n".join(f"[Page {n}]\n{t}" for n, t in items)
    if len(full) <= budget:
        return full, False
    min_share = 400
    keep = items
    omitted: tuple[int, int] | None = None
    if len(items) * (min_share + 12) > budget:
        head_count = max(1, budget // (min_share + 12) - 2)
        keep = items[:head_count] + items[-2:]
        omitted = (items[head_count][0], items[-3][0]) if len(items) - 2 > head_count else None
    share = max(min_share, budget // len(keep) - 12)
    parts: list[str] = []
    for i, (n, t) in enumerate(keep):
        cut = t if len(t) <= share else t[: share - 1].rstrip() + "…"
        parts.append(f"[Page {n}]\n{cut}")
        if omitted and i == len(keep) - 3:
            parts.append(f"[... pages {omitted[0]}-{omitted[1]} omitted ...]")
    return "\n\n".join(parts)[: budget + 200], True

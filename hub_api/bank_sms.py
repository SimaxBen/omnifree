"""Bank SMS parsing (Moroccan banks and wallets): pure functions, no I/O.

:func:`parse_sms` turns a forwarded SMS into a :class:`ParsedSms`: what happened (card payment or
direct debit = ``expense``, incoming transfer = ``income``, ATM withdrawal = ``withdrawal``), the
amount and currency, the merchant or payer, the date, the card's last digits and the available
balance when the bank gives it. OTPs, codes and ads are ``ignored``; messages from a bank that the
regexes cannot read are ``unknown`` (the router may then ask an LLM).

The regexes are keyword-driven rather than one template per bank: CIH, Attijariwafa, Bank of Africa,
Banque Populaire, Crédit Agricole, Al Barid Bank, CDM, Saham/Société Générale, CFG and the wallets
word their alerts differently but with the same vocabulary (paiement, achat, retrait GAB/DAB,
virement reçu/émis, prélèvement, solde...). French, English and the common Arabic words are covered.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

__all__ = [
    "BANKS", "ParsedSms", "parse_sms", "parse_amount", "bank_for", "looks_like_bank", "mask_text",
    "ignore_reason", "guess_category",
]

Kind = Literal["expense", "income", "withdrawal", "ignored", "unknown"]

# (display name used for the account, sender codes matched as whole tokens, names matched anywhere)
BANKS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("CIH Bank", ("CIH", "CIHBANK"), ("cih bank", "cihbank")),
    ("Attijariwafa bank", ("AWB", "ATTIJARI", "ATTIJARIWAFA"), ("attijariwafa", "attijari")),
    ("Bank of Africa", ("BMCE", "BOA", "BANKOFAFRICA", "BMCEBANK"), ("bank of africa", "bmce")),
    ("Banque Populaire", ("BCP", "CHAABI", "BPOP", "POPULAIRE", "CHAABIBANK"), ("banque populaire", "chaabi")),
    ("Crédit Agricole du Maroc", ("CAM", "CREDITAGRI", "CAMAROC"), ("credit agricole",)),
    ("Al Barid Bank", ("ABB", "ALBARID", "BARIDBANK", "ALBARIDBANK"), ("al barid bank", "albarid bank", "barid bank")),
    ("Crédit du Maroc", ("CDM", "CREDITDUMAROC"), ("credit du maroc",)),
    ("Saham Bank", ("SGMA", "SAHAM", "SAHAMBANK", "SG"), ("saham bank", "societe generale")),
    ("CFG Bank", ("CFG", "CFGBANK"), ("cfg bank",)),
    ("Barid Cash", ("BARIDCASH",), ("barid cash",)),
    ("Orange Money", ("ORANGEMONEY",), ("orange money",)),
    ("inwi money", ("INWIMONEY",), ("inwi money",)),
    ("Wafacash", ("WAFACASH",), ("wafacash",)),
)
WALLETS = frozenset({"Barid Cash", "Orange Money", "inwi money", "Wafacash"})

_CURRENCY = {
    "MAD": "MAD", "DH": "MAD", "DHS": "MAD", "DIRHAM": "MAD", "DIRHAMS": "MAD", "درهم": "MAD", "دراهم": "MAD",
    "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR", "€": "EUR", "USD": "USD", "$": "USD", "US$": "USD",
    "GBP": "GBP", "£": "GBP",
}
_CUR = r"(?:MAD|DHS?|DIRHAMS?|EUROS?|EUR|USD|US\$|GBP|€|\$|£|درهم|دراهم)"
_NUM = r"\d{1,3}(?:[   .,']\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
AMOUNT_RE = re.compile(
    rf"(?:(?P<c1>{_CUR})\s*(?P<n1>-?\s?(?:{_NUM}))(?![\d])|(?<![\d*xX•])(?P<n2>-?(?:{_NUM}))\s*(?P<c2>{_CUR})(?![A-Za-z]))",
    re.I,
)
_BALANCE_RE = re.compile(r"(?:solde|balance|رصيد|sold)\b", re.I)

_OTP_RE = re.compile(
    r"\b(?:otp|one[- ]time|mot de passe|password|code (?:de )?(?:confirmation|verification|securite|validation|d'activation|"
    r"secret|otp)|votre code|your code|code\s*:|3d ?secure|saisir le code|afin d'authentifier|pour authentifier|ne (?:le )?communiquez|ne (?:le )?partagez|do not share)\b|"
    r"رمز|كود|القن السري",
    re.I,
)
_AD_RE = re.compile(
    r"\b(?:profitez|offre|promo|promotion|gagnez|felicitations|decouvrez|jeu concours|souscrivez|"
    r"remise|reduction|bonus|cadeau|black friday|nouveau service|telechargez)\b",
    re.I,
)
# "OPERATION SUR BMCE DIRECT DE 500,00 MAD AU PROFIT DE X COMPTE 0113... CODE DE CONFIRMATION : 293440"
_ONLINE_TRANSFER_RE = re.compile(
    rf"\boperation sur [a-z ]*direct de (?P<amt>{_NUM})\s*(?P<cur>{_CUR})\s+au profit de (?P<name>[a-z' .-]+?)"
    r"\s+compte\b.*\bcode de confirmation\b",
    re.I | re.S,
)
_REJECTED_RE = re.compile(r"\b(?:rejete|refuse|declined|rejected|echoue|failed|non abouti)\b", re.I)
_WITHDRAWAL_RE = re.compile(r"\b(?:retrait|withdrawal|cash ?withdraw\w*|gab|dab|atm)\b|سحب", re.I)
_CREDIT_RE = re.compile(
    r"\b(?:vir(?:ement)?\.?\s*(?:re[cç]u|recu|entrant|en votre faveur)|virement de\b.*\ben votre faveur|"
    r"(?:a|est|ete)\s+(?:ete\s+)?credite|creditee?|credit(?:e|ed)? (?:de|of|with)|received|credited|"
    r"transfert re[cç]u|versement|depot|remboursement|refund|mise a disposition re[cç]ue|"
    r"vous avez re[cç]u|reception d'un virement|you have received)\b|توصلتم|تحويل وارد|إيداع|ايداع",
    re.I,
)
_DEBIT_RE = re.compile(
    r"\b(?:paiement|payement|achat|purchase|payment|paid|debite|debitee|debited|prelevement|"
    r"vir(?:ement)?\.?\s*(?:emis|sortant|effectue)|transfert (?:emis|envoye)|operation (?:par|carte|de paiement)|"
    r"op\.? carte|e-?commerce|transaction (?:par )?carte|transaction|utilisation de votre carte|debit)\b|"
    r"شراء|أداء|اداء|خصم",
    re.I,
)
_DIRECT_DEBIT_RE = re.compile(r"\bprelevement\b", re.I)
_TRANSFER_OUT_RE = re.compile(r"\b(?:vir(?:ement)?\.?\s*(?:emis|sortant|effectue)|transfert (?:emis|envoye))\b", re.I)
_REFUND_RE = re.compile(r"\b(?:remboursement|refund|annulation)\b", re.I)
_SALARY_RE = re.compile(r"\b(?:salaire|salary|paie|traitement|remuneration)\b", re.I)

_STOP = (
    r"(?=\s+(?:le|on|du|en date|pour|avec|par carte|d'un|d un|de\s+-?\d|via|ref|réf|solde|sold|balance|carte|card|"
    r"a\s+\d|à\s+\d|at\s+\d|a bien|a ete|est|بتاريخ|يوم|بمبلغ)\b|\s+\d{1,2}[/.-]\d{1,2}|\s+-?\d[\d  .,]*\s*" + _CUR + r"|\s*[,;!()\n]|\.\s|\.$|\s+-\s|$)"
)
_MERCHANT_RES = (
    re.compile(rf"\b(?:chez|aupr[eè]s de|at|au profit de|en faveur de|b[eé]n[eé]ficiaire\s*:?|merchant\s*:?|commer[cç]ant\s*:?)\s+(?P<m>[^\n,;]+?){_STOP}", re.I),
    re.compile(rf"\b(?:a|à)\s+(?P<m>[A-Z][A-Z0-9&'._ -]{{2,}}?){_STOP}"),
    re.compile(rf"(?:لدى|عند|لفائدة)\s+(?P<m>[^\n,;]+?){_STOP}"),
)
_PAYER_RES = (
    re.compile(rf"\b(?:vir(?:ement)?\.?\s*(?:re[cç]u|recu|entrant)\s+(?:de\s+(?:la part de\s+)?)?|de la part de|from|[eé]mis par|ordonnateur\s*:?|donneur d'ordre\s*:?|exp[eé]diteur\s*:?)\s*(?P<m>[^\n,;]+?){_STOP}", re.I),
    re.compile(rf"{_CUR}\s+(?:de|from)\s+(?P<m>[^\n,;]+?){_STOP}", re.I),
)
# A pick-up code for a cash transfer ("code de retrait: 123456") is neither a withdrawal nor an OTP.
_PICKUP_CODE_RE = re.compile(r"\bcode (?:de )?(?:retrait|transfert|transaction)\s*:?\s*[\w-]{3,}", re.I)
_CODE_VALUE_RE = re.compile(r"(\bcode[^:\d\n]{0,25}:?\s*)[\w-]{4,12}", re.I)
_DATE_RE = re.compile(r"(?<![\d*])(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?(?![\d])")
_CARD_RE = re.compile(
    r"(?:[*xX•]{2,}[\s-]?(\d{4})\b|\b(?:carte|card|cb)\b[^\d\n]{0,30}?(?:\d{4,6}[*xX•\s-]+)?(\d{4})\b)", re.I
)
_LONG_NUMBER_RE = re.compile(r"\b\d(?:[ -]?[\dxX*•]){10,22}\b")


@dataclass
class ParsedSms:
    kind: Kind
    bank: str | None = None
    amount: float | None = None
    currency: str = "MAD"
    merchant: str = ""
    date: str | None = None
    card: str | None = None
    balance: float | None = None
    balance_currency: str | None = None
    category: str = "other"
    reason: str | None = None
    parser: str = "regex"
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("extra", None)
        return out


def _plain(text: str) -> str:
    """Lowercase-insensitive matching copy: accents removed, whitespace collapsed (same length is not kept)."""
    norm = unicodedata.normalize("NFKD", text)
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch) or ord(ch) > 0x600)
    return re.sub(r"[ \t  ]+", " ", norm)


def parse_amount(raw: str) -> float | None:
    """'1 234,56' / '1.234,56' / '1,234.56' / '245,50' / '12000.00' -> float."""
    t = re.sub(r"[\s  ']", "", raw or "")
    neg = t.startswith("-")
    t = t.lstrip("-")
    if not t or not re.search(r"\d", t):
        return None
    seps = [i for i, ch in enumerate(t) if ch in ".,"]
    if seps:
        last = seps[-1]
        decimals = len(t) - last - 1
        if decimals in (1, 2) or (len(seps) == 1 and decimals != 3):
            t = re.sub(r"[.,]", "", t[:last]) + "." + t[last + 1 :]
        else:
            t = re.sub(r"[.,]", "", t)
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if neg else value


def _currency(raw: str | None) -> str:
    return _CURRENCY.get((raw or "").strip().upper(), _CURRENCY.get((raw or "").strip(), "MAD"))


def _amounts(text: str) -> list[tuple[int, int, float, str]]:
    out = []
    for m in AMOUNT_RE.finditer(text):
        num = m.group("n1") or m.group("n2")
        value = parse_amount(num or "")
        if value is None:
            continue
        out.append((m.start(), m.end(), value, _currency(m.group("c1") or m.group("c2"))))
    return out


def bank_for(sender: str | None, text: str = "") -> str | None:
    tokens = set(re.findall(r"[A-Z0-9]+", re.sub(r"[\s_.-]+", "", (sender or "").upper()))) | set(
        re.findall(r"[A-Z0-9]+", (sender or "").upper())
    )
    hay = _plain(f"{sender or ''} {text}").lower()
    for name, codes, _names in BANKS:
        if tokens & set(codes):
            return name
    for name, _codes, names in BANKS:
        if any(n in hay for n in names):
            return name
    return None


def looks_like_bank(sender: str | None, text: str) -> bool:
    if bank_for(sender, text):
        return True
    return bool(re.search(r"\b(?:banque|bank|carte|card|compte|account|solde|virement)\b", _plain(text), re.I))


def ignore_reason(text: str) -> str | None:
    plain = _plain(text)
    if _OTP_RE.search(plain):
        return "one-time code"
    if _REJECTED_RE.search(plain):
        return "payment rejected (nothing was debited)"
    has_tx = _WITHDRAWAL_RE.search(plain) or _CREDIT_RE.search(plain) or _DEBIT_RE.search(plain)
    if _AD_RE.search(plain) and not has_tx:
        return "advertising"
    return None


def mask_text(text: str) -> str:
    """Card and account numbers keep their last 4 digits only."""

    def mask(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return f"****{digits[-4:]}" if len(digits) >= 4 else m.group(0)

    return _CODE_VALUE_RE.sub(lambda m: f"{m.group(1)}****", _LONG_NUMBER_RE.sub(mask, text))


_CATEGORY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("groceries", ("marjane", "carrefour", "aswak", "bim ", "label vie", "acima", "atacadao", "supermarche", "hanout")),
    ("food", ("glovo", "mcdonald", "kfc", "burger", "pizza", "cafe", "coffee", "restaurant", "snack", "starbucks", "jumia food", "patisserie")),
    ("transport", ("taxi", "careem", "uber", "bolt", "heetch", "indrive", "yango", "oncf", "ctm", "afriquia", "total", "shell", "winxo", "petrom", "station", "autoroute", "tramway", "parking")),
    ("bills", ("inwi", "orange", "maroc telecom", "iam", "lydec", "redal", "amendis", "onee", "radeema", "facture", "recharge", "claude", "anthropic", "openai", "google", "apple.com", "icloud", "microsoft")),
    ("entertainment", ("netflix", "spotify", "deezer", "youtube", "playstation", "steam", "cinema", "megarama", "shahid")),
    ("health", ("pharmacie", "pharmacy", "clinique", "clinic", "docteur", "laboratoire", "dentist")),
    ("shopping", ("jumia", "amazon", "aliexpress", "temu", "shein", "zara", "decathlon", "h&m", "lc waikiki", "ikea", "electroplanet")),
    ("travel", ("booking", "airbnb", "ryanair", "royal air maroc", "ram ", "easyjet", "hotel", "airarabia", "air arabia")),
    ("education", ("udemy", "coursera", "ecole", "school", "universite", "librairie")),
    ("housing", ("loyer", "syndic", "rent")),
)


def guess_category(merchant: str, text: str = "") -> str:
    hay = f" {_plain(merchant).lower()} "
    for cat, words in _CATEGORY_WORDS:
        if any(w in hay for w in words):
            return cat
    if _DIRECT_DEBIT_RE.search(_plain(text)):
        return "bills"
    return "other"


def _clean_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip(" .:-*'\"()")
    name = re.sub(r"\s+(?:le|du|on|a|à|at)$", "", name, flags=re.I)
    name = _LONG_NUMBER_RE.sub("", name).strip()
    name = re.split(r"\s*>", name)[0]
    # Payment-processor tails: "Spotify P4685E6A8B" (reference), "Glovo_pz" (terminal suffix).
    name = re.sub(r"\s+(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,}$", "", name)
    name = re.sub(r"_[A-Za-z0-9]{1,4}$", "", name)
    return name.strip(" .:-*'\"()")[:80]


def _first(patterns: tuple[re.Pattern[str], ...], text: str) -> str:
    for pat in patterns:
        for m in pat.finditer(text):
            name = _clean_name(m.group("m"))
            if name and not AMOUNT_RE.fullmatch(name) and re.search(r"[A-Za-z؀-ۿ]", name):
                return name
    return ""


def _date(text: str, today: date, received: date | None) -> str:
    fallback = received or today
    for m in _DATE_RE.finditer(text):
        d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (1 <= d <= 31 and 1 <= mth <= 12):
            continue
        year = int(y) + (2000 if y and len(y) == 2 else 0) if y else fallback.year
        try:
            found = date(year, mth, d)
        except ValueError:
            continue
        if not y and found > today:
            found = found.replace(year=found.year - 1)
        if found > today or found < fallback - timedelta(days=400):
            continue
        return found.isoformat()
    return fallback.isoformat()


def parse_sms(sender: str | None, text: str, today: date, received: datetime | None = None) -> ParsedSms:
    """Read one bank SMS. ``received`` (local time) is the date used when the text has none."""
    raw = (text or "").strip()
    bank = bank_for(sender, raw)
    if not raw:
        return ParsedSms("ignored", bank, reason="empty message")
    transfer = _ONLINE_TRANSFER_RE.search(_plain(raw))
    if transfer:
        # BMCE Direct only texts the confirmation code of an online transfer, never an "executed" SMS:
        # the owner chose to record it as spent right away (the router skips transfers to themselves).
        name = re.sub(r"\s+", " ", transfer.group("name")).strip().title()
        return ParsedSms(
            "expense", bank, amount=round(abs(parse_amount(transfer.group("amt")) or 0.0), 2),
            currency=_currency(transfer.group("cur")), merchant=f"Virement à {name}"[:80],
            date=(received.date() if received else today).isoformat(), category="other",
            extra={"beneficiary": name, "confirmation_code": True},
        )
    reason = ignore_reason(raw)
    if reason:
        return ParsedSms("ignored", bank, reason=reason)
    plain = _PICKUP_CODE_RE.sub("", _plain(raw))

    amounts = _amounts(plain)
    balance: float | None = None
    balance_cur: str | None = None
    tx_amounts = []
    for start, _end, value, cur in amounts:
        before = plain[max(0, start - 45) : start]
        if _BALANCE_RE.search(before) and not re.search(r"\b(?:montant|de|of|amount)\s*:?\s*$", before, re.I):
            if balance is None:
                balance, balance_cur = value, cur
            continue
        tx_amounts.append((start, value, cur))

    if _WITHDRAWAL_RE.search(plain):
        kind: Kind = "withdrawal"
    elif _CREDIT_RE.search(plain) and not re.search(r"\b(?:debite|debited)\b", plain, re.I):
        kind = "income"
    elif _DEBIT_RE.search(plain):
        kind = "expense"
    else:
        kind = "unknown"

    if not tx_amounts:
        return ParsedSms("unknown", bank, balance=balance, balance_currency=balance_cur, reason="no amount found")
    _pos, amount, currency = tx_amounts[0]
    if kind == "unknown":
        return ParsedSms("unknown", bank, amount=abs(amount), currency=currency, balance=balance,
                         balance_currency=balance_cur, reason="no debit or credit keyword")

    received_day = received.date() if received else None
    parsed = ParsedSms(
        kind,
        bank,
        amount=round(abs(amount), 2),
        currency=currency,
        date=_date(plain, today, received_day),
        balance=balance,
        balance_currency=balance_cur,
    )
    card = _CARD_RE.search(raw)
    if card:
        parsed.card = card.group(1) or card.group(2)
    if kind == "income":
        parsed.merchant = _first(_PAYER_RES, plain) or ("Salaire" if _SALARY_RE.search(plain) else "")
        parsed.category = (
            "refund" if _REFUND_RE.search(plain) else "salary" if _SALARY_RE.search(plain) else "other"
        )
        if not parsed.merchant:
            parsed.merchant = "Virement reçu"
    elif kind == "expense":
        parsed.merchant = _first(_MERCHANT_RES, plain)
        if not parsed.merchant:
            parsed.merchant = (
                "Prélèvement" if _DIRECT_DEBIT_RE.search(plain) else "Virement émis" if _TRANSFER_OUT_RE.search(plain)
                else f"Carte {bank}" if bank else "Paiement carte"
            )
        parsed.category = guess_category(parsed.merchant, plain)
    else:
        parsed.merchant = _first(_MERCHANT_RES, plain) or "Retrait GAB"
        parsed.category = "cash"
    return parsed

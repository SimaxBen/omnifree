"""Merchant matching shared by the Money page (no I/O, no imports from the routers).

* :func:`merchant_key` turns what a bank SMS, an email receipt, a voice memo or a hand-typed name says
  about a merchant ("PRLV SEPA NETFLIX.COM 866-579", "Netflix", "ANTHROPIC* CLAUDE.AI") into one stable key
  ("netflix", "claude"), so repeated charges group together and a recurring charge can be matched with the
  expense that paid it;
* :func:`amount_close` is the "similar amount" rule (±15 % by default).
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["merchant_key", "amount_close", "AMOUNT_TOLERANCE", "KNOWN_SUBSCRIPTIONS"]

AMOUNT_TOLERANCE = 0.15

# Well-known subscription services: alias found in the merchant text -> key. The first match wins, so
# longer / more specific aliases come first.
_ALIASES: tuple[tuple[str, str], ...] = (
    ("google one", "google one"), ("googleone", "google one"), ("google storage", "google one"),
    ("youtube premium", "youtube premium"), ("youtubepremium", "youtube premium"), ("youtube music", "youtube premium"),
    ("google play", "google play"), ("apple com bill", "apple"), ("icloud", "icloud"), ("apple music", "apple music"),
    ("anthropic", "claude"), ("claude", "claude"), ("openai", "chatgpt"), ("chatgpt", "chatgpt"),
    ("netflix", "netflix"), ("spotify", "spotify"), ("deezer", "deezer"), ("disney", "disney plus"),
    ("prime video", "amazon prime"), ("amazon prime", "amazon prime"), ("primevideo", "amazon prime"),
    ("shahid", "shahid"), ("osn", "osn"), ("canal", "canal plus"), ("bein", "bein"),
    ("github", "github"), ("notion", "notion"), ("dropbox", "dropbox"), ("microsoft", "microsoft"),
    ("adobe", "adobe"), ("canva", "canva"), ("cursor", "cursor"), ("midjourney", "midjourney"),
    ("vercel", "vercel"), ("cloudflare", "cloudflare"), ("hetzner", "hetzner"), ("ovh", "ovh"),
    ("linkedin", "linkedin"), ("duolingo", "duolingo"), ("audible", "audible"), ("patreon", "patreon"),
    ("maroc telecom", "maroc telecom"), ("iam", "maroc telecom"), ("itissalat", "maroc telecom"),
    ("inwi", "inwi"), ("orange", "orange"), ("lydec", "lydec"), ("redal", "redal"), ("amendis", "amendis"),
    ("radeema", "radeema"), ("onee", "onee"), ("basic fit", "basic fit"), ("city club", "city club"),
)
KNOWN_SUBSCRIPTIONS = frozenset(key for _, key in _ALIASES)

# Words that say how a payment was made, not who was paid.
_NOISE = frozenset(
    "www com net org fr ma io ai co inc ltd llc sarl sa sas gmbh ab bv payment paiement pmt prlv sepa achat cb tpe "
    "card carte visa mastercard debit credit retrait purchase online internet web bill billing subscription "
    "abonnement abo mensuel monthly recurring premium plan the de du la le les et chez help pay ref txn trx".split()
)
_TOKEN_RE = re.compile(r"[a-z]+")


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


def _has_alias(tokens: list[str], alias: str) -> bool:
    """``alias`` as consecutive whole tokens; a long alias (6+ letters, spaces removed) may also start a token
    ("netflixcom", "googleonestorage"), a short one must be a whole token ("iam", not "william")."""
    words = alias.split()
    n = len(words)
    if any(tokens[i : i + n] == words for i in range(len(tokens) - n + 1)):
        return True
    glued = alias.replace(" ", "")
    return len(glued) >= 6 and any(t.startswith(glued) for t in tokens)


def merchant_key(name: str | None) -> str:
    """A stable grouping key for a merchant name ("" when nothing usable is left)."""
    if not name:
        return ""
    tokens = _TOKEN_RE.findall(_ascii(str(name)))
    for alias, key in _ALIASES:
        if _has_alias(tokens, alias):
            return key
    useful = [t for t in tokens if len(t) >= 2 and t not in _NOISE]
    if not useful or useful[0] in ("unknown", "inconnu"):
        return ""
    return " ".join(useful[:2])


def amount_close(a: float, b: float, tolerance: float = AMOUNT_TOLERANCE) -> bool:
    """``a`` within ``tolerance`` (relative) of ``b``; two zeros are close, a zero and a non-zero are not."""
    a, b = abs(float(a)), abs(float(b))
    if b == 0:
        return a == 0
    return abs(a - b) <= tolerance * b

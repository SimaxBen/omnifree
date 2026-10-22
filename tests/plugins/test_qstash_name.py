"""QStash job names travel in a forwarded header, which must be ASCII."""

from __future__ import annotations

from urllib.parse import quote, unquote

from plugins.compute_edge.qstash import _NAME_SAFE


def test_accented_names_round_trip_as_ascii() -> None:
    name = "Rappels des tâches (toutes les 3 h)"
    encoded = quote(name, safe=_NAME_SAFE)
    assert encoded.isascii() and unquote(encoded) == name
    assert quote("Quotas gratuits (7h + 19h Maroc)", safe=_NAME_SAFE) == "Quotas gratuits (7h + 19h Maroc)"

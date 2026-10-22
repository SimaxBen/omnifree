"""Receipt reading shared by ``/expenses/receipt`` and the Gmail import (``/inbox/receipts/sync``).

* :func:`read_receipt_file`: an image (or a PDF) sent to vision-capable LLM providers only
  (Capability.VISION, Gemini first, failover between them), answer parsed as JSON.
* :func:`read_receipt_text`: the text of an email (or of a PDF) sent to the normal LLM chain, with an
  extra ``is_receipt`` flag so newsletters and shipping notices can be told apart from purchases.

Both return the raw JSON object (or ``None``); :func:`hub_api.routers.expenses.parse_receipt`
validates it into expense fields.
"""

from __future__ import annotations

import base64
import logging
from datetime import date
from typing import Any

from core.interfaces import Capability, ChatMessage, ChatRequest, ChatResponse, ProviderKind
from core.router import TOKEN_METRIC
from hub_api.appdata import extract_json
from hub_api.state import HubState

__all__ = [
    "CATEGORIES",
    "VISION_PREFERRED",
    "RECEIPT_PROMPT",
    "TEXT_RECEIPT_PROMPT",
    "vision_candidates",
    "vision_chat",
    "receipt_vision_request",
    "read_receipt_file",
    "read_receipt_text",
]

log = logging.getLogger("omnifree.hub.receipts")

CATEGORIES = (
    "food", "groceries", "transport", "housing", "bills", "health",
    "shopping", "entertainment", "education", "travel", "other",
)
VISION_PREFERRED = "gemini"
TEXT_LIMIT = 12_000  # characters of email/PDF text sent to the LLM

RECEIPT_PROMPT = (
    "You read photos of receipts and invoices (often Moroccan: French/Arabic, amounts in dirhams 'DH'/'MAD').\n"
    "Today is {today}. Answer with ONE JSON object only, no prose, no code fences:\n"
    '{{"merchant": "store name", "date": "YYYY-MM-DD or null", "total": number (the grand total paid), '
    '"currency": "ISO 4217 code (DH/Dhs = MAD) or null", "category": one of {categories}, '
    '"items": [{{"name": "...", "qty": number or null, "price": number or null}}], '
    '"confidence": "high|medium|low", "text": "the printed text of the receipt, line by line, at most 1500 characters"}}\n'
    "Use null for anything unreadable; never invent amounts. confidence is low when the image is blurry, cut or not a receipt."
)

TEXT_RECEIPT_PROMPT = (
    "You read emails (order confirmations, receipts, invoices, payment notices; often French or English, "
    "Moroccan amounts in dirhams 'DH'/'MAD') and extract the purchase.\n"
    "Today is {today}. Answer with ONE JSON object only, no prose, no code fences:\n"
    '{{"is_receipt": true|false, "merchant": "store or company name", "date": "YYYY-MM-DD or null", '
    '"total": number (the grand total paid or charged), "currency": "ISO 4217 code (DH/Dhs = MAD) or null", '
    '"category": one of {categories}, "items": [{{"name": "...", "qty": number or null, "price": number or null}}], '
    '"confidence": "high|medium|low"}}\n'
    "is_receipt is false for newsletters, promotions, shipping updates without an amount, refunds, quotes and "
    "unpaid reminders. Use null for anything missing; never invent amounts."
)


def vision_candidates(state: HubState) -> list[str]:
    chain = state.registry.chain(ProviderKind.LLM)
    vision = [n for n in chain if Capability.VISION in state.registry.class_meta(n).get("capabilities", [])]
    if VISION_PREFERRED in vision:
        vision.remove(VISION_PREFERRED)
        vision.insert(0, VISION_PREFERRED)
    return vision


async def vision_chat(state: HubState, request: ChatRequest) -> ChatResponse:
    """Chat on vision-capable providers only (Gemini first), with failover between them."""
    routed = await state.router.execute(
        ProviderKind.LLM,
        lambda a: a.chat(request),
        candidates=vision_candidates(state),
        capability=Capability.VISION,
        op_name="chat",
    )
    resp: ChatResponse = routed.result
    resp.routed_via = resp.routed_via or routed.provider
    if resp.usage and resp.usage.total_tokens:
        await state.router.record_usage(routed.provider, TOKEN_METRIC, resp.usage.total_tokens)
    return resp


def receipt_vision_request(state: HubState, data: bytes, content_type: str, today: date, hint: str | None = None) -> ChatRequest:
    data_url = f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"
    text = "Extract this receipt as JSON."
    if hint:
        text += f"\nContext: {hint}"
    return ChatRequest(
        messages=[
            ChatMessage(
                role="system",
                content=RECEIPT_PROMPT.format(today=today.isoformat(), categories="|".join(CATEGORIES)),
            ),
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            ),
        ],
        model=state.hub.receipt_model or None,
        temperature=0.0,
        response_format={"type": "json_object"},
    )


async def read_receipt_file(
    state: HubState, data: bytes, content_type: str, today: date, hint: str | None = None
) -> dict[str, Any] | None:
    """Vision read of a receipt image (or PDF, which Gemini reads natively). ``None`` = no JSON."""
    resp = await vision_chat(state, receipt_vision_request(state, data, content_type, today, hint))
    parsed = extract_json(resp.content)
    if parsed is None:
        log.warning("receipt extraction returned no JSON (routed via %s)", resp.routed_via)
    return parsed


async def read_receipt_text(state: HubState, text: str, today: date, context: str = "") -> dict[str, Any] | None:
    """Text-LLM read of an email body / PDF text (any chat provider, normal failover)."""
    body = text.strip()[:TEXT_LIMIT]
    request = ChatRequest(
        messages=[
            ChatMessage(
                role="system",
                content=TEXT_RECEIPT_PROMPT.format(today=today.isoformat(), categories="|".join(CATEGORIES)),
            ),
            ChatMessage(role="user", content=(f"{context.strip()}\n\n" if context.strip() else "") + body),
        ],
        temperature=0.0,
        max_tokens=1200,
        response_format={"type": "json_object"},
    )
    resp = await state.router.chat(request)
    parsed = extract_json(resp.content)
    if parsed is None:
        log.warning("email receipt extraction returned no JSON (routed via %s)", resp.routed_via)
    return parsed

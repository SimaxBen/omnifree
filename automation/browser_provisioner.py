"""Guided, resumable browser provisioner for every OmniFree provider.

Opens a *headful* Chromium with a persistent profile (``data/browser-profile``) so
logins survive between runs, walks through the provider catalog
(:mod:`automation.provider_catalog`), and for each provider:

1. navigates to the API-key page (or sign-up page) and detects human gates
   (login, OTP/2FA, CAPTCHA, card entry). On a gate it pauses with
   "press Enter when done" so you finish it yourself;
2. tries to extract the key from the page (regex over visible text, input values
   and the URL, using stable selectors first) and asks for confirmation, or asks
   you to paste it (hidden input), or reads a downloaded file (OCI PEM, GCP JSON),
   or generates a random secret for self-hosted services;
3. stores each value into the encrypted vault immediately (``core.vault``);
4. rewrites ``PROVISIONING_STATUS.md`` (a checklist, never secret values) and the
   resumable state ``data/provisioning_state.json``;
5. finally runs the FreeLLMAPI key injector if one is present in the project.

It never types into card fields and never reads card inputs. Providers whose catalog entry
has ``card_required=True`` (Oracle, GCP, AWS, Cerebras trial, Koyeb) are announced up front, and
``optional=True`` entries (Cerebras trial, Koyeb, which is paid for new signups) can be skipped
with ``s`` at the first prompt.

CLI::

    python -m automation.browser_provisioner run [--only groq,gemini] [--category llm]
                                                [--force] [--no-browser] [--skip-billing]
    python -m automation.browser_provisioner status     # rewrite PROVISIONING_STATUS.md
    python -m automation.browser_provisioner list       # print the catalog
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import secrets as pysecrets
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from automation.provider_catalog import CATALOG, KeySpec, ProviderEntry, Step
from core.vault import PASSPHRASE_ENV, Vault, VaultError

__all__ = [
    "Gate",
    "PageSnapshot",
    "detect_gates",
    "extract_candidates",
    "mask",
    "ProvisioningState",
    "VaultStore",
    "render_status_markdown",
    "write_status_file",
    "find_injector",
    "run_injector",
    "Console",
    "TerminalConsole",
    "PlaywrightBrowser",
    "Provisioner",
    "main",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "data" / "browser-profile"
DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "provisioning_state.json"
DEFAULT_STATUS_PATH = PROJECT_ROOT / "PROVISIONING_STATUS.md"
INJECTOR_ENV = "OMNIFREE_FREELLMAPI_INJECTOR"
INJECTOR_ARGS_ENV = "OMNIFREE_FREELLMAPI_INJECTOR_ARGS"


# ====================================================================== page model + gate detection


class Gate(str, Enum):
    CARD = "card"
    CAPTCHA = "captcha"
    OTP = "otp"
    LOGIN = "login"


GATE_MESSAGES: dict[Gate, str] = {
    Gate.CARD: "Card entry detected. OmniFree never types or reads card data: enter it yourself in the browser.",
    Gate.CAPTCHA: "CAPTCHA / human verification detected: solve it in the browser.",
    Gate.OTP: "One-time code / 2FA detected: enter the code in the browser.",
    Gate.LOGIN: "Login or sign-up page detected: sign in (or create the account) in the browser.",
}


@dataclass
class PageSnapshot:
    """What the provisioner sees of a page (kept in memory only, never logged)."""

    url: str = ""
    title: str = ""
    text: str = ""
    inputs: list[dict[str, str]] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)

    @property
    def values(self) -> list[str]:
        return [i.get("value", "") for i in self.inputs if i.get("value")]


_CARD_FIELD = re.compile(r"card.?number|cardnumber|cc-?num|\bcvc\b|\bcvv\b|security.?code|expir", re.I)
_CARD_FRAME = re.compile(r"js\.stripe\.com|stripe\.com/.*(elements|checkout)|adyen|braintree|checkout\.com|"
                         r"paypal\.com/.*(card|checkout)|recurly|chargebee|cybersource|worldpay", re.I)
_CARD_TEXT = re.compile(r"(card number|credit card|debit card).{0,200}(cvv|cvc|expir|security code)", re.I | re.S)
_CAPTCHA_FRAME = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile|arkoselabs|funcaptcha|"
                            r"captcha", re.I)
_CAPTCHA_TEXT = re.compile(r"verify (that )?you are (a )?human|i'?m not a robot|complete the security check|"
                           r"press and hold", re.I)
_OTP_FIELD = re.compile(r"\botp\b|totp|one.?time|verification.?code|2fa|mfa|passcode|security.?code|sms.?code", re.I)
_OTP_TEXT = re.compile(r"verification code|enter the code|two.?factor|2.?step verification|"
                       r"one.?time (pass)?code|we (sent|emailed|texted) (you )?a code", re.I)
_LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|sign_in|auth|oauth|sso|signup|sign-up|register|"
                         r"accounts?/|identity/v1|u/login)", re.I)
_LOGIN_TEXT = re.compile(r"\b(sign in|log in|login|continue with google|continue with github|create (an |your )?"
                         r"account|sign up)\b", re.I)


def _field_blob(inp: dict[str, str]) -> str:
    return " ".join(inp.get(k, "") for k in ("name", "id", "placeholder", "autocomplete", "label"))


def detect_gates(snap: PageSnapshot) -> list[Gate]:
    """Return the human gates visible on the page, most blocking first."""
    gates: list[Gate] = []
    inputs = snap.inputs
    autocompletes = [i.get("autocomplete", "").lower() for i in inputs]

    card = (
        any(a.startswith("cc-") for a in autocompletes)
        or any(_CARD_FIELD.search(_field_blob(i)) and i.get("type", "") not in ("hidden",) for i in inputs
               if not _OTP_FIELD.search(_field_blob(i)))
        or any(_CARD_FRAME.search(f) for f in snap.frames)
        or bool(_CARD_TEXT.search(snap.text))
    )
    if card:
        gates.append(Gate.CARD)

    if any(_CAPTCHA_FRAME.search(f) for f in snap.frames) or _CAPTCHA_TEXT.search(snap.text):
        gates.append(Gate.CAPTCHA)

    otp = "one-time-code" in autocompletes or any(
        _OTP_FIELD.search(_field_blob(i)) and not _CARD_FIELD.search(i.get("autocomplete", "")) for i in inputs
    )
    if otp or (_OTP_TEXT.search(snap.text) and any(i.get("type") in ("text", "tel", "number") for i in inputs)):
        gates.append(Gate.OTP)

    has_password = any(i.get("type") == "password" for i in inputs)
    has_email = any(i.get("type") == "email" or "email" in _field_blob(i).lower() or
                    i.get("autocomplete") == "username" for i in inputs)
    path = urlparse(snap.url).path + ("?" + urlparse(snap.url).query if urlparse(snap.url).query else "")
    login = has_password or (has_email and (bool(_LOGIN_PATH.search(path)) or bool(_LOGIN_TEXT.search(snap.text[:5000]))))
    if login and Gate.OTP not in gates:
        gates.append(Gate.LOGIN)
    return gates


def extract_candidates(
    spec: KeySpec,
    snap: PageSnapshot,
    selector_texts: Sequence[str] = (),
    *,
    exclude: Iterable[str] = (),
) -> list[str]:
    """Distinct values matching ``spec.pattern``: selector texts first, then URL/inputs/page text."""
    rx = spec.regex()
    if rx is None:
        return []
    excluded = set(exclude)
    sources: list[str] = list(selector_texts)
    if spec.from_url:
        sources.insert(0, snap.url)
    if spec.source == "extract" or spec.from_url:
        sources.extend(snap.values)
        sources.append(snap.text)
    found: list[str] = []
    for blob in sources:
        for m in rx.finditer(blob or ""):
            value = m.group(0).strip()
            if value and value not in excluded and value not in found:
                found.append(value)
    return found


def mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 12:
        return value[:2] + "*" * max(len(value) - 2, 0)
    return f"{value[:6]}...{value[-4:]} ({len(value)} chars)"


# ====================================================================== console


class Console(Protocol):
    def print(self, message: str = "") -> None: ...
    def ask(self, prompt: str, default: str = "") -> str: ...
    def secret(self, prompt: str) -> str: ...


class TerminalConsole:
    """Blocking terminal prompts; the provisioner calls them through asyncio.to_thread."""

    def print(self, message: str = "") -> None:
        print(message, flush=True)

    def ask(self, prompt: str, default: str = "") -> str:
        try:
            answer = input(prompt)
        except EOFError:
            return default
        return answer.strip() or default

    def secret(self, prompt: str) -> str:
        try:
            return getpass.getpass(prompt).strip()
        except EOFError:
            return ""


# ====================================================================== state + vault


_STATUSES = ("pending", "in_progress", "partial", "done", "skipped", "failed")


class ProvisioningState:
    """Resumable progress, persisted as JSON (no secret values)."""

    def __init__(self, path: Path, data: dict[str, Any] | None = None) -> None:
        self.path = path
        self.data: dict[str, Any] = data or {"version": 1, "providers": {}}

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH) -> ProvisioningState:
        if path.exists():
            try:
                return cls(path, json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                backup = path.with_suffix(".corrupt.json")
                path.replace(backup)
        return cls(path)

    def provider(self, pid: str) -> dict[str, Any]:
        return self.data["providers"].setdefault(pid, {"status": "pending", "keys": {}, "billing": "pending"})

    def status(self, pid: str) -> str:
        return self.provider(pid)["status"]

    def set_status(self, pid: str, status: str, detail: str = "") -> None:
        if status not in _STATUSES:
            raise ValueError(status)
        entry = self.provider(pid)
        entry["status"] = status
        entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if detail:
            entry["detail"] = detail
        else:
            entry.pop("detail", None)
        self.save()

    def set_key(self, pid: str, vault_key: str, status: str) -> None:
        self.provider(pid)["keys"][vault_key] = status
        self.save()

    def set_billing(self, pid: str, status: str) -> None:
        self.provider(pid)["billing"] = status
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


class VaultStore:
    """Opens the vault once and saves after every write (so a crash loses nothing)."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    @classmethod
    def open(cls, path: Path | None, passphrase: str | None, console: Console | None = None) -> VaultStore:
        passphrase = passphrase or os.environ.get(PASSPHRASE_ENV)
        vault_path = Vault(path).path
        if not passphrase:
            if console is None:
                raise VaultError(f"set {PASSPHRASE_ENV}")
            passphrase = console.secret("Vault passphrase: ")
            if not vault_path.exists() and console.secret("Repeat passphrase (new vault): ") != passphrase:
                raise VaultError("passphrases do not match")
        if not passphrase:
            raise VaultError("an empty vault passphrase is not allowed")
        vault = Vault.load(path, passphrase, missing_ok=True)
        if not vault.exists:
            vault.save()
        return cls(vault)

    def has(self, key: str) -> bool:
        return bool(self.vault.get(key))

    def put(self, key: str, value: str) -> None:
        self.vault.set(key, value)
        self.vault.save()

    def keys(self) -> set[str]:
        return {k for k, v in self.vault.items() if v}


# ====================================================================== status markdown

_CATEGORY_TITLES = {
    "llm": "LLM providers", "cloud": "Clouds", "database": "Databases", "storage": "Storage",
    "edge": "Edge / hosting / CI", "network": "Network", "local": "Self-hosted / generated",
}


def render_status_markdown(
    catalog: Sequence[ProviderEntry],
    state: ProvisioningState,
    present_keys: set[str],
    *,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    required = [k.vault_key for e in catalog for k in e.keys if not k.optional]
    required_unique = list(dict.fromkeys(required))
    have = sum(1 for k in required_unique if k in present_keys)
    done = sum(1 for e in catalog if _entry_complete(e, present_keys))
    lines = [
        "# OmniFree provisioning status",
        "",
        f"_Rewritten by `python -m automation.browser_provisioner` on {now.strftime('%Y-%m-%d %H:%M UTC')}. "
        "Secret values are never written here; they live in the encrypted `.env.vault`._",
        "",
        f"**Progress:** {done}/{len(catalog)} providers complete, {have}/{len(required_unique)} required keys in the vault.",
        "",
        "Resume any time: `python -m automation.browser_provisioner run` skips what is already stored "
        "(`--force` redoes it, `--only <id>` targets one provider).",
        "",
    ]
    for category, title in _CATEGORY_TITLES.items():
        entries = [e for e in catalog if e.category == category]
        if not entries:
            continue
        lines += [f"## {title}", ""]
        for entry in entries:
            complete = _entry_complete(entry, present_keys)
            st = state.provider(entry.id)
            status = "done" if complete else st.get("status", "pending")
            detail = f" ({st['detail']})" if st.get("detail") and not complete else ""
            plugins = f" - plugins: {', '.join(entry.plugins)}" if entry.plugins else ""
            lines.append(f"- [{'x' if complete else ' '}] **{entry.name}** `{entry.id}`{entry_tags(entry)}: "
                         f"{status}{detail}{plugins}")
            if entry.api_key_url and entry.api_key_url != "about:blank":
                lines.append(f"  - keys page: {entry.api_key_url}")
            for key in entry.keys:
                mark = "x" if key.vault_key in present_keys else " "
                opt = " (optional)" if key.optional else ""
                lines.append(f"  - [{mark}] `{key.vault_key}`: {key.label}{opt}")
            if entry.billing_url:
                bill = st.get("billing", "pending")
                how = "automated by `deploy.py budgets`" if entry.budget_automated else entry.billing_instructions
                lines.append(f"  - [{'x' if bill == 'done' else ' '}] billing / $1 alert: {how} ({entry.billing_url})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def entry_tags(entry: ProviderEntry) -> str:
    """Short flags shown next to a provider: optional / card required."""
    tags = []
    if entry.optional:
        tags.append("optional")
    if entry.card_required:
        tags.append("card required")
    return f" [{', '.join(tags)}]" if tags else ""


def _entry_complete(entry: ProviderEntry, present: set[str]) -> bool:
    return all(k.vault_key in present for k in entry.keys if not k.optional)


def write_status_file(
    path: Path, catalog: Sequence[ProviderEntry], state: ProvisioningState, present_keys: set[str]
) -> Path:
    text = render_status_markdown(catalog, state, present_keys)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ====================================================================== FreeLLMAPI injector

_INJECTOR_CANDIDATES = (
    "deploy/oracle-arm/inject_keys.py",
    "deploy/oracle-arm/freellmapi_inject.py",
    "deploy/oracle-arm/freellmapi_injector.py",
    "plugins/llm_pool/freellmapi_inject.py",
    "plugins/llm_pool/freellmapi_injector.py",
    "scripts/freellmapi_inject.py",
)
_SKIP_DIRS = {"node_modules", ".git", "tests", "__pycache__", "data", ".venv", "venv", "dist"}


def find_injector(root: Path = PROJECT_ROOT) -> Path | None:
    """Locate the FreeLLMAPI key injector shipped by the llm_pool builder, if any."""
    override = os.environ.get(INJECTOR_ENV)
    if override:
        path = Path(override)
        path = path if path.is_absolute() else root / path
        return path if path.is_file() else None
    for rel in _INJECTOR_CANDIDATES:
        if (root / rel).is_file():
            return root / rel
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if any(p in _SKIP_DIRS for p in rel_parts):
            continue
        name = path.stem.lower()
        if "freellmapi" in name and "inject" in name:
            return path
    return None


def injector_command(path: Path, root: Path = PROJECT_ROOT) -> list[str]:
    """``python -m pkg.mod`` when the file lives inside packages (so imports work), else the script path."""
    rel = path.relative_to(root) if path.is_relative_to(root) else None
    if rel is not None and len(rel.parts) > 1:
        pkg_dirs = [root.joinpath(*rel.parts[:i]) for i in range(1, len(rel.parts))]
        if all((d / "__init__.py").is_file() for d in pkg_dirs):
            return [sys.executable, "-m", ".".join(rel.with_suffix("").parts)]
    return [sys.executable, str(path)]


def injector_arg_sets(has_dashboard_token: bool) -> list[list[str]]:
    """``plan`` (validates which vault keys will reach FreeLLMAPI); ``push`` too when a dashboard token exists.

    ``OMNIFREE_FREELLMAPI_INJECTOR_ARGS`` (shell-style) replaces this with a single custom invocation.
    The compose ``render`` step needs the ARM Tailscale IP, so ``automation.deploy deploy-arm`` runs it.
    """
    custom = os.environ.get(INJECTOR_ARGS_ENV)
    if custom:
        return [shlex.split(custom)]
    sets = [["plan"]]
    if has_dashboard_token:
        sets.append(["push", "--only-enabled"])
    return sets


def run_injector(
    root: Path = PROJECT_ROOT,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    extra_args: Sequence[str] = (),
) -> int | None:
    """Run the injector (inherits the env, incl. the vault passphrase). None when absent."""
    path = find_injector(root)
    if path is None:
        return None
    cmd = injector_command(path, root) + list(extra_args)
    result = runner(cmd, cwd=str(root), check=False)
    return int(result.returncode)


# ====================================================================== browser


_SNAPSHOT_JS = r"""
() => {
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const labelOf = (e) => { const l = e.labels && e.labels[0]; return l ? l.innerText : (e.getAttribute('aria-label') || ''); };
  const sensitive = (e) => {
    const ac = (e.autocomplete || '').toLowerCase();
    const blob = [e.name, e.id, e.placeholder, ac].join(' ');
    return e.type === 'password' || ac.startsWith('cc-') || /card.?number|cvc|cvv|expir/i.test(blob);
  };
  const inputs = [...document.querySelectorAll('input, textarea')].filter(visible).map((e) => ({
    type: (e.type || 'text').toLowerCase(), name: e.name || '', id: e.id || '',
    autocomplete: (e.autocomplete || '').toLowerCase(), placeholder: e.placeholder || '',
    label: labelOf(e), value: sensitive(e) ? '' : (e.value || ''),
  }));
  const frames = [...document.querySelectorAll('iframe')].map((f) => f.src || '');
  return { url: location.href, title: document.title,
           text: (document.body ? document.body.innerText : '').slice(0, 300000), inputs, frames };
}
"""


class PlaywrightBrowser:
    """Headful Chromium with a persistent profile. Playwright is imported lazily."""

    def __init__(self, profile_dir: Path = DEFAULT_PROFILE_DIR, *, channel: str | None = None) -> None:
        self.profile_dir = profile_dir
        self.channel = channel
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is not installed: pip install playwright && python -m playwright install chromium"
            ) from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        kwargs: dict[str, Any] = {"headless": False, "viewport": None, "accept_downloads": True}
        if self.channel:
            kwargs["channel"] = self.channel
        self._context = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._pw is not None:
            await self._pw.stop()
        self._context = self._pw = self._page = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise RuntimeError("browser not started")
        if self._page.is_closed():
            self._page = self._context.pages[-1] if self._context.pages else None
        return self._page

    async def goto(self, url: str) -> None:
        if not url or url == "about:blank":
            return
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await self.page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception as exc:  # navigation timeouts on SPAs are normal: keep the page as is
            if type(exc).__name__ not in ("TimeoutError", "Error"):
                raise

    async def snapshot(self) -> PageSnapshot:
        page = self.page
        try:
            raw = await page.evaluate(_SNAPSHOT_JS)
        except Exception:
            raw = {"url": page.url, "title": "", "text": "", "inputs": [], "frames": []}
        frames = list(raw.get("frames") or []) + [f.url for f in page.frames if f.url]
        return PageSnapshot(url=raw.get("url") or page.url, title=raw.get("title", ""),
                            text=raw.get("text", ""), inputs=list(raw.get("inputs") or []), frames=frames)

    async def selector_texts(self, selectors: Sequence[str]) -> list[str]:
        texts: list[str] = []
        for sel in selectors:
            try:
                for handle in await self.page.query_selector_all(sel):
                    value = await handle.evaluate("(e) => (e.value !== undefined && e.value) ? e.value : e.innerText")
                    if value:
                        texts.append(str(value))
            except Exception:
                continue
        return texts


class BrowserLike(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def goto(self, url: str) -> None: ...
    async def snapshot(self) -> PageSnapshot: ...
    async def selector_texts(self, selectors: Sequence[str]) -> list[str]: ...


# ====================================================================== provisioner


def validate_file_content(spec: KeySpec, text: str) -> str | None:
    """Return an error message, or None when the content is acceptable."""
    if spec.validator == "service_account_json":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            return f"not valid JSON: {exc}"
        if doc.get("type") != "service_account" or not doc.get("private_key") or not doc.get("client_email"):
            return "not a service account key (expected type=service_account, private_key, client_email)"
    elif spec.validator == "pem_private_key":
        if "-----BEGIN" not in text or "PRIVATE KEY-----" not in text:
            return "not a PEM private key"
    return None


class Provisioner:
    def __init__(
        self,
        vault: VaultStore,
        state: ProvisioningState,
        console: Console,
        browser: BrowserLike | None,
        *,
        catalog: Sequence[ProviderEntry] = CATALOG,
        status_path: Path = DEFAULT_STATUS_PATH,
        force: bool = False,
        skip_billing: bool = False,
        retry_skipped: bool = False,
        max_gate_rounds: int = 6,
        root: Path = PROJECT_ROOT,
    ) -> None:
        self.vault = vault
        self.state = state
        self.console = console
        self.browser = browser
        self.catalog = list(catalog)
        self.status_path = status_path
        self.force = force
        self.skip_billing = skip_billing
        self.retry_skipped = retry_skipped
        self.max_gate_rounds = max_gate_rounds
        self.root = root

    # ---------------------------------------------------------------- console helpers (thread offload)
    async def _say(self, message: str = "") -> None:
        self.console.print(message)

    async def _ask(self, prompt: str, default: str = "") -> str:
        return await asyncio.to_thread(self.console.ask, prompt, default)

    async def _secret(self, prompt: str) -> str:
        return await asyncio.to_thread(self.console.secret, prompt)

    async def pause(self, message: str) -> str:
        return (await self._ask(f"{message} -> press Enter when done ('s' to skip): ")).lower()

    def write_status(self) -> Path:
        return write_status_file(self.status_path, self.catalog, self.state, self.vault.keys())

    # ---------------------------------------------------------------- top level
    async def run(self, entries: Sequence[ProviderEntry] | None = None, *, run_injector_after: bool = True) -> dict[str, str]:
        entries = list(entries) if entries is not None else self.catalog
        results: dict[str, str] = {}
        self.write_status()
        for entry in entries:
            results[entry.id] = await self.provision_entry(entry)
        self.write_status()
        if run_injector_after:
            await self._run_injector()
        return results

    async def _run_injector(self) -> None:
        path = find_injector(self.root)
        if path is None:
            await self._say("FreeLLMAPI injector not found; skipping key injection.")
            return
        shown = path.relative_to(self.root) if path.is_relative_to(self.root) else path
        for args in injector_arg_sets(self.vault.has("FREELLMAPI_DASHBOARD_TOKEN")):
            await self._say(f"Running FreeLLMAPI injector: {shown} {' '.join(args)}")
            code = await asyncio.to_thread(run_injector, self.root, extra_args=args)
            await self._say(f"FreeLLMAPI injector exited with code {code}.")
            if code != 0:
                break

    async def provision_entry(self, entry: ProviderEntry) -> str:
        missing = [k for k in entry.keys if self.force or not self.vault.has(k.vault_key)]
        required_missing = [k for k in missing if not k.optional]
        prior = self.state.status(entry.id)
        if not self.force and prior == "skipped" and not self.retry_skipped:
            return "skipped"
        if not self.force and not required_missing and prior == "done":
            return "done"
        if not missing and not self.force:
            self.state.set_status(entry.id, "done")
            await self._billing(entry)
            self.write_status()
            return "done"

        await self._say("")
        await self._say(f"=== {entry.name} ({entry.id}){entry_tags(entry)} ===")
        if entry.notes:
            await self._say(entry.notes)
        if entry.card_required:
            await self._say("This provider asks for a card at signup. OmniFree never types or reads card data.")
        if entry.optional:
            await self._say("Optional provider: answer 's' at the first prompt to skip it.")
        self.state.set_status(entry.id, "in_progress")
        self.write_status()
        try:
            for step in entry.steps:
                outcome = await self._run_step(entry, step)
                if outcome == "skip_provider":
                    self.state.set_status(entry.id, "skipped", "skipped by user")
                    self.write_status()
                    return "skipped"
            await self._billing(entry)
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.state.set_status(entry.id, "partial", "interrupted")
            self.write_status()
            raise
        except VaultError:
            raise
        except Exception as exc:  # keep going with the next provider; the state records why
            self.state.set_status(entry.id, "failed", f"{type(exc).__name__}: {exc}"[:300])
            self.write_status()
            await self._say(f"!! {entry.name} failed: {exc}")
            return "failed"
        complete = all(self.vault.has(k.vault_key) for k in entry.keys if not k.optional)
        status = "done" if complete else "partial"
        self.state.set_status(entry.id, status, "" if complete else "some required keys missing")
        self.write_status()
        return status

    async def _run_step(self, entry: ProviderEntry, step: Step) -> str:
        keys = [k for k in step.keys if self.force or not self.vault.has(k.vault_key)]
        if not keys:
            return "ok"
        needs_page = any(k.source in ("extract", "paste", "file") for k in keys)
        await self._say(f"-- {step.title}: {step.instructions}")
        if needs_page and step.url and step.url != "about:blank":
            if self.browser is not None:
                await self.browser.goto(step.url)
                await self._clear_gates(entry, step)
            else:
                await self._say(f"   Open: {step.url}")
                if entry.signup_url:
                    await self._say(f"   (no account yet? sign up at {entry.signup_url})")
            answer = await self.pause("   Follow the instructions above until the key is visible / downloaded")
            if answer == "s":
                return "skip_provider"
            if self.browser is not None:
                await self._clear_gates(entry, step)
        used: list[str] = []
        for key in keys:
            value = await self.obtain(key, used)
            if value is None:
                self.state.set_key(entry.id, key.vault_key, "skipped")
                continue
            self.vault.put(key.vault_key, value)
            used.append(value)
            self.state.set_key(entry.id, key.vault_key, "stored")
            await self._say(f"   stored {key.vault_key} in the vault")
            self.write_status()
        return "ok"

    async def _clear_gates(self, entry: ProviderEntry, step: Step) -> None:
        assert self.browser is not None
        target_host = urlparse(step.url).netloc
        previous: list[Gate] | None = None
        for _ in range(self.max_gate_rounds):
            snap = await self.browser.snapshot()
            gates = detect_gates(snap)
            if not gates:
                return
            if gates == previous:
                choice = (await self._ask(
                    f"   Still seeing {', '.join(g.value for g in gates)} (may be a false positive). "
                    "[c]ontinue anyway or [w]ait? [c] ", "c")).lower()
                if not choice.startswith("w"):
                    return
            previous = gates
            for gate in gates:
                await self._say(f"   [{gate.value}] {GATE_MESSAGES[gate]}")
            if Gate.LOGIN in gates and entry.signup_url:
                await self._say(f"   No account yet? Sign up at {entry.signup_url}")
            answer = await self.pause("   Complete it in the browser")
            if answer == "s":
                return
            after = await self.browser.snapshot()
            if Gate.LOGIN in gates and not detect_gates(after) and urlparse(after.url).netloc != target_host:
                await self.browser.goto(step.url)
        await self._say("   Gate still detected; continuing anyway (paste manually if needed).")

    async def obtain(self, key: KeySpec, used: Sequence[str] = ()) -> str | None:
        label = f"{key.label} [{key.vault_key}]" + (" (optional)" if key.optional else "")
        if key.source == "generate":
            choice = (await self._ask(f"   {label}: [g]enerate a random value, [p]aste your own, [s]kip? [g] ", "g")).lower()
            if choice == "s":
                return None
            if choice == "p":
                return await self._paste(key, label)
            if key.generate_format == "hex":
                return pysecrets.token_hex(key.generate_bytes)
            return pysecrets.token_urlsafe(key.generate_bytes)
        if key.source == "file":
            return await self._from_file(key, label)

        candidates: list[str] = []
        if self.browser is not None and (key.source == "extract" or key.selectors or key.from_url):
            snap = await self.browser.snapshot()
            texts = await self.browser.selector_texts(key.selectors) if key.selectors else []
            candidates = extract_candidates(key, snap, texts, exclude=used)
        if len(candidates) == 1:
            ok = (await self._ask(f"   Found {label}: {mask(candidates[0])}. Store it? [Y/n] ", "y")).lower()
            if ok.startswith("y"):
                return candidates[0]
        elif len(candidates) > 1:
            await self._say(f"   Several candidates for {label}:")
            for i, c in enumerate(candidates, 1):
                await self._say(f"     {i}) {mask(c)}")
            pick = await self._ask("   Number to store, or Enter to paste manually: ", "")
            if pick.isdigit() and 1 <= int(pick) <= len(candidates):
                return candidates[int(pick) - 1]
        return await self._paste(key, label)

    async def _paste(self, key: KeySpec, label: str) -> str | None:
        for _ in range(3):
            value = await self._secret(f"   Paste {label} (hidden; Enter to skip): ")
            if not value:
                if key.optional:
                    return None
                again = (await self._ask("   Empty value. [r]etry or [s]kip this key? [r] ", "r")).lower()
                if again == "s":
                    return None
                continue
            if not key.matches(value):
                keep = (await self._ask(f"   That does not look like {key.label} (expected /{key.pattern}/). "
                                        "Store anyway? [y/N] ", "n")).lower()
                if not keep.startswith("y"):
                    continue
            return value
        return None

    async def _from_file(self, key: KeySpec, label: str) -> str | None:
        for _ in range(3):
            raw = await self._ask(f"   Path to {label} (Enter to skip): ", "")
            if not raw:
                return None
            path = Path(raw.strip().strip('"').strip("'")).expanduser()
            if not path.is_file():
                await self._say(f"   {path} does not exist")
                continue
            text = path.read_text(encoding="utf-8").strip()
            error = validate_file_content(key, text)
            if error:
                await self._say(f"   {error}")
                continue
            if key.validator == "service_account_json":
                text = json.dumps(json.loads(text), separators=(",", ":"))
            return text
        return None

    async def _billing(self, entry: ProviderEntry) -> None:
        if not entry.billing_url or self.skip_billing:
            return
        if self.state.provider(entry.id).get("billing") == "done" and not self.force:
            return
        if entry.budget_automated:
            await self._say(f"   Billing: {entry.billing_instructions}")
            self.state.set_billing(entry.id, "automated")
            return
        await self._say(f"-- Billing / spending alert: {entry.billing_instructions}")
        if self.browser is not None:
            await self.browser.goto(entry.billing_url)
        else:
            await self._say(f"   Open: {entry.billing_url}")
        answer = await self.pause("   Configure the alert/cap yourself (never typed by OmniFree)")
        self.state.set_billing(entry.id, "skipped" if answer == "s" else "done")


# ====================================================================== CLI


def select_entries(only: str | None, category: str | None, catalog: Sequence[ProviderEntry] = CATALOG) -> list[ProviderEntry]:
    entries = list(catalog)
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        by_id = {e.id: e for e in entries}
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            raise SystemExit(f"unknown provider id(s): {', '.join(unknown)} (see: list)")
        entries = [by_id[w] for w in wanted]
    if category:
        entries = [e for e in entries if e.category == category]
    return entries


def _print_catalog(catalog: Sequence[ProviderEntry]) -> None:
    for e in catalog:
        keys = ", ".join(k.vault_key + ("?" if k.optional else "") for k in e.keys)
        print(f"{e.id:14} {e.category:9} {e.name}{entry_tags(e)}")
        print(f"{'':25}keys: {keys}")
        if e.signup_url:
            print(f"{'':25}signup: {e.signup_url}")
        if e.api_key_url and e.api_key_url != "about:blank":
            print(f"{'':25}api keys: {e.api_key_url}")
        if e.billing_url:
            print(f"{'':25}billing: {e.billing_url}")


async def _run_cli(args: argparse.Namespace) -> int:
    console = TerminalConsole()
    state = ProvisioningState.load(Path(args.state))
    status_path = Path(args.status_file)
    vault_path = Path(args.vault) if args.vault else None
    if args.cmd == "status":
        present: set[str] = set()
        if Vault(vault_path).exists:
            present = VaultStore.open(vault_path, None, console).keys()
        write_status_file(status_path, CATALOG, state, present)
        print(f"wrote {status_path}")
        return 0
    vault = VaultStore.open(vault_path, None, console)
    entries = select_entries(args.only, args.category)
    browser: PlaywrightBrowser | None = None
    if not args.no_browser:
        browser = PlaywrightBrowser(Path(args.profile), channel=args.channel)
        await browser.start()
    prov = Provisioner(vault, state, console, browser, status_path=status_path, force=args.force,
                       skip_billing=args.skip_billing, retry_skipped=bool(args.only))
    try:
        results = await prov.run(entries, run_injector_after=not args.no_injector)
    finally:
        if browser is not None:
            await browser.close()
    print("")
    for pid, status in results.items():
        print(f"{pid:14} {status}")
    print(f"\nChecklist: {status_path}")
    return 0 if all(s in ("done", "skipped") for s in results.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m automation.browser_provisioner",
                                     description="Guided provider sign-up and API key collection into the vault.")
    parser.add_argument("--vault", help="vault path (default: OMNIFREE_VAULT_PATH or .env.vault)")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--status-file", default=str(DEFAULT_STATUS_PATH))
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="provision providers (default)")
    p_run.add_argument("--only", help="comma-separated provider ids")
    p_run.add_argument("--category", choices=sorted(_CATEGORY_TITLES))
    p_run.add_argument("--force", action="store_true", help="redo providers/keys already stored")
    p_run.add_argument("--no-browser", action="store_true", help="print URLs and ask for pastes only")
    p_run.add_argument("--skip-billing", action="store_true")
    p_run.add_argument("--no-injector", action="store_true", help="do not run the FreeLLMAPI injector at the end")
    p_run.add_argument("--profile", default=str(DEFAULT_PROFILE_DIR))
    p_run.add_argument("--channel", help="browser channel, e.g. chrome or msedge (default: bundled chromium)")
    sub.add_parser("status", help="rewrite PROVISIONING_STATUS.md from the vault + state")
    sub.add_parser("list", help="print the provider catalog")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args([*argv, "run"])
    if args.cmd == "list":
        _print_catalog(CATALOG)
        return 0
    try:
        return asyncio.run(_run_cli(args))
    except VaultError as exc:
        print(f"vault error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; progress is saved, run again to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

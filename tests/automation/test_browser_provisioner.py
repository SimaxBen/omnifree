from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from automation import browser_provisioner as bp
from automation.browser_provisioner import (
    Gate,
    PageSnapshot,
    Provisioner,
    ProvisioningState,
    VaultStore,
    detect_gates,
    extract_candidates,
    find_injector,
    injector_command,
    mask,
    render_status_markdown,
    run_injector,
)
from automation.provider_catalog import CATALOG, KeySpec, catalog_by_id
from core.vault import Vault
from tests.automation.fakes import FakeBrowser, ScriptedConsole, write_file

ROOT = Path(__file__).resolve().parents[2]
GROQ_KEY = "gsk_" + "A1b2C3d4" * 6 + "Zz"


# ---------------------------------------------------------------------- catalog


def test_catalog_covers_every_secret_in_providers_yaml() -> None:
    doc = yaml.safe_load((ROOT / "config" / "providers.yaml").read_text(encoding="utf-8"))
    catalog_keys = {k.vault_key for e in CATALOG for k in e.keys}
    missing = []
    for name, spec in doc["plugins"].items():
        for section in ("secrets", "optional_secrets"):
            values = spec.get(section) or {}
            keys = values.values() if isinstance(values, dict) else values
            missing += [f"{name}:{k}" for k in keys if k not in catalog_keys]
    assert missing == []


def test_catalog_lists_every_spec_provider_once() -> None:
    ids = [e.id for e in CATALOG]
    assert len(ids) == len(set(ids))
    for required in ("groq", "gemini", "mistral", "openrouter", "cloudflare", "nvidia_nim", "cohere", "sambanova",
                     "zai", "cerebras", "freellmapi", "oracle", "oracle_adb", "gcp", "aws", "supabase", "neon",
                     "turso", "mongodb_atlas", "upstash", "ibm_cloudant", "backblaze_b2", "koyeb", "vercel",
                     "netlify", "github", "tailscale"):
        assert required in ids
    all_keys = [k.vault_key for e in CATALOG for k in e.keys]
    assert len(all_keys) == len(set(all_keys)), "a vault key is claimed by two providers"


def test_catalog_entries_are_well_formed() -> None:
    for entry in CATALOG:
        assert entry.steps, entry.id
        for step in entry.steps:
            assert step.url.startswith(("https://", "http://oracle-arm", "about:blank")), step.url
        if entry.signup_url:
            assert entry.signup_url.startswith("https://")
        if entry.billing_url:
            assert entry.billing_url.startswith("https://")
        if entry.budget_automated:
            assert entry.billing_url
        for key in entry.keys:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]+", key.vault_key)
            if key.source == "extract":
                assert key.pattern, key.vault_key
            if key.pattern:
                re.compile(key.pattern)


@pytest.mark.parametrize("key,value", [
    ("GROQ_API_KEY", GROQ_KEY),
    ("GEMINI_API_KEY", "AIza" + "x" * 35),
    ("OPENROUTER_API_KEY", "sk-or-v1-" + "ab12" * 16),
    ("NVIDIA_API_KEY", "nvapi-" + "Q" * 64),
    ("OCI_TENANCY_OCID", "ocid1.tenancy.oc1..aaaabbbbcccc"),
    ("OCI_FINGERPRINT", ":".join(["ab"] * 16)),
    ("TAILSCALE_API_KEY", "tskey-api-" + "kAbC123-XyZ"),  # split: a fake key, not for secret scanners
    ("AWS_ACCESS_KEY_ID", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("TURSO_DATABASE_URL", "libsql://omnifree-me.turso.io"),
])
def test_key_patterns_accept_real_formats(key: str, value: str) -> None:
    spec = next(k for e in CATALOG for k in e.keys if k.vault_key == key)
    assert spec.matches(value)
    assert not spec.matches("nope")


# ---------------------------------------------------------------------- gates + extraction


def test_detect_card_gate_from_autocomplete_and_frames() -> None:
    snap = PageSnapshot(url="https://x/billing", inputs=[{"type": "text", "autocomplete": "cc-number"}])
    assert detect_gates(snap)[0] is Gate.CARD
    stripe = PageSnapshot(url="https://x/pay", frames=["https://js.stripe.com/v3/elements-inner-card.html"])
    assert Gate.CARD in detect_gates(stripe)


def test_detect_captcha_otp_login() -> None:
    assert detect_gates(PageSnapshot(frames=["https://www.google.com/recaptcha/api2/anchor"])) == [Gate.CAPTCHA]
    otp = PageSnapshot(text="Enter the code we sent", inputs=[{"type": "text", "autocomplete": "one-time-code"}])
    assert detect_gates(otp) == [Gate.OTP]
    login = PageSnapshot(url="https://console.groq.com/login", text="Sign in",
                         inputs=[{"type": "email", "name": "email"}, {"type": "password", "name": "pw"}])
    assert detect_gates(login) == [Gate.LOGIN]


def test_key_page_has_no_gates() -> None:
    snap = PageSnapshot(url="https://console.groq.com/keys", text=f"API Keys\n{GROQ_KEY}\nCopy",
                        inputs=[{"type": "text", "name": "search", "value": ""}])
    assert detect_gates(snap) == []


def test_extract_candidates_text_url_and_exclude() -> None:
    groq = catalog_by_id()["groq"].keys[0]
    snap = PageSnapshot(text=f"Your key {GROQ_KEY} copy it now")
    assert extract_candidates(groq, snap) == [GROQ_KEY]
    assert extract_candidates(groq, snap, exclude=[GROQ_KEY]) == []
    acct = catalog_by_id()["cloudflare"].keys[0]
    cf = PageSnapshot(url="https://dash.cloudflare.com/0123456789abcdef0123456789abcdef/home")
    assert extract_candidates(acct, cf) == ["0123456789abcdef0123456789abcdef"]
    paste_only = KeySpec("X", "x", "paste", r"[a-z]{5}")
    assert extract_candidates(paste_only, PageSnapshot(text="hello world")) == []  # never scans text for pastes
    assert extract_candidates(paste_only, PageSnapshot(), ["abcde"]) == ["abcde"]  # selector hits are used


def test_mask_hides_secret() -> None:
    masked = mask(GROQ_KEY)
    assert GROQ_KEY not in masked and masked.startswith("gsk_A1") and masked.endswith(f"({len(GROQ_KEY)} chars)")


# ---------------------------------------------------------------------- provisioning flow


@pytest.fixture
def vault(tmp_path: Path) -> VaultStore:
    v = Vault(tmp_path / ".env.vault", "test-pass", kdf_params={"n": 2**10})
    v.save()
    return VaultStore(v)


def _prov(tmp_path: Path, vault: VaultStore, console: ScriptedConsole, browser: FakeBrowser | None,
          entries: list[str], **kw: object) -> Provisioner:
    catalog = [catalog_by_id()[i] for i in entries]
    return Provisioner(vault, ProvisioningState(tmp_path / "state.json"), console, browser, catalog=catalog,
                       status_path=tmp_path / "PROVISIONING_STATUS.md", root=tmp_path, **kw)  # type: ignore[arg-type]


async def test_groq_flow_login_gate_then_extract(tmp_path: Path, vault: VaultStore) -> None:
    login = PageSnapshot(url="https://console.groq.com/login", text="Sign in",
                         inputs=[{"type": "email"}, {"type": "password"}])
    keys = PageSnapshot(url="https://console.groq.com/keys", text=f"Created key {GROQ_KEY}")
    browser = FakeBrowser([login, keys])
    console = ScriptedConsole(answers=["", "", "y", ""])  # gate Enter, instructions Enter, confirm, billing Enter
    prov = _prov(tmp_path, vault, console, browser, ["groq"])
    results = await prov.run(run_injector_after=False)
    assert results == {"groq": "done"}
    assert vault.vault.get("GROQ_API_KEY") == GROQ_KEY
    assert browser.visited[0] == "https://console.groq.com/keys"
    assert "Login or sign-up" in console.output
    status = (tmp_path / "PROVISIONING_STATUS.md").read_text(encoding="utf-8")
    assert "[x] `GROQ_API_KEY`" in status and GROQ_KEY not in status
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["providers"]["groq"]["status"] == "done"
    assert GROQ_KEY not in (tmp_path / "state.json").read_text(encoding="utf-8")


async def test_card_gate_pauses_and_never_types(tmp_path: Path, vault: VaultStore) -> None:
    card = PageSnapshot(url="https://x/verify", text="Card number  CVV  Expiry",
                        inputs=[{"type": "text", "autocomplete": "cc-number"}])
    keys = PageSnapshot(url="https://console.groq.com/keys", text=f"{GROQ_KEY}")
    browser = FakeBrowser([card, keys])
    console = ScriptedConsole(answers=["", "", "y", "s"])
    prov = _prov(tmp_path, vault, console, browser, ["groq"])
    await prov.run(run_injector_after=False)
    assert "never types or reads card data" in console.output
    assert not hasattr(browser, "fill") and not hasattr(browser, "type")
    assert vault.has("GROQ_API_KEY")


async def test_paste_flow_validates_pattern(tmp_path: Path, vault: VaultStore) -> None:
    console = ScriptedConsole(answers=["", "n"], secrets=["not-a-key", GROQ_KEY])
    prov = _prov(tmp_path, vault, console, None, ["groq"], skip_billing=True)
    assert await prov.run(run_injector_after=False) == {"groq": "done"}
    assert vault.vault.get("GROQ_API_KEY") == GROQ_KEY
    assert "Open: https://console.groq.com/keys" in console.output


async def test_file_source_validates_service_account(tmp_path: Path, vault: VaultStore) -> None:
    bad = write_file(tmp_path / "bad.json", '{"type": "user"}')
    good = write_file(tmp_path / "sa.json", json.dumps({"type": "service_account", "project_id": "p",
                                                        "private_key": "-----BEGIN PRIVATE KEY-----\n", "client_email": "a@b"}))
    console = ScriptedConsole(answers=["", str(bad), str(good)])
    prov = _prov(tmp_path, vault, console, None, ["gcp"], skip_billing=True)
    assert await prov.run(run_injector_after=False) == {"gcp": "done"}
    assert json.loads(vault.vault.get("GCP_SERVICE_ACCOUNT_JSON") or "{}")["project_id"] == "p"
    assert "not a service account key" in console.output


async def test_generated_secrets_and_resume(tmp_path: Path, vault: VaultStore) -> None:
    console = ScriptedConsole(answers=["g", "g", "s"], secrets=[""])
    prov = _prov(tmp_path, vault, console, None, ["freellmapi"], skip_billing=True)
    # encryption key generated, admin email skipped (empty paste), admin password generated, then 's' skips step 2
    result = await prov.run(run_injector_after=False)
    key = vault.vault.get("FREELLMAPI_ENCRYPTION_KEY") or ""
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert vault.has("FREELLMAPI_ADMIN_PASSWORD")
    assert result == {"freellmapi": "skipped"}

    # A completed provider is not prompted again.
    done_console = ScriptedConsole()
    vault.put("GROQ_API_KEY", GROQ_KEY)
    prov2 = _prov(tmp_path, vault, done_console, None, ["groq"], skip_billing=True)
    assert await prov2.run(run_injector_after=False) == {"groq": "done"}
    assert done_console.prompts == []


def test_status_markdown_counts(tmp_path: Path) -> None:
    state = ProvisioningState(tmp_path / "s.json")
    text = render_status_markdown(CATALOG, state, {"GROQ_API_KEY"})
    assert text.startswith("# OmniFree provisioning status")
    assert "- [x] **Groq** `groq`: done" in text
    assert "- [ ] **Google AI Studio (Gemini)**" in text
    assert "1/" in text.split("**Progress:**")[1].split("\n")[0]


# ---------------------------------------------------------------------- injector


def test_find_and_run_injector_as_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bp.INJECTOR_ENV, raising=False)
    assert find_injector(tmp_path) is None
    for d in ("plugins", "plugins/llm_pool"):
        write_file(tmp_path / d / "__init__.py", "")
    inj = write_file(tmp_path / "plugins" / "llm_pool" / "freellmapi_injector.py", "print('hi')\n")
    assert find_injector(tmp_path) == inj
    cmd = injector_command(inj, tmp_path)
    assert cmd[1:] == ["-m", "plugins.llm_pool.freellmapi_injector"]
    calls: list[list[str]] = []

    def runner(argv: list[str], cwd: str, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    assert run_injector(tmp_path, runner=runner, extra_args=["plan"]) == 0
    assert calls[0][-1] == "plan"
    assert bp.injector_arg_sets(False) == [["plan"]]
    assert bp.injector_arg_sets(True)[1][0] == "push"


def test_real_project_injector_is_discovered() -> None:
    found = find_injector(ROOT)
    if (ROOT / "plugins" / "llm_pool" / "freellmapi_injector.py").exists():
        assert found is not None and found.name == "freellmapi_injector.py"


def test_cli_list_prints_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    assert bp.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "groq" in out and "https://console.groq.com/keys" in out

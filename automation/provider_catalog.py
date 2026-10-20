"""Provider catalog for the guided browser provisioner.

Every cloud and LLM provider of the OmniFree spec, with:

* where to sign up, where API keys are created, where billing/budget alerts live;
* the vault keys each provider fills (names match ``config/providers.yaml`` exactly);
* how each key is obtained: extracted from the page (regex over visible text, input
  values and, for account ids, the page URL), pasted by the user, read from a downloaded
  file, or generated locally.

URLs, card requirements and key formats follow ``docs/provider_research.json`` (checked
2026-10-19); they are best-known values and drift over time. When a URL 404s the
provisioner still works: the user navigates manually and pastes. Regexes are only used
where the key format has a stable, documented prefix; otherwise the user pastes the key.
Selectors are hints tried first; they never type anything into the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

__all__ = ["KeySpec", "Step", "ProviderEntry", "CATALOG", "catalog_by_id", "all_vault_keys", "Source"]

Source = Literal["extract", "paste", "file", "generate"]
Validator = Literal["service_account_json", "pem_private_key", "url", "none"]


@dataclass(frozen=True)
class KeySpec:
    vault_key: str
    label: str
    source: Source = "paste"
    pattern: str | None = None  # regex for extraction / validation (no anchors)
    selectors: tuple[str, ...] = ()  # CSS selectors tried first for extraction
    from_url: bool = False  # extract the pattern from the current page URL
    optional: bool = False
    validator: Validator = "none"
    generate_bytes: int = 32  # for source == "generate"
    generate_format: Literal["urlsafe", "hex"] = "urlsafe"

    def regex(self) -> re.Pattern[str] | None:
        return re.compile(self.pattern) if self.pattern else None

    def matches(self, value: str) -> bool:
        rx = self.regex()
        return True if rx is None else bool(rx.fullmatch(value.strip()))


@dataclass(frozen=True)
class Step:
    """One page to visit and the keys expected there."""

    title: str
    url: str
    instructions: str
    keys: tuple[KeySpec, ...] = ()


@dataclass(frozen=True)
class ProviderEntry:
    id: str
    name: str
    category: Literal["llm", "cloud", "database", "storage", "edge", "network", "local"]
    signup_url: str | None
    steps: tuple[Step, ...]
    billing_url: str | None = None
    billing_instructions: str = ""
    plugins: tuple[str, ...] = ()  # providers.yaml entries this provider feeds
    budget_automated: bool = False  # automation/deploy.py budgets creates the $1 alert by API
    notes: str = ""
    optional: bool = False  # provider not needed for a minimal setup
    card_required: bool | None = None  # True: signup/free tier needs a card; None: unknown / not applicable
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def api_key_url(self) -> str | None:
        return self.steps[0].url if self.steps else None

    @property
    def keys(self) -> tuple[KeySpec, ...]:
        return tuple(k for s in self.steps for k in s.keys)


_JWT = r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"

CATALOG: tuple[ProviderEntry, ...] = (
    # ------------------------------------------------------------------ LLM upstreams
    ProviderEntry(
        id="groq", name="Groq", category="llm",
        signup_url="https://console.groq.com/login",
        steps=(Step("API key", "https://console.groq.com/keys",
                    "Click 'Create API Key', name it 'omnifree', then leave the key visible on screen.",
                    (KeySpec("GROQ_API_KEY", "Groq API key", "extract", r"gsk_[A-Za-z0-9]{20,}",
                             ("input[readonly]", "code", "[data-testid*='key']")),)),),
        billing_url="https://console.groq.com/settings/billing",
        billing_instructions="Stay on the free plan; do not add a card.",
        plugins=("groq",),
        card_required=False,
    ),
    ProviderEntry(
        id="gemini", name="Google AI Studio (Gemini)", category="llm",
        signup_url="https://aistudio.google.com/",
        steps=(Step("API key", "https://aistudio.google.com/apikey",
                    "Click 'Create API key' (pick or create a project), then keep the key dialog open.",
                    (KeySpec("GEMINI_API_KEY", "Gemini API key", "extract", r"AIza[0-9A-Za-z_\-]{35}",
                             ("input[readonly]", "code")),)),),
        billing_url="https://console.cloud.google.com/billing/budgets",
        billing_instructions="Keep the project on the free tier (no Cloud Billing linked to it). Per-model limits "
                             "are only shown under your project in AI Studio; free-tier prompts may be used by Google.",
        plugins=("gemini",),
        card_required=False,
    ),
    ProviderEntry(
        id="mistral", name="Mistral La Plateforme", category="llm",
        signup_url="https://console.mistral.ai/",
        steps=(
            Step("API key", "https://console.mistral.ai/api-keys",
                 "Choose the free 'Experiment' plan if asked, then 'Create new key'. Keep the key visible.",
                 (KeySpec("MISTRAL_API_KEY", "Mistral API key", "paste", r"[A-Za-z0-9]{32}",
                          ("input[readonly]", "code")),)),
            Step("Codestral key (optional)", "https://console.mistral.ai/codestral",
                 "Generate the Codestral key if you want codestral.mistral.ai routing (optional).",
                 (KeySpec("CODESTRAL_API_KEY", "Codestral API key", "paste", r"[A-Za-z0-9]{32}", optional=True),)),
        ),
        billing_url="https://console.mistral.ai/billing",
        billing_instructions="Experiment (Free mode) plan needs a verified phone number only; no card.",
        plugins=("mistral",),
        card_required=False,
        notes="Free plan requests may be used for training. Use -latest model aliases (pinned ids retire often).",
    ),
    ProviderEntry(
        id="openrouter", name="OpenRouter", category="llm",
        signup_url="https://openrouter.ai/",
        steps=(Step("API key", "https://openrouter.ai/settings/keys",
                    "Click 'Create Key', name it 'omnifree', set credit limit 0, keep the key visible.",
                    (KeySpec("OPENROUTER_API_KEY", "OpenRouter API key", "extract", r"sk-or-v1-[0-9a-f]{64}",
                             ("input[readonly]", "code")),)),),
        billing_url="https://openrouter.ai/settings/credits",
        billing_instructions="Do not buy credits; ':free' models only (free_only: true in providers.yaml). "
                             "Without $10 of lifetime credit purchases the free cap is 50 requests/day.",
        plugins=("openrouter",),
        card_required=False,
    ),
    ProviderEntry(
        id="cloudflare", name="Cloudflare (Workers, Workers AI, R2, D1, Pages)", category="edge",
        signup_url="https://dash.cloudflare.com/sign-up",
        steps=(
            Step("Account id", "https://dash.cloudflare.com/",
                 "Log in; the dashboard URL contains your 32-hex account id.",
                 (KeySpec("CLOUDFLARE_ACCOUNT_ID", "Cloudflare account id", "extract", r"[0-9a-f]{32}",
                          from_url=True),)),
            Step("API tokens", "https://dash.cloudflare.com/profile/api-tokens",
                 "Create two custom tokens. (1) 'omnifree-deploy': Account > Workers Scripts Edit, "
                 "Cloudflare Pages Edit, D1 Edit, Workers R2 Storage Edit. (2) 'omnifree-ai': "
                 "Account > Workers AI Read. Each token is shown only once.",
                 (KeySpec("CLOUDFLARE_API_TOKEN", "Cloudflare deploy API token (Workers/Pages/D1)", "paste",
                          r"[A-Za-z0-9_\-]{40,}"),
                  KeySpec("CLOUDFLARE_AI_API_TOKEN", "Cloudflare Workers AI token", "paste",
                          r"[A-Za-z0-9_\-]{40,}"))),
            Step("R2 access keys", "https://dash.cloudflare.com/?to=/:account/r2/api-tokens",
                 "Enable R2 (free 10GB; Cloudflare may ask for a card, which you type yourself), create bucket "
                 "'omnifree', then 'Create API token' with Object Read & Write. Copy the S3 access key id + secret.",
                 (KeySpec("R2_ACCESS_KEY_ID", "R2 access key id", "paste", r"[0-9a-f]{32}"),
                  KeySpec("R2_SECRET_ACCESS_KEY", "R2 secret access key", "paste", r"[0-9a-f]{64}"))),
        ),
        billing_url="https://dash.cloudflare.com/?to=/:account/notifications",
        billing_instructions="Add a 'Usage Based Billing' notification so any paid usage emails you "
                             "(Cloudflare has no hard spending cap).",
        plugins=("cloudflare_ai", "cloudflare_workers", "cloudflare_r2", "cloudflare_d1"),
        card_required=False,
        notes="Only enabling R2 asks for a payment method; Workers, Workers AI, D1 and Pages need no card.",
    ),
    ProviderEntry(
        id="nvidia_nim", name="NVIDIA NIM (build.nvidia.com)", category="llm",
        signup_url="https://build.nvidia.com/",
        steps=(Step("API key", "https://build.nvidia.com/settings/api-keys",
                    "Click 'Generate API Key' and keep it visible.",
                    (KeySpec("NVIDIA_API_KEY", "NVIDIA API key", "extract", r"nvapi-[A-Za-z0-9_\-]{40,}",
                             ("input[readonly]", "code")),)),),
        plugins=("nvidia_nim",),
        card_required=False,
        notes="Free for development/prototyping only (about 40 RPM); not for production, inputs may be logged.",
    ),
    ProviderEntry(
        id="cohere", name="Cohere (trial key)", category="llm",
        signup_url="https://dashboard.cohere.com/welcome/register",
        steps=(Step("Trial API key", "https://dashboard.cohere.com/api-keys",
                    "Copy the default Trial key (or create a new trial key).",
                    (KeySpec("COHERE_API_KEY", "Cohere trial API key", "paste", r"[A-Za-z0-9]{40}"),)),),
        billing_url="https://dashboard.cohere.com/billing",
        billing_instructions="Trial keys are free (1,000 calls/month, non-commercial); never create a production key.",
        plugins=("cohere",),
        card_required=False,
    ),
    ProviderEntry(
        id="sambanova", name="SambaNova Cloud", category="llm",
        signup_url="https://cloud.sambanova.ai/",
        steps=(Step("API key", "https://cloud.sambanova.ai/apis",
                    "Create an API key and keep it visible.",
                    (KeySpec("SAMBANOVA_API_KEY", "SambaNova API key", "paste",
                             r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[A-Za-z0-9_\-]{20,}"),)),),
        billing_url="https://cloud.sambanova.ai/plans/billing",
        billing_instructions="Do NOT add a payment method: that moves the account to the paid Developer tier. "
                             "The Free tier is only 20 requests/day per model.",
        plugins=("sambanova",),
        card_required=False,
    ),
    ProviderEntry(
        id="zai", name="Z.AI (GLM)", category="llm",
        signup_url="https://z.ai/",
        steps=(Step("API key", "https://z.ai/manage-apikey/apikey-list",
                    "Create an API key (GLM flash models are free).",
                    (KeySpec("ZAI_API_KEY", "Z.AI API key", "paste", r"[A-Za-z0-9._\-]{20,}"),)),),
        billing_url="https://z.ai/manage-apikey/billing",
        billing_instructions="Do not top up: only the GLM Flash models are free.",
        plugins=("zai",),
        card_required=False,
    ),
    ProviderEntry(
        id="cerebras", name="Cerebras Inference (trial, card required)", category="llm",
        signup_url="https://cloud.cerebras.ai/",
        steps=(Step("API key", "https://cloud.cerebras.ai/platform/api-keys",
                    "Generate a key and keep it visible. The API stays inactive until a card is added.",
                    (KeySpec("CEREBRAS_API_KEY", "Cerebras API key", "extract", r"csk-[a-z0-9]{32,}",
                             ("input[readonly]", "code")),)),),
        billing_url="https://cloud.cerebras.ai/platform/billing",
        billing_instructions="The Free Trial needs a verified card and gives $5 of credits that expire after 30 days; "
                             "do not buy credits.",
        plugins=("cerebras",),
        optional=True,
        card_required=True,
        notes="No permanent free tier any more: a 30-day / $5 trial that needs a card. Skip it unless you want the trial.",
    ),
    # ------------------------------------------------------------------ clouds
    ProviderEntry(
        id="oracle", name="Oracle Cloud (OCI Always Free)", category="cloud",
        signup_url="https://signup.cloud.oracle.com/",
        steps=(
            Step("API signing key", "https://cloud.oracle.com/identity/domains/my-profile/api-keys",
                 "Click 'Add API key' > 'Generate API key pair', DOWNLOAD the private key, click 'Add'. "
                 "Leave the 'Configuration file preview' open: tenancy, user and fingerprint are read from it.",
                 (KeySpec("OCI_TENANCY_OCID", "Tenancy OCID", "extract", r"ocid1\.tenancy\.oc1\.\.[a-z0-9]+"),
                  KeySpec("OCI_USER_OCID", "User OCID", "extract", r"ocid1\.user\.oc1\.\.[a-z0-9]+"),
                  KeySpec("OCI_FINGERPRINT", "API key fingerprint", "extract", r"(?:[0-9a-f]{2}:){15}[0-9a-f]{2}"),
                  KeySpec("OCI_PRIVATE_KEY_PEM", "Downloaded private key (.pem path)", "file",
                          validator="pem_private_key"))),
        ),
        billing_url="https://cloud.oracle.com/usage/budgets",
        billing_instructions="`python -m automation.deploy budgets` creates the $1 budget alert by API; "
                             "do not upgrade to Pay As You Go unless you want A1 capacity priority.",
        plugins=("oracle_compute",),
        budget_automated=True,
        card_required=True,
        notes="Card verification at signup is mandatory and must be typed by you. Always Free A1 is 2 OCPU / 12 GB "
              "in total (VM.Standard.A1.Flex), home region only.",
    ),
    ProviderEntry(
        id="oracle_adb", name="Oracle Autonomous Database x2", category="database",
        signup_url=None,
        steps=(
            Step("Autonomous DB 1", "https://cloud.oracle.com/db/adbs",
                 "Create an Always Free Autonomous Database (Transaction Processing). Set the ADMIN password, "
                 "Network: 'Secure access from everywhere' + allow TLS (mTLS not required). Then 'Database "
                 "connection' > TLS > copy a connection string (e.g. the _low one).",
                 (KeySpec("ORACLE_ADB1_USER", "ADB1 user (usually ADMIN)", "paste", r"[A-Za-z][A-Za-z0-9_$#]{0,127}"),
                  KeySpec("ORACLE_ADB1_PASSWORD", "ADB1 password", "paste"),
                  KeySpec("ORACLE_ADB1_DSN", "ADB1 TLS connection string", "paste"),
                  KeySpec("ORACLE_ADB1_WALLET_PASSWORD", "ADB1 wallet password (mTLS only)", "paste", optional=True))),
            Step("Autonomous DB 2", "https://cloud.oracle.com/db/adbs",
                 "Repeat for the second Always Free database.",
                 (KeySpec("ORACLE_ADB2_USER", "ADB2 user (usually ADMIN)", "paste", r"[A-Za-z][A-Za-z0-9_$#]{0,127}"),
                  KeySpec("ORACLE_ADB2_PASSWORD", "ADB2 password", "paste"),
                  KeySpec("ORACLE_ADB2_DSN", "ADB2 TLS connection string", "paste"),
                  KeySpec("ORACLE_ADB2_WALLET_PASSWORD", "ADB2 wallet password (mTLS only)", "paste", optional=True))),
        ),
        plugins=("oracle_adb_1", "oracle_adb_2"),
        card_required=True,
    ),
    ProviderEntry(
        id="gcp", name="Google Cloud (Cloud Run, e2-micro, GCS, Firestore)", category="cloud",
        signup_url="https://console.cloud.google.com/freetrial",
        steps=(Step("Service account key", "https://console.cloud.google.com/iam-admin/serviceaccounts",
                    "Create project 'omnifree', create service account 'omnifree-hub' with roles: Cloud Run Admin, "
                    "Compute Viewer, Storage Admin, Cloud Datastore User, Service Account User. "
                    "Keys > Add key > JSON. The JSON file downloads to your machine.",
                    (KeySpec("GCP_SERVICE_ACCOUNT_JSON", "Service account JSON (file path)", "file",
                             validator="service_account_json"),)),),
        billing_url="https://console.cloud.google.com/billing/budgets",
        billing_instructions="`python -m automation.deploy budgets` creates the $1 budget by gcloud.",
        plugins=("gcp_cloud_run", "gcp_e2_micro", "gcp_firestore", "gcp_gcs"),
        budget_automated=True,
        card_required=True,
        notes="The Free Tier needs a billing account (card, $0-1 temporary hold). Budgets alert, they do not cap.",
    ),
    ProviderEntry(
        id="aws", name="AWS (DynamoDB Always Free)", category="cloud",
        signup_url="https://portal.aws.amazon.com/billing/signup",
        steps=(Step("Access key", "https://console.aws.amazon.com/iam/home#/users",
                    "Create IAM user 'omnifree' with AmazonDynamoDBFullAccess + AWSBudgetsActionsWithAWSResourceControlAccess "
                    "(or a budgets:* inline policy). Security credentials > Create access key > 'Application running outside AWS'.",
                    (KeySpec("AWS_ACCESS_KEY_ID", "AWS access key id", "extract", r"AKIA[0-9A-Z]{16}"),
                     KeySpec("AWS_SECRET_ACCESS_KEY", "AWS secret access key", "paste", r"[A-Za-z0-9/+=]{40}"))),),
        billing_url="https://console.aws.amazon.com/billing/home#/budgets",
        billing_instructions="`python -m automation.deploy budgets` creates the $1 budget by API.",
        plugins=("aws_dynamodb",),
        budget_automated=True,
        card_required=True,
        notes="DynamoDB is free for PROVISIONED capacity only (25 RCU/WCU, 25 GB); on-demand mode is billed.",
    ),
    ProviderEntry(
        id="ibm_cloudant", name="IBM Cloudant Lite", category="database",
        signup_url="https://cloud.ibm.com/registration",
        steps=(Step("Service credentials", "https://cloud.ibm.com/catalog/services/cloudant",
                    "Create a Cloudant instance on the Lite plan (IAM auth). Service credentials > New credential, "
                    "expand it and copy 'url' and 'apikey'.",
                    (KeySpec("CLOUDANT_URL", "Cloudant URL", "extract",
                             r"https://[A-Za-z0-9\-]+\.cloudantnosqldb\.appdomain\.cloud"),
                     KeySpec("CLOUDANT_APIKEY", "Cloudant IAM apikey", "paste", r"[A-Za-z0-9_\-]{30,}"))),),
        billing_url="https://cloud.ibm.com/billing/spending-notifications",
        billing_instructions="Enable spending notifications at the lowest threshold.",
        plugins=("ibm_cloudant",),
        card_required=None,
        notes="IBM Cloud has asked new accounts for a card for identity verification since about 2023 (unverified).",
    ),
    # ------------------------------------------------------------------ databases
    ProviderEntry(
        id="supabase", name="Supabase", category="database",
        signup_url="https://supabase.com/dashboard/sign-up",
        steps=(
            Step("API keys", "https://supabase.com/dashboard/project/_/settings/api-keys",
                 "Create project 'omnifree' (free plan), then copy the service_role (secret) key.",
                 (KeySpec("SUPABASE_SERVICE_ROLE_KEY", "Supabase service_role key", "paste",
                          rf"{_JWT}|sb_secret_[A-Za-z0-9_\-]+"),)),
            Step("Database URL (optional)", "https://supabase.com/dashboard/project/_/settings/database",
                 "Copy the connection string (Session pooler) with your DB password filled in.",
                 (KeySpec("SUPABASE_DB_URL", "Supabase Postgres URL", "paste", r"postgres(?:ql)?://\S+",
                          optional=True),)),
        ),
        billing_url="https://supabase.com/dashboard/org/_/billing",
        billing_instructions="Stay on the Free plan (projects pause after 1 week idle; the health monitor keeps them alive).",
        plugins=("supabase",),
        card_required=False,
        notes="Also set providers.yaml supabase.settings.project_url (https://<ref>.supabase.co).",
    ),
    ProviderEntry(
        id="neon", name="Neon Postgres", category="database",
        signup_url="https://console.neon.tech/signup",
        steps=(
            Step("Connection string", "https://console.neon.tech/app/projects",
                 "Create project 'omnifree', click 'Connect' and reveal the connection string.",
                 (KeySpec("NEON_DATABASE_URL", "Neon database URL", "extract",
                          r"postgres(?:ql)?://[^\s\"'<>]+neon\.tech[^\s\"'<>]*"),)),
            Step("API key (optional)", "https://console.neon.tech/app/settings/api-keys",
                 "Create a personal API key for usage reporting (optional).",
                 (KeySpec("NEON_API_KEY", "Neon API key", "extract", r"napi_[A-Za-z0-9]{20,}", optional=True),)),
        ),
        billing_url="https://console.neon.tech/app/billing",
        billing_instructions="Stay on the Free plan (no card required).",
        plugins=("neon",),
        card_required=False,
    ),
    ProviderEntry(
        id="turso", name="Turso (libSQL)", category="database",
        signup_url="https://app.turso.tech/",
        steps=(Step("Database URL + token", "https://app.turso.tech/",
                    "Create database 'omnifree', open it, copy the libsql:// URL and 'Create Token' (read/write).",
                    (KeySpec("TURSO_DATABASE_URL", "Turso database URL", "extract",
                             r"libsql://[a-z0-9\-]+\.turso\.io"),
                     KeySpec("TURSO_AUTH_TOKEN", "Turso auth token", "paste", _JWT))),),
        plugins=("turso",),
        card_required=False,
    ),
    ProviderEntry(
        id="mongodb_atlas", name="MongoDB Atlas M0", category="database",
        signup_url="https://www.mongodb.com/cloud/atlas/register",
        steps=(Step("Connection string", "https://cloud.mongodb.com/",
                    "Create a free M0 cluster, a database user, allow your IPs (or 0.0.0.0/0 for Cloud Run), "
                    "then Connect > Drivers and copy the mongodb+srv URI with the password filled in.",
                    (KeySpec("MONGODB_URI", "MongoDB URI", "paste", r"mongodb(?:\+srv)?://\S+"),)),),
        plugins=("mongodb_atlas",),
        card_required=False,
    ),
    ProviderEntry(
        id="upstash", name="Upstash (Redis, QStash)", category="database",
        signup_url="https://console.upstash.com/login",
        steps=(
            Step("Redis REST", "https://console.upstash.com/redis",
                 "Create a free Redis database, open 'REST API' and copy UPSTASH_REDIS_REST_URL + TOKEN.",
                 (KeySpec("UPSTASH_REDIS_REST_URL", "Redis REST URL", "extract", r"https://[a-z0-9\-]+\.upstash\.io"),
                  KeySpec("UPSTASH_REDIS_REST_TOKEN", "Redis REST token", "paste", r"[A-Za-z0-9_=\-]{30,}"))),
            Step("QStash", "https://console.upstash.com/qstash",
                 "Copy QSTASH_TOKEN and both signing keys.",
                 (KeySpec("QSTASH_TOKEN", "QStash token", "paste", r"[A-Za-z0-9_=\-]{30,}"),
                  KeySpec("QSTASH_CURRENT_SIGNING_KEY", "QStash current signing key", "extract",
                          r"sig_[A-Za-z0-9]{20,}", optional=True),
                  KeySpec("QSTASH_NEXT_SIGNING_KEY", "QStash next signing key", "extract",
                          r"sig_[A-Za-z0-9]{20,}", optional=True))),
        ),
        billing_url="https://console.upstash.com/account/billing",
        billing_instructions="Do not add a card: that moves databases to pay-as-you-go automatically.",
        plugins=("upstash_redis", "upstash_qstash"),
        card_required=False,
        notes="Upstash Kafka was discontinued on 2025-03-11 and is not provisioned; QStash replaces it.",
    ),
    # ------------------------------------------------------------------ storage
    ProviderEntry(
        id="backblaze_b2", name="Backblaze B2", category="storage",
        signup_url="https://www.backblaze.com/sign-up/cloud-storage",
        steps=(Step("Application key", "https://secure.backblaze.com/app_keys.htm",
                    "Create private bucket 'omnifree', then 'Add a New Application Key' restricted to it. "
                    "The applicationKey is shown once.",
                    (KeySpec("B2_KEY_ID", "B2 keyID", "paste", r"[0-9a-f]{12,25}"),
                     KeySpec("B2_APPLICATION_KEY", "B2 applicationKey", "paste", r"K[0-9A-Za-z+/]{30}"))),),
        billing_url="https://secure.backblaze.com/b2_caps_alerts.htm",
        billing_instructions="B2 has no budget API: on 'Caps & Alerts' set every daily cap to $0 (or $1) yourself.",
        plugins=("backblaze_b2",),
        card_required=False,
    ),
    # ------------------------------------------------------------------ compute / edge
    ProviderEntry(
        id="koyeb", name="Koyeb (optional, paid for new signups)", category="edge",
        signup_url="https://app.koyeb.com/auth/signup",
        steps=(Step("API token", "https://app.koyeb.com/user/settings/api",
                    "Only for an org that still has the legacy free instance: create an API access token "
                    "named 'omnifree'. Otherwise skip this provider.",
                    (KeySpec("KOYEB_API_TOKEN", "Koyeb API token", "paste", r"[A-Za-z0-9_\-]{30,}"),)),),
        billing_url="https://app.koyeb.com/settings/billing",
        billing_instructions="New signups start at Pro ($29/mo + compute); do not subscribe for OmniFree.",
        plugins=("koyeb",),
        optional=True,
        card_required=True,
        notes="No free compute tier for new signups since 2026 (Mistral AI acquisition). The health monitor runs on "
              "the GCP e2-micro; Koyeb is only for legacy free orgs. The koyeb plugin is disabled by default.",
    ),
    ProviderEntry(
        id="vercel", name="Vercel", category="edge",
        signup_url="https://vercel.com/signup",
        steps=(Step("Token", "https://vercel.com/account/settings/tokens",
                    "Create a token (scope: your Hobby account). Optional: copy your Team ID from team settings.",
                    (KeySpec("VERCEL_TOKEN", "Vercel token", "paste", r"[A-Za-z0-9]{24,}"),
                     KeySpec("VERCEL_TEAM_ID", "Vercel team id", "extract", r"team_[A-Za-z0-9]{20,}", optional=True))),),
        plugins=("vercel",),
        card_required=False,
        notes="Hobby is for non-commercial personal use only; limits pause the feature instead of billing.",
    ),
    ProviderEntry(
        id="netlify", name="Netlify", category="edge",
        signup_url="https://app.netlify.com/signup",
        steps=(Step("Personal access token", "https://app.netlify.com/user/applications#personal-access-tokens",
                    "Create a personal access token.",
                    (KeySpec("NETLIFY_TOKEN", "Netlify token", "extract", r"nfp_[A-Za-z0-9]{30,}"),)),),
        plugins=("netlify",),
        card_required=False,
        notes="Free plan is credit-based now (300 credits: about 20 production deploys or 15 GB bandwidth).",
    ),
    ProviderEntry(
        id="github", name="GitHub (Actions, Pages)", category="edge",
        signup_url="https://github.com/signup",
        steps=(Step("Fine-grained token", "https://github.com/settings/personal-access-tokens/new",
                    "Repository access: the omnifree repo. Permissions: Actions RW, Pages RW, Contents RW, "
                    "Workflows RW, Metadata R. Generate and keep it visible.",
                    (KeySpec("GITHUB_TOKEN", "GitHub token", "extract",
                             r"github_pat_[A-Za-z0-9_]{50,}|ghp_[A-Za-z0-9]{36}"),)),),
        billing_url="https://github.com/settings/billing/budgets",
        billing_instructions="Set a $0 budget for Actions/Packages so private-repo overages stop instead of billing.",
        plugins=("github_pages", "github_actions"),
        card_required=False,
    ),
    # ------------------------------------------------------------------ network
    ProviderEntry(
        id="tailscale", name="Tailscale", category="network",
        signup_url="https://login.tailscale.com/start",
        steps=(Step("API + auth keys", "https://login.tailscale.com/admin/settings/keys",
                    "Generate an API access token, then an auth key (Reusable, Pre-approved, tag:omnifree "
                    "if you use ACL tags) for the VMs.",
                    (KeySpec("TAILSCALE_API_KEY", "Tailscale API access token", "extract", r"tskey-api-[A-Za-z0-9\-]+"),
                     KeySpec("TAILSCALE_AUTHKEY", "Tailscale auth key (for ssh-bootstrap)", "extract",
                             r"tskey-auth-[A-Za-z0-9\-]+"),
                     KeySpec("TAILSCALE_HUB_AUTHKEY", "Ephemeral reusable auth key for Cloud Run (hub)", "paste",
                             r"tskey-auth-[A-Za-z0-9\-]+", optional=True))),),
        plugins=("tailscale",),
        card_required=False,
        notes="Enable MagicDNS in the admin console so oracle-arm / amd-vm1 / amd-vm2 resolve. API keys expire "
              "(max 90 days); an OAuth client is better for long-lived automation.",
    ),
    ProviderEntry(
        id="local_secrets", name="Locally generated secrets", category="local",
        signup_url=None,
        steps=(Step("Generated", "about:blank",
                    "Random secrets for services you host yourself.",
                    (KeySpec("HUB_API_TOKEN", "hub_api bearer token", "generate"),
                     KeySpec("INSTINCT_API_TOKEN", "Instinct agent bearer token (hub -> instinct)", "generate"),
                     KeySpec("PIHOLE_PASSWORD", "Pi-hole web/API password", "generate", generate_bytes=18),
                     KeySpec("FRPS_TOKEN", "frps auth token (AMD VM2)", "generate"),
                     KeySpec("ORACLE_BLOCK_WEBDAV_USER", "WebDAV user for the block volume", "generate",
                             generate_bytes=6),
                     KeySpec("ORACLE_BLOCK_WEBDAV_PASSWORD", "WebDAV password for the block volume", "generate"),
                     KeySpec("HEALTHMONITOR_ALERT_WEBHOOK_URL", "Alert webhook (Discord/Slack/ntfy URL)", "paste",
                             r"https://\S+", optional=True),
                     KeySpec("HEALTHMONITOR_HEARTBEAT_URL", "Dead-man's-switch URL (e.g. healthchecks.io)", "paste",
                             r"https://\S+", optional=True))),),
        plugins=("pihole", "oracle_block_storage"),
    ),
    ProviderEntry(
        id="freellmapi", name="FreeLLMAPI (self-hosted pool on Oracle ARM)", category="local",
        signup_url=None,
        steps=(
            Step("Server secrets", "about:blank",
                 "Generated locally; plugins/llm_pool/freellmapi_injector.py renders them into the compose .env "
                 "and the declarative config (upstream keys come from the providers above).",
                 (KeySpec("FREELLMAPI_ENCRYPTION_KEY", "FreeLLMAPI ENCRYPTION_KEY (64 hex)", "generate",
                          r"[0-9a-f]{64}", generate_format="hex"),
                  KeySpec("FREELLMAPI_ADMIN_EMAIL", "FreeLLMAPI first dashboard account email", "paste",
                          r"[^@\s]+@[^@\s]+", optional=True),
                  KeySpec("FREELLMAPI_ADMIN_PASSWORD", "FreeLLMAPI first dashboard account password", "generate",
                          optional=True))),
            Step("Unified API key (after deploy-arm)", "http://oracle-arm:3001/",
                 "Only after `python -m automation.deploy deploy-arm`: open the FreeLLMAPI dashboard over Tailscale, "
                 "sign in, open 'Keys' and copy the unified freellmapi-... key. Optionally copy "
                 "localStorage.freellmapi_dashboard_token (DevTools) for hot key pushes.",
                 (KeySpec("FREELLMAPI_API_KEY", "FreeLLMAPI unified API key", "extract", r"freellmapi-[A-Za-z0-9_\-]{8,}",
                          ("input[readonly]", "code")),
                  KeySpec("FREELLMAPI_DASHBOARD_TOKEN", "FreeLLMAPI dashboard session token", "paste",
                          optional=True))),
        ),
        plugins=("freellmapi",),
        notes="Run this provider last: the unified key exists only once FreeLLMAPI is running on oracle-arm.",
    ),
)


def catalog_by_id() -> dict[str, ProviderEntry]:
    return {p.id: p for p in CATALOG}


def all_vault_keys(*, include_optional: bool = True) -> list[str]:
    seen: list[str] = []
    for entry in CATALOG:
        for key in entry.keys:
            if (include_optional or not key.optional) and key.vault_key not in seen:
                seen.append(key.vault_key)
    return seen

# OmniFree provisioning status

_Rewritten by `python -m automation.browser_provisioner` on 2026-10-19 10:58 UTC. Secret values are never written here; they live in the encrypted `.env.vault`._

**Progress:** 18/28 providers complete, 32/53 required keys in the vault.

Resume any time: `python -m automation.browser_provisioner run` skips what is already stored (`--force` redoes it, `--only <id>` targets one provider).

## LLM providers

- [x] **Groq** `groq`: done - plugins: groq
  - keys page: https://console.groq.com/keys
  - [x] `GROQ_API_KEY`: Groq API key
  - [ ] billing / $1 alert: Stay on the free plan; do not add a card. (https://console.groq.com/settings/billing)
- [x] **Google AI Studio (Gemini)** `gemini`: done - plugins: gemini
  - keys page: https://aistudio.google.com/apikey
  - [x] `GEMINI_API_KEY`: Gemini API key
  - [ ] billing / $1 alert: Keep the project on the free tier (no Cloud Billing linked to it). Per-model limits are only shown under your project in AI Studio; free-tier prompts may be used by Google. (https://console.cloud.google.com/billing/budgets)
- [ ] **Mistral La Plateforme** `mistral`: pending - plugins: mistral
  - keys page: https://console.mistral.ai/api-keys
  - [ ] `MISTRAL_API_KEY`: Mistral API key
  - [ ] `CODESTRAL_API_KEY`: Codestral API key (optional)
  - [ ] billing / $1 alert: Experiment (Free mode) plan needs a verified phone number only; no card. (https://console.mistral.ai/billing)
- [x] **OpenRouter** `openrouter`: done - plugins: openrouter
  - keys page: https://openrouter.ai/settings/keys
  - [x] `OPENROUTER_API_KEY`: OpenRouter API key
  - [ ] billing / $1 alert: Do not buy credits; ':free' models only (free_only: true in providers.yaml). Without $10 of lifetime credit purchases the free cap is 50 requests/day. (https://openrouter.ai/settings/credits)
- [x] **NVIDIA NIM (build.nvidia.com)** `nvidia_nim`: done - plugins: nvidia_nim
  - keys page: https://build.nvidia.com/settings/api-keys
  - [x] `NVIDIA_API_KEY`: NVIDIA API key
- [x] **Cohere (trial key)** `cohere`: done - plugins: cohere
  - keys page: https://dashboard.cohere.com/api-keys
  - [x] `COHERE_API_KEY`: Cohere trial API key
  - [ ] billing / $1 alert: Trial keys are free (1,000 calls/month, non-commercial); never create a production key. (https://dashboard.cohere.com/billing)
- [x] **SambaNova Cloud** `sambanova`: done - plugins: sambanova
  - keys page: https://cloud.sambanova.ai/apis
  - [x] `SAMBANOVA_API_KEY`: SambaNova API key
  - [ ] billing / $1 alert: Do NOT add a payment method: that moves the account to the paid Developer tier. The Free tier is only 20 requests/day per model. (https://cloud.sambanova.ai/plans/billing)
- [x] **Z.AI (GLM)** `zai`: done - plugins: zai
  - keys page: https://z.ai/manage-apikey/apikey-list
  - [x] `ZAI_API_KEY`: Z.AI API key
  - [ ] billing / $1 alert: Do not top up: only the GLM Flash models are free. (https://z.ai/manage-apikey/billing)
- [ ] **Cerebras Inference (trial, card required)** `cerebras` [optional, card required]: pending - plugins: cerebras
  - keys page: https://cloud.cerebras.ai/platform/api-keys
  - [ ] `CEREBRAS_API_KEY`: Cerebras API key
  - [ ] billing / $1 alert: The Free Trial needs a verified card and gives $5 of credits that expire after 30 days; do not buy credits. (https://cloud.cerebras.ai/platform/billing)

## Clouds

- [ ] **Oracle Cloud (OCI Always Free)** `oracle` [card required]: pending - plugins: oracle_compute
  - keys page: https://cloud.oracle.com/identity/domains/my-profile/api-keys
  - [ ] `OCI_TENANCY_OCID`: Tenancy OCID
  - [ ] `OCI_USER_OCID`: User OCID
  - [ ] `OCI_FINGERPRINT`: API key fingerprint
  - [ ] `OCI_PRIVATE_KEY_PEM`: Downloaded private key (.pem path)
  - [ ] billing / $1 alert: automated by `deploy.py budgets` (https://cloud.oracle.com/usage/budgets)
- [ ] **Google Cloud (Cloud Run, e2-micro, GCS, Firestore)** `gcp` [card required]: pending - plugins: gcp_cloud_run, gcp_e2_micro, gcp_firestore, gcp_gcs
  - keys page: https://console.cloud.google.com/iam-admin/serviceaccounts
  - [ ] `GCP_SERVICE_ACCOUNT_JSON`: Service account JSON (file path)
  - [ ] billing / $1 alert: automated by `deploy.py budgets` (https://console.cloud.google.com/billing/budgets)
- [ ] **AWS (DynamoDB Always Free)** `aws` [card required]: pending - plugins: aws_dynamodb
  - keys page: https://console.aws.amazon.com/iam/home#/users
  - [ ] `AWS_ACCESS_KEY_ID`: AWS access key id
  - [ ] `AWS_SECRET_ACCESS_KEY`: AWS secret access key
  - [ ] billing / $1 alert: automated by `deploy.py budgets` (https://console.aws.amazon.com/billing/home#/budgets)

## Databases

- [ ] **Oracle Autonomous Database x2** `oracle_adb` [card required]: pending - plugins: oracle_adb_1, oracle_adb_2
  - keys page: https://cloud.oracle.com/db/adbs
  - [ ] `ORACLE_ADB1_USER`: ADB1 user (usually ADMIN)
  - [ ] `ORACLE_ADB1_PASSWORD`: ADB1 password
  - [ ] `ORACLE_ADB1_DSN`: ADB1 TLS connection string
  - [ ] `ORACLE_ADB1_WALLET_PASSWORD`: ADB1 wallet password (mTLS only) (optional)
  - [ ] `ORACLE_ADB2_USER`: ADB2 user (usually ADMIN)
  - [ ] `ORACLE_ADB2_PASSWORD`: ADB2 password
  - [ ] `ORACLE_ADB2_DSN`: ADB2 TLS connection string
  - [ ] `ORACLE_ADB2_WALLET_PASSWORD`: ADB2 wallet password (mTLS only) (optional)
- [ ] **IBM Cloudant Lite** `ibm_cloudant`: pending - plugins: ibm_cloudant
  - keys page: https://cloud.ibm.com/catalog/services/cloudant
  - [ ] `CLOUDANT_URL`: Cloudant URL
  - [ ] `CLOUDANT_APIKEY`: Cloudant IAM apikey
  - [ ] billing / $1 alert: Enable spending notifications at the lowest threshold. (https://cloud.ibm.com/billing/spending-notifications)
- [x] **Supabase** `supabase`: done - plugins: supabase
  - keys page: https://supabase.com/dashboard/project/_/settings/api-keys
  - [x] `SUPABASE_SERVICE_ROLE_KEY`: Supabase service_role key
  - [x] `SUPABASE_DB_URL`: Supabase Postgres URL (optional)
  - [ ] billing / $1 alert: Stay on the Free plan (projects pause after 1 week idle; the health monitor keeps them alive). (https://supabase.com/dashboard/org/_/billing)
- [x] **Neon Postgres** `neon`: done - plugins: neon
  - keys page: https://console.neon.tech/app/projects
  - [x] `NEON_DATABASE_URL`: Neon database URL
  - [x] `NEON_API_KEY`: Neon API key (optional)
  - [ ] billing / $1 alert: Stay on the Free plan (no card required). (https://console.neon.tech/app/billing)
- [x] **Turso (libSQL)** `turso`: done - plugins: turso
  - keys page: https://app.turso.tech/
  - [x] `TURSO_DATABASE_URL`: Turso database URL
  - [x] `TURSO_AUTH_TOKEN`: Turso auth token
- [x] **MongoDB Atlas M0** `mongodb_atlas`: done - plugins: mongodb_atlas
  - keys page: https://cloud.mongodb.com/
  - [x] `MONGODB_URI`: MongoDB URI
- [x] **Upstash (Redis, QStash)** `upstash`: done - plugins: upstash_redis, upstash_qstash
  - keys page: https://console.upstash.com/redis
  - [x] `UPSTASH_REDIS_REST_URL`: Redis REST URL
  - [x] `UPSTASH_REDIS_REST_TOKEN`: Redis REST token
  - [x] `QSTASH_TOKEN`: QStash token
  - [x] `QSTASH_CURRENT_SIGNING_KEY`: QStash current signing key (optional)
  - [x] `QSTASH_NEXT_SIGNING_KEY`: QStash next signing key (optional)
  - [ ] billing / $1 alert: Do not add a card: that moves databases to pay-as-you-go automatically. (https://console.upstash.com/account/billing)

## Storage

- [x] **Backblaze B2** `backblaze_b2`: done - plugins: backblaze_b2
  - keys page: https://secure.backblaze.com/app_keys.htm
  - [x] `B2_KEY_ID`: B2 keyID
  - [x] `B2_APPLICATION_KEY`: B2 applicationKey
  - [ ] billing / $1 alert: B2 has no budget API: on 'Caps & Alerts' set every daily cap to $0 (or $1) yourself. (https://secure.backblaze.com/b2_caps_alerts.htm)

## Edge / hosting / CI

- [ ] **Cloudflare (Workers, Workers AI, R2, D1, Pages)** `cloudflare`: pending - plugins: cloudflare_ai, cloudflare_workers, cloudflare_r2, cloudflare_d1
  - keys page: https://dash.cloudflare.com/
  - [x] `CLOUDFLARE_ACCOUNT_ID`: Cloudflare account id
  - [x] `CLOUDFLARE_API_TOKEN`: Cloudflare deploy API token (Workers/Pages/D1)
  - [x] `CLOUDFLARE_AI_API_TOKEN`: Cloudflare Workers AI token
  - [ ] `R2_ACCESS_KEY_ID`: R2 access key id
  - [ ] `R2_SECRET_ACCESS_KEY`: R2 secret access key
  - [ ] billing / $1 alert: Add a 'Usage Based Billing' notification so any paid usage emails you (Cloudflare has no hard spending cap). (https://dash.cloudflare.com/?to=/:account/notifications)
- [ ] **Koyeb (optional, paid for new signups)** `koyeb` [optional, card required]: pending - plugins: koyeb
  - keys page: https://app.koyeb.com/user/settings/api
  - [ ] `KOYEB_API_TOKEN`: Koyeb API token
  - [ ] billing / $1 alert: New signups start at Pro ($29/mo + compute); do not subscribe for OmniFree. (https://app.koyeb.com/settings/billing)
- [x] **Vercel** `vercel`: done - plugins: vercel
  - keys page: https://vercel.com/account/settings/tokens
  - [x] `VERCEL_TOKEN`: Vercel token
  - [x] `VERCEL_TEAM_ID`: Vercel team id (optional)
- [x] **Netlify** `netlify`: done - plugins: netlify
  - keys page: https://app.netlify.com/user/applications#personal-access-tokens
  - [x] `NETLIFY_TOKEN`: Netlify token
- [x] **GitHub (Actions, Pages)** `github`: done - plugins: github_pages, github_actions
  - keys page: https://github.com/settings/personal-access-tokens/new
  - [x] `GITHUB_TOKEN`: GitHub token
  - [ ] billing / $1 alert: Set a $0 budget for Actions/Packages so private-repo overages stop instead of billing. (https://github.com/settings/billing/budgets)

## Network

- [x] **Tailscale** `tailscale`: done - plugins: tailscale
  - keys page: https://login.tailscale.com/admin/settings/keys
  - [x] `TAILSCALE_API_KEY`: Tailscale API access token
  - [x] `TAILSCALE_AUTHKEY`: Tailscale auth key (for ssh-bootstrap)
  - [x] `TAILSCALE_HUB_AUTHKEY`: Ephemeral reusable auth key for Cloud Run (hub) (optional)

## Self-hosted / generated

- [x] **Locally generated secrets** `local_secrets`: done - plugins: pihole, oracle_block_storage
  - [x] `HUB_API_TOKEN`: hub_api bearer token
  - [x] `INSTINCT_API_TOKEN`: Instinct agent bearer token (hub -> instinct)
  - [x] `PIHOLE_PASSWORD`: Pi-hole web/API password
  - [x] `FRPS_TOKEN`: frps auth token (AMD VM2)
  - [x] `ORACLE_BLOCK_WEBDAV_USER`: WebDAV user for the block volume
  - [x] `ORACLE_BLOCK_WEBDAV_PASSWORD`: WebDAV password for the block volume
  - [ ] `HEALTHMONITOR_ALERT_WEBHOOK_URL`: Alert webhook (Discord/Slack/ntfy URL) (optional)
  - [ ] `HEALTHMONITOR_HEARTBEAT_URL`: Dead-man's-switch URL (e.g. healthchecks.io) (optional)
- [ ] **FreeLLMAPI (self-hosted pool on Oracle ARM)** `freellmapi`: pending - plugins: freellmapi
  - [x] `FREELLMAPI_ENCRYPTION_KEY`: FreeLLMAPI ENCRYPTION_KEY (64 hex)
  - [x] `FREELLMAPI_ADMIN_EMAIL`: FreeLLMAPI first dashboard account email (optional)
  - [x] `FREELLMAPI_ADMIN_PASSWORD`: FreeLLMAPI first dashboard account password (optional)
  - [ ] `FREELLMAPI_API_KEY`: FreeLLMAPI unified API key
  - [ ] `FREELLMAPI_DASHBOARD_TOKEN`: FreeLLMAPI dashboard session token (optional)

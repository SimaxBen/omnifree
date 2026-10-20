# OmniFree

OmniFree is a personal cloud built from free tiers. It pools free LLM APIs behind one failover router and spreads files across several free storage backends. It also gives you databases, DNS filtering and compute, and you control all of it from a dark-mode mobile PWA.

The design is hexagonal. `core/` defines the ports (LLM, storage, DB, compute, DNS, cron), the registry, the smart router and the encrypted vault. Every provider is an adapter under `plugins/<group>/`, declared in `config/providers.yaml`. `CONTRACT.md` is the reference for module paths, class names and vault keys.

```
                        phone / laptop (PWA, dark)
                                   |
                 Cloudflare Worker "omnifree-edge"  (/api/* -> hub, rest -> PWA mirrors)
                    |                                    |
          Cloud Run: hub_api (FastAPI + WS)     Cloudflare Pages / Vercel / Netlify / GH Pages
          + tailscaled (userspace)                       (static PWA)
                    |
   ---------------- Tailscale mesh (MagicDNS) ---------------------------------------
   |                         |                          |                     |
 oracle-arm (A1 2/12)     amd-vm1 (E2 micro)       amd-vm2 (E2 micro)    gcp-monitor (e2-micro)
 FreeLLMAPI :3001         Pi-hole :53/:80          frps :7000            healthmonitor :8000
 Ollama :11434                                     WireGuard (opt.)      (Koyeb: legacy only)
 Instinct agent :8090
 WebDAV on block volume
                    \__ upstream free LLM APIs: Groq, Gemini, Mistral, OpenRouter, Cloudflare AI,
                        NVIDIA NIM, Cohere, SambaNova, Z.AI, Cerebras trial (+ local Ollama fallback)
   Storage pool: R2 10GB, B2 10GB, GCS 5GB, Oracle block volume      Cron: GitHub Actions + QStash
   DBs: 2x Oracle ADB, Supabase, Neon, Turso, D1, Upstash Redis, DynamoDB, Firestore, Atlas, Cloudant
```

## Free-tier reality check (2026-10)

The original spec assumed free tiers that changed or disappeared. `docs/provider_research.json`
(checked on official pages on 2026-10-19) is the source for `config/providers.yaml` and the provisioner
catalog. What changed:

| provider | the spec assumed | reality in 2026-10 | what OmniFree does |
|---|---|---|---|
| Oracle Ampere A1 | 4 OCPU / 24 GB free | **2 OCPU / 12 GB in total** (1,500 OCPU-h + 9,000 GB-h per month), home region only; idle VMs can be reclaimed | one `VM.Standard.A1.Flex` 2 OCPU / 12 GB; `deploy_oci.py` refuses larger A1 layouts |
| Koyeb | free instance for the health monitor | **no free compute for new signups** since the Mistral AI acquisition (Pro from $29/mo) | `koyeb` plugin disabled by default, provisioner marks it optional / card required; monitor runs on the GCP e2-micro |
| Upstash Kafka | free Kafka REST | **shut down on 2025-03-11** | plugin, keys and tests removed; QStash covers queues/cron |
| Cerebras | permanent free tier | **trial only**: $5 credits for 30 days, card required | lowest cloud LLM priority, marked `trial`, optional in the provisioner |
| SambaNova | usable free tier | 20 requests/day per model (adding a card switches to paid Developer tier) | near the end of the LLM chain |
| OpenRouter | many `:free` models | 50 requests/day without $10 of lifetime credits; the deepseek/llama/qwen `:free` variants are gone | low priority; models fetched live and filtered to `:free` |
| Gemini API | published free RPM/RPD | limits no longer published and cut twice (Dec 2025, Apr 2026); prompts may be used by Google | conservative 5 RPM / 100 RPD; models fetched live |
| Mistral | pinned model ids | many pinned ids retired in 2026; phone verification required; free-plan data may be used for training | `-latest` aliases only |
| Cohere | `command-r(-plus)` aliases | old aliases retired; trial is 1,000 calls/month, non-commercial | `command-a-03-2025` default |
| NVIDIA NIM | ~1,000 free credits | credits phased out; ~40 RPM for prototyping only | RPM limit only |
| Netlify | 100 GB / 300 build minutes | credit-based: 300 credits (about 20 deploys or 15 GB) | mirror only |
| Neon | 0.5 GB | 1 GB per project, 100 CU-hours/month | limit raised |
| Tailscale | 3 users / 100 devices | 6 users, unlimited user devices, 50 tagged resources | no change needed |

Cards: Oracle, Google Cloud, AWS, Cerebras (trial) and Koyeb need a card at signup. Cloudflare asks for one only
when you enable R2. Upstash and SambaNova switch to pay-as-you-go as soon as you add a card, so don't add one.
Google Cloud budgets only send alerts; they never cap spending. Backblaze B2 is the only provider here with real
spending caps.

Model lists in `providers.yaml` are fallbacks. The router remembers the ids each adapter reports live
(`GET /models`, refreshed by the hub's background sweep), so a new OpenRouter `:free` or Gemini model is routed
without editing the YAML.

## Repository layout

| path | what |
|---|---|
| `core/` | ports, registry, router, quota store, event bus, vault, config |
| `plugins/` | one adapter per provider (see `config/providers.yaml`) |
| `config/providers.yaml` | which plugins exist, their priority/fallback order, limits and vault key names |
| `hub_api/` | FastAPI + WebSocket control plane (Cloud Run) |
| `instinct/` | autonomous browsing agent (FastAPI + headless Playwright), runs on oracle-arm |
| `mobile_app/` | React + Vite + Tailwind PWA |
| `automation/browser_provisioner.py` | guided sign-up and API key collection into the vault |
| `automation/deploy.py` | provisioning and deployment orchestrator |
| `healthmonitor/` | uptime checks, alerts and keep-alive jobs (GCP e2-micro; Koyeb only for legacy free orgs) |
| `deploy/` | cloud-init, compose files, Cloud Build, the Cloudflare Worker, `deploy.yaml` |
| `.github/workflows/` | CI (pytest, PWA build, Worker typecheck) and the hub deploy |
| `PROVISIONING_STATUS.md` | generated checklist of which accounts and keys are done (no secret values) |
| `data/` | local state, git-ignored: browser profile, quota DB, deploy state, known_hosts |

## Prerequisites (on your workstation)

* Python 3.12 and Node 24 (npm). Docker is optional locally, since images build on the hosts or in Cloud Build.
* `pip install -r requirements.txt`.
* Optional packages, installed per step:
  * `pip install playwright && python -m playwright install chromium` for the provisioner.
  * `pip install oci` for `oci-provision`, `budgets` and `lock-ssh`.
  * `pip install boto3` for the AWS budget.
  * `pip install asyncssh` (or `paramiko`) for the SSH steps.
  * `pip install qrcode` for the `qr` command.
* [gcloud CLI](https://cloud.google.com/sdk/docs/install) for `deploy-hub`, `deploy-monitor --target gce` and the GCP budget.
* The [Koyeb CLI](https://www.koyeb.com/docs/build-and-deploy/cli/installation), only if you use `deploy-monitor --target koyeb`
  (legacy free orgs only: Koyeb has no free tier for new signups).
* `wrangler`, `vercel` and `netlify-cli` run through `npx`, so you do not need to install them.

Run every command from the project root.

## 1. Create the vault

All secrets live in the encrypted `.env.vault` (AES-256-GCM, scrypt key). The passphrase only ever lives in your environment.

```bash
export OMNIFREE_VAULT_PASSPHRASE='a long passphrase'     # PowerShell: $env:OMNIFREE_VAULT_PASSPHRASE='...'
python -m core.vault init
python -m core.vault list
```

At runtime an environment variable with the same name overrides the vault value (`core.config.Settings.secret`).

## 2. Collect accounts and API keys (guided browser)

```bash
python -m automation.browser_provisioner list                # catalog: signup / key / billing URLs, vault keys
python -m automation.browser_provisioner run                 # everything, in catalog order
python -m automation.browser_provisioner run --only groq,gemini
python -m automation.browser_provisioner run --category llm
python -m automation.browser_provisioner run --no-browser    # print URLs, paste keys (headless machines)
python -m automation.browser_provisioner status              # rewrite PROVISIONING_STATUS.md
```

* The provisioner opens a **visible** Chromium with a persistent profile (`data/browser-profile`), so logins survive between runs. Treat that directory like a password store.
* For each provider it opens the API-key page. When it detects a login, OTP/2FA, CAPTCHA or card form, it stops and prints `press Enter when done`. You finish that step yourself in the browser.
* **It never types or reads card fields.** Card verification (Oracle, possibly Cloudflare R2, AWS) is always yours.
* It extracts keys whose format is stable: prefixes like `gsk_`, `AIza`, `sk-or-v1-`, `nvapi-`, `csk-` and `tskey-`, OCI OCIDs and fingerprints, and the Cloudflare account id from the dashboard URL. It shows each one masked and asks you to confirm. Everything else you paste into a hidden prompt.
* Downloaded files are read by path: the OCI API private key (`.pem`) and the GCP service account JSON.
* Self-hosted secrets are generated locally: `HUB_API_TOKEN`, `INSTINCT_API_TOKEN`, `PIHOLE_PASSWORD`, `FRPS_TOKEN`, the WebDAV user and password, and `FREELLMAPI_ENCRYPTION_KEY`.
* Every value goes into the vault immediately. Progress is saved in `data/provisioning_state.json` and mirrored to `PROVISIONING_STATUS.md`. Interrupt at any time and run again to resume; `--force` redoes keys you already have.
* Billing pages: for B2, IBM and Cloudflare it opens the page and you set the cap or alert yourself. OCI, GCP and AWS budgets are created by `deploy.py budgets` (step 6).
* At the end it runs the FreeLLMAPI injector (`plugins/llm_pool/freellmapi_injector.py plan`, plus `push` when `FREELLMAPI_DASHBOARD_TOKEN` is stored). This step checks which upstream keys will reach FreeLLMAPI.

The `freellmapi` entry is intentionally last. Its unified `freellmapi-...` key only exists after FreeLLMAPI is running (step 4), so run `--only freellmapi` again after `deploy-arm`.

You also need a Tailscale **auth key** (`TAILSCALE_AUTHKEY`: reusable, pre-approved) for the VMs. Ideally add an **ephemeral** one as `TAILSCALE_HUB_AUTHKEY` for Cloud Run. Turn on MagicDNS in the Tailscale admin console.

## 3. Oracle Cloud VMs

```bash
python -m automation.deploy ssh-keygen          # ~/.ssh/omnifree_ed25519(.pub), paths in deploy/deploy.yaml
python -m automation.deploy oci-provision       # VCN + subnet + security list, ARM A1 + 2 AMD micro, block volume
python -m automation.deploy ssh-bootstrap       # docker, tailscale up (auth key via a 0600 file), mount block volume
python -m automation.deploy status              # ids, public/private/Tailscale IPs (data/deploy_state.json)
```

* All settings are in `deploy/deploy.yaml`: shapes, OCPU/RAM, boot sizes, ingress ports and the block volume. The region and compartment come from `config/providers.yaml` (`oracle_compute.settings`).
* Each VM gets its cloud-init from `deploy/cloud-init/<host>.yaml`. Those files contain no secrets.
* **A1 capacity:** "Out of host capacity" is common. The launch is retried across availability domains for `capacity_retry_minutes`; rerun later if it still fails. The command is idempotent and skips anything that already exists.
* **Always Free block storage is 200 GB in total, boot volumes included.** The default is 3 boot volumes of 50 GB plus a 50 GB data volume.
* After bootstrap, every later step reaches the hosts by MagicDNS (`oracle-arm`, `amd-vm1`, `amd-vm2`). Use `--via public` or `--host NAME=IP` to override.

Once Tailscale works everywhere, close the public SSH rule:

```bash
python -m automation.deploy lock-ssh
```

## 4. Services on the VMs

```bash
python -m plugins.llm_pool.freellmapi_injector genkey    # only if FREELLMAPI_ENCRYPTION_KEY is not stored yet
python -m automation.deploy deploy-arm                   # FreeLLMAPI + Ollama + Instinct agent + WebDAV
python -m automation.deploy deploy-pihole --set-tailnet-dns
python -m automation.deploy deploy-edge                  # frps; add --wireguard for the WireGuard profile
```

* `deploy-arm` uploads `core/`, `plugins/`, `instinct/`, `config/` and the compose files to `/opt/omnifree`. It runs the injector **locally** to render the FreeLLMAPI declarative config and the compose `.env` (bound to the ARM's Tailscale IP), then uploads both with mode 600.
* It then starts `deploy/oracle-arm` (llm_pool stack: FreeLLMAPI, Ollama and the `ollama-pull` job for the models in `providers.yaml`) and `deploy/instinct`. Last comes `deploy/blockstore`, rclone WebDAV over the block volume, served on the port from `oracle_block_storage.settings.webdav_url`.
* After the first deploy, open `http://oracle-arm:3001` over Tailscale, sign in and copy the unified key. Then run `python -m automation.browser_provisioner run --only freellmapi` and `deploy-arm` again, so Instinct and the hub get `FREELLMAPI_API_KEY`.
* `deploy-pihole` publishes DNS and the web UI on the Tailscale IP only. `--set-tailnet-dns` makes Pi-hole the tailnet nameserver through the Tailscale API.
* `deploy-edge` runs frps on `:7000` with token auth and forced TLS. HTTP vhosts listen on `:8080`, reachable over Tailscale.

## 5. Hub, edge, PWA, monitor

```bash
python -m automation.deploy deploy-hub                   # Cloud Build + Cloud Run (gcloud must be logged in, or --activate-sa)
python -m automation.deploy deploy-pwa --target pages    # build mobile_app with VITE_HUB_URL, publish to Cloudflare Pages
python -m automation.deploy deploy-worker                # edge router: /api/* -> hub, rest -> PWA origins
python -m automation.deploy deploy-pwa                   # rebuild so the PWA points at <edge>/api
python -m automation.deploy deploy-pwa --target vercel   # optional mirrors (fallback origins for the Worker)
python -m automation.deploy deploy-monitor --target gce  # e2-micro in the tailnet (then: ssh-bootstrap --hosts gcp-monitor)
python -m automation.deploy deploy-monitor --target koyeb --repo you/omnifree   # optional, legacy free Koyeb orgs only
```

`deploy-hub` works like this:

* It enables the needed APIs and creates the Artifact Registry repo.
* It builds `hub_api/Dockerfile` plus `deploy/hub/Dockerfile.tailscale`. The second layer adds `tailscaled` in userspace mode, so plain-HTTP calls to `http://oracle-arm:3001` and other tailnet hosts go through its local proxy, while HTTPS calls to public APIs go direct.
* It stores **three** Secret Manager secrets: the encrypted vault file, its passphrase, and the Tailscale key for the hub. Older versions are destroyed, which keeps you inside the free tier of 6 active versions. Cloud Run mounts the vault at `/secrets/vault/env.vault`.
* It deploys with `min-instances 0`, `max-instances 2` and 512 MiB, and stores the URL in `data/deploy_state.json`.

Later commands read the hub, edge and PWA URLs from that state file. `--hub-url` and `--pwa-origin` override them.

The health monitor (`healthmonitor/targets.yaml`):

* It checks the hub, the edge Worker and the PWA. On the tailnet it also checks FreeLLMAPI, Ollama, Pi-hole and frps.
* It runs a daily Supabase keep-alive, since free projects pause after about a week idle.
* It sends alerts to `HEALTHMONITOR_ALERT_WEBHOOK_URL` (Discord, Slack, ntfy or generic JSON), or to Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`).
* It can ping a dead-man's switch (`HEALTHMONITOR_HEARTBEAT_URL`).
* Run it locally with `python -m healthmonitor` (`/healthz`, `/status` on `$PORT`, default 8000).

## 6. $1 billing alerts

```bash
python -m automation.deploy budgets --email you@example.com
```

This creates a $1/month budget with an alert on OCI (Budgets API, at 100% of actual spend), GCP (`gcloud billing budgets`, at 50% and 100%) and AWS (Budgets API, at 100% of actual spend, emailed to you). Backblaze B2 has no budget API, so set its caps to $0 on the Caps & Alerts page yourself; the provisioner opens that page for you.

## 7. Verify and open on your phone

```bash
python -m automation.deploy verify                       # 8 sequential /llm/chat probes
python -m automation.deploy verify --burst 40 --expect-failover
python -m automation.deploy qr --svg data/omnifree-qr.svg
```

For each probe, `verify` asserts four things:

* HTTP 200, with an `X-Routed-Via` header equal to the body's `routed_via`.
* The serving provider is an enabled LLM plugin.
* It never also appears among the failed attempts.
* Every failed attempt ranks above it in the `providers.yaml` chain.

`--burst` fires concurrent requests to push the top provider into its rate limit, so real 429 failover happens. `qr` prints the final URL (edge, then PWA, then hub) and a terminal QR code. In the PWA, open Settings and paste `HUB_API_TOKEN` (`python -m core.vault get HUB_API_TOKEN`).

## Operations runbook

| task | how |
|---|---|
| Rotate a provider key | `python -m automation.browser_provisioner run --only <id> --force`. Then `deploy-hub` (new vault version) and, for LLM upstreams, `deploy-arm` (re-renders FreeLLMAPI config) |
| Change fallback order / disable a provider | PWA Switchboard, or edit `config/providers.yaml` (`priority`, `enabled`) and redeploy the hub |
| Correct a free-tier limit or model id | edit `limits` / `settings.models` in `config/providers.yaml`; never in code |
| Add a provider | adapter under `plugins/<group>/`, entry in `providers.yaml`, vault keys in `automation/provider_catalog.py` |
| A1 VM missing | `oci-provision --only oracle-arm` (retries capacity), then `ssh-bootstrap --hosts oracle-arm`, `deploy-arm` |
| Port 53 busy on amd-vm1 | cloud-init disables the systemd-resolved stub; check `/etc/systemd/resolved.conf.d/omnifree-pihole.conf` |
| Hub cannot reach tailnet services | check `TAILSCALE_HUB_AUTHKEY` (ephemeral + reusable + pre-approved) and the Cloud Run logs for `tailscale up` |
| Back up | the encrypted `.env.vault` and passphrase (separately), the `freellmapi-data` volume **with** `FREELLMAPI_ENCRYPTION_KEY`, `/mnt/omnifree-block` |
| See what is deployed | `python -m automation.deploy status`, `PROVISIONING_STATUS.md`, hub `/plugins`, monitor `/status` |
| Dry run any CLI step | `python -m automation.deploy --dry-run deploy-hub` prints the gcloud/npx commands with secrets masked |

## CI/CD

* `.github/workflows/ci.yml` runs on every push and PR: `python -m pytest -q`, the `mobile_app` build (`tsc -b && vite build`) and the Worker typecheck.
* `.github/workflows/deploy-hub.yml` runs on pushes to `main` that touch the hub, core, plugins or providers. It tests, then runs `python -m automation.deploy deploy-hub` (Cloud Build + Cloud Run) and smoke-tests `/healthz`. It needs these repository secrets: `GCP_SERVICE_ACCOUNT_JSON`, `OMNIFREE_VAULT_PASSPHRASE`, and `OMNIFREE_ENV_VAULT_B64` (unless the encrypted vault is committed). Optional variables: `GCP_PROJECT_ID`, `GCP_REGION`, `OMNIFREE_CORS_ORIGINS`.
* Scheduled jobs use the `github_actions` and `upstash_qstash` cron plugins through the hub.

## Security notes

* Secrets exist in three places only: the encrypted vault, Secret Manager (encrypted vault + passphrase), and `.env` files with mode 600 on your own VMs. They never appear on command lines: the Tailscale auth key goes through a file, Secret Manager values through stdin, and runner output is masked.
* Everything except frps (`:7000`), WireGuard (`:51820/udp`) and temporary SSH (`:22`) is Tailscale-only. Run `lock-ssh` after bootstrap.
* `data/` (browser profile with live sessions, known_hosts, deploy state) is git- and gcloud-ignored. Never share it.
* Free-tier facts drift: quotas, model ids and URLs are best-known values kept in config, so check them in each console.

## Tests

```bash
python -m pytest -q                       # all Python tests
python -m pytest tests/automation -q      # automation, healthmonitor, deploy assets
(cd deploy/cloudflare-worker && npm ci && npm run typecheck)
(cd mobile_app && npm ci && npm run build)
```

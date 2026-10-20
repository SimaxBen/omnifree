# OmniFree core contract

This is the reference for everyone building on `core/`: plugin, hub_api, automation, instinct and PWA builders.
The code is the source of truth: `core/interfaces.py`, `core/registry.py`, `core/router.py`,
`core/events.py`, `core/quota.py`, `core/vault.py`, `core/config.py`, `core/openai_compat.py`.

Run everything from the project root (`omnifree/`). Top-level packages: `core`, `plugins`, `hub_api`,
`automation`, `instinct`. Tests: `python -m pytest tests -q` (pytest `pythonpath=.`, `asyncio_mode=auto`).

## 1. Ground rules for plugin builders

* One adapter class per entry in the table in section 9, at **exactly** the listed `plugins/<group>/<file>.py : ClassName`.
  One class may serve several entries (e.g. `oracle_adb_1`/`oracle_adb_2`) because config is per entry.
* Subclass exactly one port: `LLMAdapter`, `StorageAdapter`, `DBAdapter`, `ComputeAdapter`, `DNSAdapter`, `CronAdapter`.
* The constructor signature is `__init__(self, context: PluginContext)` (call `super().__init__(context)`). Do **no I/O** in it.
  Raise `NotConfigured` from the first operation that finds a missing secret or setting (`self.require_secret("api_key")`,
  `self.require_setting("bucket")`).
* Read secrets **only** through `self.secret(alias)` / `self.require_secret(alias)`. Aliases are the keys of
  `secrets:`/`optional_secrets:` in providers.yaml. Never read env vars or the vault directly, and never hardcode secrets.
* Uncertain provider facts (model ids, quotas, regions, URLs) belong in providers.yaml `settings`/`limits`, not in code.
* Use `httpx.AsyncClient` through `self.http` (or override `build_http_client()`, and **always** pass
  `transport=self.context.http_transport` so tests can inject `httpx.MockTransport`). `self.request(method, url, **kw)`
  sends the request, converts transport errors and calls `raise_for_response`.
* Heavy SDKs (`boto3`, `oracledb`, `oci`, `google-*`, `motor`/`pymongo`, `asyncpg`, `libsql_client`, `playwright`) must be imported
  **inside methods**. If one is missing, raise `NotConfigured("pip install oracledb", provider=self.name)`.
* Optional operations are never stubbed. Declare them in `capabilities` and override the method. Leave the method out
  when you do not support it (the base raises `CapabilityNotSupported`). `NotImplementedError`, `pass` stubs and TODOs are forbidden.
* `health()` must never raise. Use `return await self.probe(some_cheap_call)`.
* Declare `meta: ClassVar[PluginMeta] = PluginMeta(display_name=..., required_secrets=(...aliases...), docs_url=...)`.
  Concrete subclasses are registered automatically (`core.interfaces.adapter_catalog()`), and `registry.class_meta(name)` exposes them.
* Put plugin tests in `tests/plugins/<group>/test_<file>.py` and use `httpx.MockTransport`/`respx`. Build adapters with
  `PluginContext(name=..., kind=..., settings=..., secrets=..., http_transport=httpx.MockTransport(handler))`.

## 2. Ports (`core/interfaces.py`)

### ProviderAdapter (base of every port)

| member | contract |
|---|---|
| `kind: ClassVar[ProviderKind]` | set by the port base class |
| `capabilities: ClassVar[frozenset[str]]` | `Capability` values; `supports(cap)` |
| `meta: ClassVar[PluginMeta \| None]` | self-declared metadata |
| `name`, `context`, `settings` | instance name = providers.yaml key |
| `setting(key, default)`, `require_setting(key)` | settings access (`require_*` raises `NotConfigured`) |
| `secret(alias, default)`, `require_secret(alias)` | resolved secrets by alias |
| `http` / `build_http_client()` / `request(method, url, **kw)` | managed `httpx.AsyncClient` with error mapping |
| `probe(coro_fn, detail="ok") -> HealthStatus` | wraps a check with latency; never raises |
| `async health() -> HealthStatus` | **abstract**, must not raise |
| `async quota() -> QuotaInfo` | provider-reported usage. The default returns `unit="unknown", source="router"`. Override when the provider exposes usage. Storage default: config `bytes` limit with `source="config"` |
| `async close()` | closes `self.http`; extend it when you hold other clients/pools |

`ProviderKind`: `llm | storage | db | compute | dns | cron`.

`Capability`: `chat, stream, list_models, embed, rerank, transcribe, tools, vision` (LLM), `presign` (storage),
`sql` (DB accepts SQL text), `power` (compute start/stop), `dns_records`, `cron_trigger`.

### Port method sets

| port | abstract (required) | optional (declare capability) | default capabilities |
|---|---|---|---|
| `LLMAdapter` | `chat(ChatRequest)->ChatResponse`, `stream(ChatRequest)->AsyncIterator[ChatChunk]` (an async generator), `list_models()->list[ModelInfo]` | `embed(EmbeddingRequest)->EmbeddingResponse` [embed], `rerank(RerankRequest)->RerankResponse` [rerank], `transcribe(TranscriptionRequest)->TranscriptionResponse` [transcribe] | chat, stream, list_models |
| `StorageAdapter` | `put(key, data: bytes, content_type=None, metadata=None)->StoredObject`, `get(key)->bytes` (missing: `ProviderError(status_code=404)`), `delete(key)`, `list(prefix="", limit=1000)->list[StoredObject]`, `stat(key)->StoredObject\|None` | `presign(key, expires_s=3600, method="GET")->str` [presign] | (none) |
| `DBAdapter` | `list_tables()->list[TableInfo]`, `describe(table)->TableInfo`, `query(statement, params=None, limit=500)->QueryResult` (read), `execute(statement, params=None)->QueryResult` (write), `usage()->QuotaInfo` (storage bytes) | (none; declare `sql` if statements are SQL) | (none) |
| `ComputeAdapter` | `list_instances()->list[VMInstance]`, `status(instance_id)->VMInstance` | `start(id)`, `stop(id)` -> `VMInstance` [power] | (none) |
| `DNSAdapter` | `toggle_blocking(enabled, duration_s=None)->DNSStatus`, `status()->DNSStatus`, `query_log(limit=100, blocked_only=False)->list[DNSQueryLogEntry]` | `list_records()->list[DNSRecord]` [dns_records] | (none) |
| `CronAdapter` | `create(CronJob)->CronJob`, `list()->list[CronJob]`, `delete(job_id)`, `trigger(job_id)` | (none) | cron_trigger |

`ADAPTER_BASES: dict[ProviderKind, type]` maps each kind to its port.

How non-obvious providers map onto the ports:
* Tailscale devices are `VMInstance`s with state `online`/`offline` and no power capability.
* Vercel, Netlify and GitHub Pages projects/sites are `VMInstance`s (state `deploying`/`running`/`error`, `url`).
* Koyeb and Cloudflare Workers services/scripts are instances. Koyeb pause/resume = `power`. (Koyeb is disabled by default:
  no free tier for new signups since 2026. Upstash Kafka was removed: discontinued by Upstash on 2025-03-11.)
* For Redis, Mongo, Firestore, Cloudant and DynamoDB, keyspaces/collections/databases/tables map to `TableInfo.kind`
  (`keyspace|topic|collection|table`). `query`/`execute` take the provider's native statement: Redis command line or JSON array,
  Mongo JSON command document, DynamoDB PartiQL, Cloudant Mango JSON. Document the format in the adapter docstring.

### Models (pydantic v2)

* `HealthStatus(provider, ok, latency_ms?, detail="", checked_at)`
* `QuotaInfo(provider, used=0, limit?, unit="requests", window?, reset_at?, source: provider|router|config, extra={})` plus the properties `remaining` and `fraction_used`.
* `QuotaLimit(metric, window: minute|hour|day|month|total, limit)`. Metrics: `requests` (alias `calls`), `tokens`, `bytes`, `neurons`, `minutes`, ...
* `PluginMeta(display_name, description, required_secrets, optional_secrets, docs_url)`
* `PluginContext(name, kind, settings, secrets{alias:value}, secret_names{alias:VAULT_KEY}, limits, timeout_s, http_transport)`
* `ChatMessage(role: system|user|assistant|tool, content: str|list[dict]|None, name?, tool_call_id?, tool_calls?)` (extra fields allowed)
* `ChatRequest(messages, model?, temperature?, top_p?, max_tokens?, stop?, tools?, tool_choice?, response_format?, stream=False, user?, provider?, allow_fallback=True, extra={})`.
  `model` None or `"auto"` means the provider's `settings.default_model`. `provider` pins the request to a plugin name. `extra` is merged into the provider body.
* `Usage(prompt_tokens, completion_tokens, total_tokens)`
* `ChatResponse(id, model, content?, role, finish_reason?, tool_calls?, usage, routed_via, attempts[str], latency_ms?, created_at)`
* `ChatChunk(delta, model, finish_reason?, tool_calls?, usage?, routed_via)`
* `ModelInfo(id, provider, owned_by?, context_window?, capabilities[])`
* `EmbeddingRequest(input[str], model?, provider?, input_type?)` and `EmbeddingResponse(model, vectors, usage, routed_via)`
* `RerankRequest(query, documents, model?, top_n?, provider?)`, `RerankResponse(model, results[RerankResult(index, relevance_score)], routed_via)`
* `TranscriptionRequest(audio: bytes, filename, model?, language?, provider?)` and `TranscriptionResponse(model, text, routed_via)`
* `StoredObject(key, size, content_type?, etag?, last_modified?, backend, url?, metadata{})`
* `ColumnInfo(name, type, nullable, primary_key, default?)` and `TableInfo(name, schema_name?, kind, row_count?, size_bytes?, columns[])`
* `QueryResult(columns[str], rows[list], row_count, affected_rows?, elapsed_ms?, backend, truncated)`
* `VMInstance(id, name, provider, state, region?, shape?, public_ip?, private_ip?, tailscale_ip?, url?, tags{}, meta{})`.
  `state` is one of `running|stopped|starting|stopping|provisioning|terminated|sleeping|deploying|error|online|offline|unknown`.
* `DNSRecord(name, type, value, ttl?)`, `DNSStatus(provider, blocking_enabled, blocking_resumes_at?, queries_today?, blocked_today?, percent_blocked?, domains_on_blocklist?)`,
  `DNSQueryLogEntry(timestamp, domain, client?, query_type?, status?, blocked)`
* `CronJob(id, name, schedule (5-field cron, UTC), target (URL or workflow file), method="POST", headers{}, body?, enabled, provider, last_run_at?, next_run_at?)`

### Exceptions

```
ProviderError(message, provider=, status_code=, retry_after=)
├── QuotaExhausted          HTTP 429/402 or a free-tier limit reached       -> failover, cooldown
├── ProviderUnavailable     network error / 5xx / 408                       -> failover, counts toward breaker
│   └── ProviderTimeout     timeout                                          -> failover, counts toward breaker
├── NotConfigured           missing secret/setting, unknown/disabled plugin -> skipped
│   ├── ProviderAuthError   401/403
│   └── PluginLoadError     module/class import or validation failed
├── CapabilityNotSupported  optional op called without the capability
└── NoProviderAvailable(message, kind=, attempts=[...])   router: the whole chain failed
```

Helpers: `raise_for_response(httpx.Response, provider)` maps status codes to the classes above and parses `Retry-After`.
`translate_transport_error(exc, provider)` turns httpx/asyncio errors into `ProviderTimeout`/`ProviderUnavailable`.
Map provider-specific "quota" bodies (for example a 400 that says "quota exceeded") to `QuotaExhausted` yourself.

### OpenAI-compatible base (`core/openai_compat.py`)

`OpenAICompatibleAdapter(LLMAdapter)` implements chat, SSE stream, list_models, embed (only when `Capability.EMBED` is declared) and health.
Most LLM plugins should subclass it and add `meta`/`capabilities`, overriding `auth_headers()`, `resolve_base_url()`,
`build_body()` or `routed_via_from()` only where needed. The settings it reads are:
* `base_url`: may contain `{placeholders}` filled from settings and secret aliases, e.g. `{account_id}`.
* `default_model`, `default_embedding_model`.
* `chat_path`, `models_path`, `embeddings_path`.
* `extra_headers`.
* `auth`: `bearer`, which uses secret alias `api_key`, or `none`.
* `stream_usage`.
* `routed_via_header`: when this response header is present, `routed_via` becomes `"<name>:<header value>"`.

## 3. Registry (`core/registry.py`) and the providers.yaml schema

```yaml
version: 1
defaults:
  timeout_s: 30.0              # per-attempt timeout (overridable per plugin: timeout_s or settings.timeout_s)
  quota_cooldown_s: 300.0      # cooldown after a 429 without Retry-After
  storage_strategy: most_free  # most_free | priority
  llm_model_fallback: true     # requested model not available -> other providers with their default model
  circuit_breaker: {failure_threshold: 3, cooldown_s: 30.0, max_cooldown_s: 600.0}
notes: ...                     # extra top-level keys are preserved on save (e.g. hosts:)
plugins:
  <name>:                      # ^[a-z][a-z0-9_]*$, unique, used everywhere as the provider id
    module: plugins.<group>.<file>:<ClassName>
    kind: llm|storage|db|compute|dns|cron
    group: llm_pool|oracle|gcp|cloudflare|aws|databases|storage|compute_edge|network
    enabled: true
    priority: 10               # lower = tried first within its kind
    description: "..."
    limits: [{metric: requests, window: day, limit: 1000}, ...]
    secrets: {alias: VAULT_KEY}            # required; list form [KEY] means alias == key
    optional_secrets: {alias: VAULT_KEY}
    settings: {...}                        # free-form, adapter-specific (base_url, models, bucket, ...)
    timeout_s: 60                          # optional
```

LLM settings the router reads: `models`, `embedding_models`, `rerank_models` and `transcription_models` (model ids hosted by the provider),
plus `accepts_any_model: true` (FreeLLMAPI).
`capacity_bytes` is the convention for storage capacity. A `bytes` limit with `window: total` is the capacity used by the router.

`PluginRegistry(path, *, secret_lookup=Callable[[str], str|None], bus=EventBus, transport_factory=None)`:

* `PluginRegistry.from_file(path, **kw)`, `load()`, `await reload()`
* `specs: dict[str, PluginSpec]`, `spec(name)` (raises `NotConfigured` for an unknown name), `list(kind=None, enabled_only=False)` (sorted),
  `chain(kind) -> [names]` (enabled, priority asc, then name), `timeout_for(name)`, `missing_secrets(name) -> [VAULT_KEY]`,
  `load_error(name)`, `describe(name) -> dict`, `class_meta(name) -> dict` (`available`, `capabilities`, `PluginMeta` fields)
* `await get(name, allow_disabled=False) -> ProviderAdapter`: a cached instance. Raises `NotConfigured` when the plugin is disabled and `PluginLoadError` when it cannot load.
* Runtime mutations, all persisted atomically to the YAML (temp file + `os.replace`; comments are not kept, so documentation lives in `description`):
  `await set_enabled(name, bool)`, `await set_priority(name, int)`,
  `await reorder(kind, [names first...]) -> chain` (rewrites priorities 10, 20, ...), `await update_settings(name, patch)` (a `None` value deletes the key; the plugin is re-instantiated).
  Pass `persist=False` to skip writing.
* `await close()` closes every instance.

`PluginSpec` fields: `name, module, kind, group, enabled, priority, description, limits, secrets, optional_secrets, settings, timeout_s`,
plus the properties `module_path` and `class_name`.

## 4. Router (`core/router.py`)

`SmartRouter(registry, QuotaStore(path), bus=None (defaults to registry.bus), *, clock=time.time, capacity_ttl_s=60)`. Call `await start()` / `await close()`, or use it as an async context manager.

* Generic: `await execute(kind, op, *, candidates=None, provider=None, allow_fallback=True, capability=None, op_name="call") -> Routed[T]`,
  where `op` is `async (adapter) -> T`. `Routed(result, provider, attempts: list[Attempt], latency_ms)` and `Attempt(provider, outcome, detail, latency_ms)`.
  Outcomes are `ok|quota|unavailable|timeout|error|not_configured|unsupported|circuit_open`.
  `await call(name, op, op_name=, capability=)` targets a single provider (no failover).
  `await adapter(name)` gives raw access to an adapter (DB explorer, compute and DNS actions).
* LLM: `await chat(ChatRequest) -> ChatResponse`. The router fills `routed_via` (plugin name, or `"freellmapi:groq"` when the adapter reports an upstream), `attempts` and `latency_ms`.
  `stream(ChatRequest)` is an async iterator of `ChatChunk`. It fails over only before the first chunk. Errors after that are raised as `ProviderError`.
  Also `await embed(...)`, `await rerank(...)`, `await transcribe(...)` and `await list_models() -> {provider: [ModelInfo]}` (falls back to `settings.models`).
  Model routing: when `request.model` is listed by some providers, those go first. The rest follow with `model=None` (their default) when `llm_model_fallback` is set.
* Storage: `await storage_put(key, data, content_type=None, metadata=None, *, provider=None)` picks the backend with the most free capacity that fits.
  Capacity comes from `adapter.quota()` with `unit="bytes", source="provider"`, cached 60 s. The fallback is the router byte counter against the `bytes` limit. Backends with unknown capacity go last.
  Other storage calls: `storage_get/stat/delete/presign(key, provider)`, `storage_list(prefix, providers=None) -> {name: [StoredObject] | "error"}`,
  `storage_locate(key) -> StoredObject|None` and `storage_capacity(name) -> QuotaInfo`.
* Quota: `await record_usage(name, metric, amount)` (always records day+month windows, plus the windows of matching limits),
  `await usage(name) -> [QuotaInfo]` (router counters), `await provider_quota(name)` and `await quotas(kind=None) -> {name: [QuotaInfo]}`
  (provider-reported entry first, then router counters).
  The router counts `requests` (one per attempt that reached the provider) and `tokens` (`usage.total_tokens`). Adapters report every other metric through `quota()`.
* Health and status: `await health_check(name)`, `await health_all(kind=None)`, and `await status(kind=None) -> [ProviderStatus]`.
  `ProviderStatus` carries `name, kind, group, enabled, priority, breaker, exhausted_until, last_error, last/avg_latency_ms, successes, failures, missing_secrets, load_error, last_health, usage`.
  `reset_provider(name)` closes the breaker and clears the quota cooldown.
* Failover policy, applied per attempt in chain order:
  1. Skip a provider that is in a quota cooldown, cannot be loaded or configured, lacks the capability, has reached a local limit (non-capacity metric) or has an open breaker.
  2. Run the op with `asyncio.wait_for(timeout_for(name))`.
  3. `QuotaExhausted` sets a cooldown of `retry_after`, or `quota_cooldown_s` when there is none.
  4. `ProviderUnavailable`, timeouts, httpx errors and unexpected exceptions count toward the breaker.
  5. A generic `ProviderError` fails over without tripping the breaker.
  6. `NotConfigured` is skipped.
  7. When everything fails, the router raises `NoProviderAvailable` with the attempt trail.
* Circuit breaker (`CircuitBreaker`): opens after `failure_threshold` consecutive failures, for `cooldown_s`. It then allows a single half-open trial.
  Success closes it. Failure reopens it with double the cooldown, capped at `max_cooldown_s`.

Quota store (`core/quota.py`): SQLite (aiosqlite) at `data/quota.db`, table `counters(provider, metric, window, window_start, used, updated_at)`.
UTC windows: minute `YYYY-MM-DDTHH:MM`, hour, day `YYYY-MM-DD`, month `YYYY-MM`, total `all`. API: `QuotaStore(path)` with
`open/close/add/used/snapshot/reset`, `window_start(window, now)` and `window_reset_at(window, now)`.

## 5. Event bus (`core/events.py`)

`EventBus(history=500)` exposes `publish(type, *, provider=None, kind=None, **data) -> Event` (non-blocking), `emit(Event)`,
`subscribe(types=None, maxsize=1000) -> Subscription`, `recent(limit, types)` and `subscriber_count`.
Patterns are an exact type, a `"prefix.*"` or `"*"`. A `Subscription` is an async iterator and an async context manager, with `get(timeout)` and `close()`.
A slow consumer loses its oldest events (`dropped` counter).
`Event(id, type, ts, provider, kind, data)` is JSON-serialisable with `event.model_dump(mode="json")`, which is what hub_api sends over WebSocket.

| `EventType` | value | data keys |
|---|---|---|
| REQUEST_SUCCEEDED | `router.success` | op, latency_ms, attempts |
| REQUEST_FAILED | `router.attempt_failed` | op, outcome, error, latency_ms |
| FAILOVER | `router.failover` | op, from_provider, to_provider |
| ALL_FAILED | `router.exhausted` | op, attempts |
| REQUEST_STARTED | `router.request` | reserved (hub may publish) |
| QUOTA_UPDATED | `quota.updated` | metric, amount |
| QUOTA_EXHAUSTED | `quota.exhausted` | source (provider/router), retry_after/reset_at, metric/window/used/limit |
| BREAKER_OPENED / BREAKER_HALF_OPEN / BREAKER_CLOSED | `breaker.opened` / `breaker.half_open` / `breaker.closed` | cooldown_s, error |
| HEALTH_CHECKED | `health.checked` | ok, latency_ms, detail |
| PLUGIN_LOADED / PLUGIN_LOAD_FAILED | `registry.plugin_loaded` / `registry.plugin_load_failed` | module / error |
| PLUGIN_ENABLED / PLUGIN_DISABLED / PLUGIN_UPDATED | `registry.enabled` / `registry.disabled` / `registry.updated` | (none) |
| CHAIN_REORDERED | `registry.reordered` | chain |
| REGISTRY_RELOADED | `registry.reloaded` | count |
| OBJECT_STORED / OBJECT_DELETED | `storage.stored` / `storage.deleted` | key, size |
| INSTINCT_TASK_CREATED / _STEP / _SCREENSHOT / _COMPLETED / _FAILED | `instinct.task.*` | task_id, plus step text / `image_b64` (JPEG) / result / error (published by instinct) |
| DNS_BLOCKING_CHANGED | `dns.blocking_changed` | enabled, duration_s (published by hub_api) |
| COMPUTE_STATE_CHANGED | `compute.state_changed` | instance_id, state (published by hub_api) |

## 6. Vault (`core/vault.py`)

The encrypted file `.env.vault` at the project root (override with `OMNIFREE_VAULT_PATH`) is JSON. The key comes from scrypt
(n=2^15, r=8, p=1, random 16-byte salt) and the payload is encrypted with AES-256-GCM (random 12-byte nonce). The header is authenticated as AAD.
The passphrase comes from `OMNIFREE_VAULT_PASSPHRASE`.

* API: `Vault.load(path=None, passphrase=None, missing_ok=True)`, `Vault.init(path, passphrase, overwrite=False)`, `get(key, default)`, `set(key, value)`,
  `delete(key) -> bool`, `items()`, `keys()`, `as_dict()`, `save()` (atomic, fresh salt and nonce), `export_env()`, `import_env(text)`. Errors raise `VaultError`.
* CLI: `python -m core.vault [--path P] init | set KEY [VALUE] | get KEY | delete KEY | list | export-env | import-env FILE`.
  When `set` gets no VALUE it reads stdin, or prompts if stdin is a TTY. Exit codes: 0 ok, 1 key not found, 2 vault error.

## 7. Config (`core/config.py`)

`Settings` (pydantic-settings, env prefix `OMNIFREE_`, optional `.env`) has these fields: `root_dir`, `providers_file=config/providers.yaml`, `data_dir=data`,
`quota_db=data/quota.db`, `vault_path=.env.vault`, `vault_passphrase` (`OMNIFREE_VAULT_PASSPHRASE`), `vault_required=False`, `environment`,
`log_level`, `hub_host=0.0.0.0`, `hub_port=8080`, `hub_api_token`, `cors_origins` (CSV or JSON list), `request_timeout_s`, `health_interval_s`.
Relative paths resolve against `root_dir` through the `providers_path`, `data_path`, `quota_db_path` and `vault_file` properties.

* `settings.secret(NAME)`: the process env var `NAME` first, then the vault. Empty values count as missing. `require_secret(NAME)` raises `NotConfigured`.
* `settings.hub_token()` returns `OMNIFREE_HUB_API_TOKEN` or the vault key `HUB_API_TOKEN`. `settings.reload_vault()` re-reads the vault.
* `get_settings()` returns a cached singleton.

Wiring (hub_api):

```python
settings = get_settings()
bus = EventBus()
registry = PluginRegistry.from_file(settings.providers_path, secret_lookup=settings.secret, bus=bus)
router = SmartRouter(registry, QuotaStore(settings.quota_db_path), bus)
await router.start()
```

## 8. Non-plugin vault keys

| key | used by |
|---|---|
| `HUB_API_TOKEN` | hub_api bearer auth (PWA and Instinct) |
| `OMNIFREE_VAULT_PASSPHRASE` | env only, never stored in the vault |
| `CLOUDFLARE_API_TOKEN` | also used by automation/deploy for Workers/Pages |
| `FREELLMAPI_ENCRYPTION_KEY`, `FREELLMAPI_ADMIN_EMAIL`, `FREELLMAPI_ADMIN_PASSWORD`, `FREELLMAPI_DASHBOARD_TOKEN`, `FREELLMAPI_LICENSE` | plugins/llm_pool/freellmapi_injector (container config + admin API); `FREELLMAPI_API_KEY` is the plugin secret |
| `INSTINCT_API_TOKEN` | bearer token hub_api presents to the Instinct service (and Instinct requires) |
| `TAILSCALE_AUTHKEY`, `TAILSCALE_HUB_AUTHKEY` | automation/deploy (VM join, Cloud Run userspace tailscaled) |
| `FRPS_TOKEN` | frps on AMD VM2 (deploy-edge) |
| `PIHOLE_PASSWORD` | Pi-hole web/API password (deploy-pihole; also the `pihole` plugin secret) |
| `ORACLE_BLOCK_WEBDAV_USER`, `ORACLE_BLOCK_WEBDAV_PASSWORD` | rclone WebDAV on the block volume (deploy-arm; `oracle_block_storage` plugin) |
| `HEALTHMONITOR_ALERT_WEBHOOK_URL`, `HEALTHMONITOR_HEARTBEAT_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | healthmonitor alerts and dead-man's switch |
| `HEALTHMONITOR_STATUS_TOKEN` | bearer token for the full healthmonitor `/status` report (falls back to `HUB_API_TOKEN`; without it only up/down is shown) |
| `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY` | verification of QStash-signed cron callbacks (`QSTASH_TOKEN` is the plugin secret) |

## 9. Plugin table (implement exactly these names)

| name | kind | module:Class | file | required secrets (alias = VAULT_KEY) | optional secrets |
|---|---|---|---|---|---|
| freellmapi | llm | `plugins.llm_pool.freellmapi:FreeLLMAPIAdapter` | plugins/llm_pool/freellmapi.py | api_key=FREELLMAPI_API_KEY | - |
| groq | llm | `plugins.llm_pool.groq:GroqAdapter` | plugins/llm_pool/groq.py | api_key=GROQ_API_KEY | - |
| cerebras | llm | `plugins.llm_pool.cerebras:CerebrasAdapter` | plugins/llm_pool/cerebras.py | api_key=CEREBRAS_API_KEY | - |
| gemini | llm | `plugins.llm_pool.gemini:GeminiAdapter` | plugins/llm_pool/gemini.py | api_key=GEMINI_API_KEY | - |
| mistral | llm | `plugins.llm_pool.mistral:MistralAdapter` | plugins/llm_pool/mistral.py | api_key=MISTRAL_API_KEY | codestral_api_key=CODESTRAL_API_KEY |
| openrouter | llm | `plugins.llm_pool.openrouter:OpenRouterAdapter` | plugins/llm_pool/openrouter.py | api_key=OPENROUTER_API_KEY | - |
| sambanova | llm | `plugins.llm_pool.sambanova:SambaNovaAdapter` | plugins/llm_pool/sambanova.py | api_key=SAMBANOVA_API_KEY | - |
| nvidia_nim | llm | `plugins.llm_pool.nvidia_nim:NvidiaNIMAdapter` | plugins/llm_pool/nvidia_nim.py | api_key=NVIDIA_API_KEY | - |
| zai | llm | `plugins.llm_pool.zai:ZAIAdapter` | plugins/llm_pool/zai.py | api_key=ZAI_API_KEY | - |
| cloudflare_ai | llm | `plugins.llm_pool.cloudflare_ai:CloudflareWorkersAIAdapter` | plugins/llm_pool/cloudflare_ai.py | account_id=CLOUDFLARE_ACCOUNT_ID, api_key=CLOUDFLARE_AI_API_TOKEN | - |
| cohere | llm | `plugins.llm_pool.cohere:CohereAdapter` | plugins/llm_pool/cohere.py | api_key=COHERE_API_KEY | - |
| ollama | llm | `plugins.llm_pool.ollama:OllamaAdapter` | plugins/llm_pool/ollama.py | - (settings.auth: none) | - |
| oracle_compute | compute | `plugins.oracle.compute:OCIComputeAdapter` | plugins/oracle/compute.py | tenancy_ocid=OCI_TENANCY_OCID, user_ocid=OCI_USER_OCID, fingerprint=OCI_FINGERPRINT, private_key=OCI_PRIVATE_KEY_PEM | - |
| oracle_adb_1 | db | `plugins.oracle.autonomous_db:OracleAutonomousDBAdapter` | plugins/oracle/autonomous_db.py | user=ORACLE_ADB1_USER, password=ORACLE_ADB1_PASSWORD, dsn=ORACLE_ADB1_DSN | wallet_password=ORACLE_ADB1_WALLET_PASSWORD |
| oracle_adb_2 | db | `plugins.oracle.autonomous_db:OracleAutonomousDBAdapter` | plugins/oracle/autonomous_db.py | user=ORACLE_ADB2_USER, password=ORACLE_ADB2_PASSWORD, dsn=ORACLE_ADB2_DSN | wallet_password=ORACLE_ADB2_WALLET_PASSWORD |
| oracle_block_storage | storage | `plugins.oracle.block_storage:OracleBlockVolumeStorageAdapter` | plugins/oracle/block_storage.py | - | webdav_user=ORACLE_BLOCK_WEBDAV_USER, webdav_password=ORACLE_BLOCK_WEBDAV_PASSWORD |
| gcp_cloud_run | compute | `plugins.gcp.cloud_run:CloudRunAdapter` | plugins/gcp/cloud_run.py | service_account_json=GCP_SERVICE_ACCOUNT_JSON | - |
| gcp_e2_micro | compute | `plugins.gcp.compute_engine:GCEComputeAdapter` | plugins/gcp/compute_engine.py | service_account_json=GCP_SERVICE_ACCOUNT_JSON | - |
| gcp_firestore | db | `plugins.gcp.firestore:FirestoreAdapter` | plugins/gcp/firestore.py | service_account_json=GCP_SERVICE_ACCOUNT_JSON | - |
| gcp_gcs | storage | `plugins.gcp.gcs:GCSStorageAdapter` | plugins/gcp/gcs.py | service_account_json=GCP_SERVICE_ACCOUNT_JSON | - |
| cloudflare_workers | compute | `plugins.cloudflare.workers:CloudflareWorkersAdapter` | plugins/cloudflare/workers.py | account_id=CLOUDFLARE_ACCOUNT_ID, api_token=CLOUDFLARE_API_TOKEN | - |
| cloudflare_r2 | storage | `plugins.cloudflare.r2:R2StorageAdapter` | plugins/cloudflare/r2.py | account_id=CLOUDFLARE_ACCOUNT_ID, access_key_id=R2_ACCESS_KEY_ID, secret_access_key=R2_SECRET_ACCESS_KEY | - |
| cloudflare_d1 | db | `plugins.cloudflare.d1:D1Adapter` | plugins/cloudflare/d1.py | account_id=CLOUDFLARE_ACCOUNT_ID, api_token=CLOUDFLARE_API_TOKEN | - |
| aws_dynamodb | db | `plugins.aws.dynamodb:DynamoDBAdapter` | plugins/aws/dynamodb.py | access_key_id=AWS_ACCESS_KEY_ID, secret_access_key=AWS_SECRET_ACCESS_KEY | - |
| supabase | db | `plugins.databases.supabase:SupabaseAdapter` | plugins/databases/supabase.py | service_role_key=SUPABASE_SERVICE_ROLE_KEY | db_url=SUPABASE_DB_URL |
| neon | db | `plugins.databases.neon:NeonAdapter` | plugins/databases/neon.py | database_url=NEON_DATABASE_URL | api_key=NEON_API_KEY |
| turso | db | `plugins.databases.turso:TursoAdapter` | plugins/databases/turso.py | database_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN | - |
| mongodb_atlas | db | `plugins.databases.mongodb_atlas:MongoAtlasAdapter` | plugins/databases/mongodb_atlas.py | uri=MONGODB_URI | - |
| upstash_redis | db | `plugins.databases.upstash_redis:UpstashRedisAdapter` | plugins/databases/upstash_redis.py | rest_url=UPSTASH_REDIS_REST_URL, rest_token=UPSTASH_REDIS_REST_TOKEN | - |
| ibm_cloudant | db | `plugins.databases.cloudant:CloudantAdapter` | plugins/databases/cloudant.py | url=CLOUDANT_URL, apikey=CLOUDANT_APIKEY | - |
| backblaze_b2 | storage | `plugins.storage.backblaze_b2:B2StorageAdapter` | plugins/storage/backblaze_b2.py | key_id=B2_KEY_ID, application_key=B2_APPLICATION_KEY | - |
| koyeb | compute | `plugins.compute_edge.koyeb:KoyebAdapter` | plugins/compute_edge/koyeb.py | api_token=KOYEB_API_TOKEN | - |
| vercel | compute | `plugins.compute_edge.vercel:VercelAdapter` | plugins/compute_edge/vercel.py | token=VERCEL_TOKEN | team_id=VERCEL_TEAM_ID |
| netlify | compute | `plugins.compute_edge.netlify:NetlifyAdapter` | plugins/compute_edge/netlify.py | token=NETLIFY_TOKEN | - |
| github_pages | compute | `plugins.compute_edge.github_pages:GitHubPagesAdapter` | plugins/compute_edge/github_pages.py | token=GITHUB_TOKEN | - |
| github_actions | cron | `plugins.compute_edge.github_actions:GitHubActionsCronAdapter` | plugins/compute_edge/github_actions.py | token=GITHUB_TOKEN | - |
| upstash_qstash | cron | `plugins.compute_edge.qstash:QStashCronAdapter` | plugins/compute_edge/qstash.py | token=QSTASH_TOKEN | current_signing_key=QSTASH_CURRENT_SIGNING_KEY, next_signing_key=QSTASH_NEXT_SIGNING_KEY |
| pihole | dns | `plugins.network.pihole:PiHoleDNSAdapter` | plugins/network/pihole.py | password=PIHOLE_PASSWORD | - |
| tailscale | compute | `plugins.network.tailscale:TailscaleAdapter` | plugins/network/tailscale.py | api_key=TAILSCALE_API_KEY | - |

Notes per group:
* llm_pool: every adapter except `cohere` can subclass `OpenAICompatibleAdapter`. Cohere uses the native v2 API (`/v2/chat`, `/v2/embed`, `/v2/rerank`).
  Groq declares `transcribe` (`/audio/transcriptions`, multipart). Gemini, Mistral and Cloudflare AI declare `embed`. Cohere declares `embed` and `rerank`.
  Cloudflare AI sends `Authorization: Bearer <api_key>` and fills `{account_id}` in `base_url`. Mistral uses `codestral_base_url`/`codestral_api_key` for `codestral-*` models when that key is set.
  OpenRouter must refuse non-`:free` models when `settings.free_only` is set: raise `ProviderError` (status 400). Cloudflare AI `quota()` may report neurons when the API exposes them.
* Storage adapters must override `quota()` with `unit="bytes", source="provider"` when the provider reports usage.
  Otherwise they keep the default (config capacity) and the router counts bytes it wrote.
* The shared GCP adapter auth helper may live in `plugins/gcp/_auth.py`. AWS SigV4 or R2/B2 S3 signing helpers may live in the group dir as `_sigv4.py` etc.
  Private helper modules (prefix `_`) inside your group directory are allowed.
* `settings.billing_alert_usd: 1` marks providers where `automation/deploy.py` must create a $1 budget alert (OCI, GCP, AWS, B2).

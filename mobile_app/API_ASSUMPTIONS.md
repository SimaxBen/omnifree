# mobile_app: hub_api contract

> Reconciled with `hub_api/routers/*` by the integrator (2026-10). Where the PWA's first guesses differed
> from hub_api, the PWA was changed to match; this file documents the routes as implemented.

Every HTTP and WebSocket call the PWA makes is listed here. The client lives in `src/lib/api.ts`
(WebSocket: `src/lib/events.ts`). The payload types in `src/lib/types.ts` mirror the core pydantic
models (`core/interfaces.py`, `core/router.py`, `core/events.py`) serialised with `model_dump(mode="json")`.
The normalisers still tolerate a few alternative shapes (flat arrays, `{data: [...]}`), but the shapes
below are what hub_api returns.

## Cross-cutting

| topic | contract |
|---|---|
| Base URL | Set in Settings (localStorage `omnifree.settings.v1`). The default is `VITE_HUB_URL` at build time (`<edge>/api` when deployed behind the Cloudflare Worker), then `window.location.origin`. |
| Auth (HTTP) | `Authorization: Bearer <HUB_API_TOKEN>` on every request, including multipart upload (XHR) and download. hub_api also accepts `?access_token=`. No token configured on the hub -> 503 on every protected route. |
| Auth (WS) | Browsers cannot set headers on a WebSocket, so the token travels as the subprotocol pair `["bearer", token]` (kept out of the URL, which Cloud Run request logs record). Only a token with characters that are illegal in a subprotocol falls back to `/ws?token=<HUB_API_TOKEN>`. |
| CORS | hub_api allows `OMNIFREE_CORS_ORIGINS` and exposes `X-Routed-Via`, `X-Storage-Backend`, `Retry-After`, `Content-Disposition`. |
| Errors | Non-2xx responses carry `{"detail": "..."}`; provider errors add `error`, `provider`, `status`, `retry_after?`, `attempts?`. Mapping (`hub_api/errors.py`): `NoProviderAvailable` 503, `QuotaExhausted` 429, `ProviderTimeout` 504, `ProviderUnavailable` 503, `ProviderAuthError` 502, `NotConfigured` 409 (unknown plugin 404), `CapabilityNotSupported` 501, provider 404 -> 404, other provider 4xx -> 400. |
| Datetimes | ISO 8601 strings in UTC. |

## System

| method + path | request | response | used by |
|---|---|---|---|
| `GET /healthz` | none (unauthenticated; the token is sent anyway) | 2xx JSON | Settings: "Test connection" (reachability) |
| `GET /events/recent` | none | recent bus events | not used by the PWA yet |

## LLM pool

| method + path | request | response | notes |
|---|---|---|---|
| `POST /llm/chat` | `ChatRequest` JSON: `{messages:[{role,content}], model: str\|null, provider: str\|null, allow_fallback: bool, stream: false}` | `ChatResponse` JSON plus the `X-Routed-Via` header (e.g. `freellmapi:groq`) | `model: null` = auto. `attempts[]` is displayed as the failover trail, `latency_ms`/`usage` as badges. |
| `POST /llm/stream` | same `ChatRequest` with `stream: true` | `text/event-stream`: `event: chunk` + `data: <ChatChunk JSON>` per chunk, then `event: done` (`{routed_via, chunks}`) or, on a mid-stream failure, `event: error` (`{detail, ...}`). A failure before the first chunk is a normal HTTP error. `X-Routed-Via` is a response header (the hub awaits the first chunk before replying). | The client also tolerates OpenAI-style `data: [DONE]`. This is a POST, so the client uses fetch and a ReadableStream, not EventSource. |
| `GET /llm/quotas` | optional `?kind=` (default `llm`) | `{kind, providers: {name: {quotas: QuotaInfo[] (+remaining, fraction_used), exhausted_until, breaker}}}` | The gauges use the highest `used/limit` per provider. |
| `GET /llm/models` | none | `{providers: {name: ModelInfo[]}, chain: [names]}` | |
| `POST /llm/embed` | `EmbeddingRequest` | `EmbeddingResponse` + `X-Routed-Via` | not used by the PWA yet |
| `POST /llm/transcribe` | `multipart/form-data`: `file` (required), `model?`, `language?` | `{text, model?, language?, duration?}` | Chat panel, mic button: "Transcribe" card (audio file or MediaRecorder voice note); "Send to chat" puts the text in the message box. |
| `POST /llm/rerank` | `{query, documents: str[], top_n?}` | `{results: [{index, score, document?}]}` | `HubClient.rerank()`; no UI yet. |
| `GET /chats?q=&limit=` | none | `{chats: ChatSummary[]}` (`id, title, title_source: auto\|llm\|user, model, message_count, preview, pinned, created_at, updated_at`), pinned then newest first | `chatList()`: chat history sheet (search, Today / Yesterday / This week / Older). |
| `PUT /chats/{id}` | `{messages: [{role, content, routed_via?, model?}], model?, title?, pinned?}` | 201 (created) or 200 + `ChatThread` (summary + `messages`) | `chatSave()`: idempotent upsert under a client id after every exchange; unsynced chats are kept in localStorage and retried (offline, 5xx, 404/405 from an older hub). 413 past ~1.8 MB. |
| `GET/PATCH/DELETE /chats/{id}`, `POST /chats` | PATCH `{title?, pinned?, model?, append?: messages}` | `ChatThread` / 204 | `chatGet()` reopens a chat; `chatUpdate()` renames (blank = automatic title) and pins; `chatDelete()`. |
| `POST /chats/{id}/title` | none | `ChatThread` | `chatAutoTitle()` after the first saved exchange: one small LLM call for a 3-6 word title; keeps the current title on failure or when renamed. |

## Agent (Instinct)

| method + path | request | response | notes |
|---|---|---|---|
| `GET /agent/tasks` | none | `{items: AgentTask[]}` (summaries with `step_count`, no `steps`) | Polled every 15 s and refreshed on `instinct.task.created/completed/failed`. |
| `POST /agent/tasks` | `{goal: str, start_url?: str, model?: str, max_steps?: int (1-200)}` | 202 + `AgentTask` summary | Proxied to Instinct. 503 when `OMNIFREE_INSTINCT_URL` is unset, 502/504 when Instinct is unreachable. |
| `GET /agent/tasks/{id}` | none | `AgentTask` with `steps: StepRecord[]` | |
| `POST /agent/tasks/{id}/cancel` | none | `AgentTask` summary | Shown while the status is `queued` or `running`. |
| `GET /agent/tasks/{id}/screenshot` | none | `image/jpeg` | Latest frame kept by the hub. |

`AgentTask` = Instinct `TaskState` (`instinct/models.py`): `{id, goal, status: queued|running|completed|failed|cancelled, start_url, model, max_steps, created_at, started_at, finished_at, result, error, screenshot_step, step_count | steps}`.
`StepRecord` = `{step: int, action?: {action, url?, element?, selector?, text?}, result, error?, url?, routed_via?, at}`.

## Unified Drive

| method + path | request | response | notes |
|---|---|---|---|
| `GET /drive/files?prefix=&provider=&limit=` | query | `{items: StoredObject[], count, errors: {provider: msg}}` | Used when the search box is empty. |
| `GET /drive/search?q=&prefix=&limit=` | `q` required | `{query, items, count, errors}` | Substring match on key and metadata values; used when the search box is not empty. |
| `GET /drive/usage` | none | `{backends: [{provider, description, used, limit, remaining, fraction_used, source}], total: {used, limit}, strategy}` | Bytes. The quota bars and the upload target list come from `backends`. |
| `POST /drive/upload-url` | `{filename, size, content_type, prefix?}` | `{provider, key, method, url, headers, expires_at}` | Used for files over `DIRECT_UPLOAD_THRESHOLD` (4 MiB): the PWA then sends the file with XHR to `url` using `method` and exactly `headers` (no bearer token, typeless body so the browser adds no Content-Type of its own) and refreshes the listing. 404/405/422/501 -> "Large uploads need a storage backend with direct uploads (B2 or R2)". Large files are never sent through the hub (serverless ~4.5 MB limit). |
| `POST /drive/upload` | `multipart/form-data`: `file`, `key` (optional, defaults to `prefix/filename`), `prefix` (optional), `provider` (optional; absent = most free space, with fallback) | 201 + `StoredObject` | Progress from XHR upload events. Over 32 MiB -> 413; keys with `..` -> 400. |
| `GET /drive/presign/{provider}/{key}` | path | `{url, provider, key, expires_s, method}` | Downloads: the PWA opens `url` in a new tab (pre-opened during the tap so popup blockers allow it). A 4xx (backend without presign) falls back to `GET /drive/files/...`. |
| `GET /drive/files/{provider}/{key}` | path (each `key` segment URL-encoded, `/` kept) | raw bytes, `Content-Disposition: attachment`, `X-Storage-Backend` | Fallback only (responses over ~4.5 MB fail on the serverless hub). Fetched as a blob with the bearer header, then saved by the browser. `?inline=true` for inline. |
| `DELETE /drive/files/{provider}/{key}` | path | `{deleted, provider}` | `router.storage_delete`. |
| `GET /drive/locate/{key}` | path | `StoredObject` | not used by the PWA yet |

## DNS & Network

| method + path | request | response | notes |
|---|---|---|---|
| `GET /dns/status` | optional `?provider=` | `DNSStatus` | Refreshed on `dns.*` events. |
| `POST /dns/blocking` | `{enabled: bool, duration_s: int\|null, provider?: str}` | `DNSStatus` after the change | The PWA offers 300/1800/3600 s or null (indefinitely) when disabling. The hub publishes `dns.blocking_changed`. |
| `GET /dns/queries?limit=100&blocked_only=false` | query | `{provider, items: DNSQueryLogEntry[], count}` | |
| `GET /dns/nodes` | none | `{provider, items: VMInstance[], online, total}` from the `tailscale` plugin | Uses `state` (online/offline), `tailscale_ip` and `meta.os`. Refreshed on `compute.state_changed` with `provider == "tailscale"`. |
| `GET /dns/records`, `GET /dns/providers` | none | `{provider, items: DNSRecord[]}` / `{providers, chain}` | not used by the PWA yet |

## Database Explorer

| method + path | request | response | notes |
|---|---|---|---|
| `GET /db/providers` (optional `?usage=true`) | none | `{providers: [{name, enabled, group, display_name, description, available, capabilities: str[], sql: bool, missing_secrets: str[], usage}]}` | `capabilities` containing `"sql"` switches the editor placeholder and the starter query. Only `enabled` entries are offered. |
| `GET /db/{provider}/tables` | none | `{provider, items: TableInfo[]}` | `adapter.list_tables()` |
| `GET /db/{provider}/tables/{table}` | none | `TableInfo` with `columns` | `adapter.describe(table)`. Only called when the list entry has no columns. |
| `POST /db/{provider}/query` | `{statement: str, params: null, limit: int, write: bool}` | `QueryResult` + `mode` | `write=false` -> `adapter.query(...)`; SQL engines additionally refuse multi-statement or write-keyword statements with 400. `write=true` -> `adapter.execute(...)`. The PWA asks for confirmation before a write. |
| `GET /db/{provider}/usage` | none | `QuotaInfo` (+`remaining`, `fraction_used`) | Storage bar. |

## Plugin & Quota Switchboard

| method + path | request | response | notes |
|---|---|---|---|
| `GET /plugins` (optional `?kind=`, `?health=true`) | none (the PWA fetches all kinds and filters client-side) | `{plugins: [ProviderStatus + description, module, limits, healthy, latency_ms], chains: {kind: [names]}}` | Settings "Test connection" also calls this to prove the token works. |
| `POST /plugins/{name}/enable` / `disable` | none | the updated entry | `registry.set_enabled` (persisted to providers.yaml). `PUT /plugins/{name}/enabled {enabled}` also exists. |
| `POST /plugins/reorder` | `{kind: str, names: [names...]}` (every plugin of that kind, in the new order) | `{kind, chain: [names...]}` | The PWA optimistically sets priorities to 10, 20, ... and re-fetches on failure. |
| `POST /plugins/{name}/health` | none | `HealthStatus` | `router.health_check(name)` |
| `POST /plugins/{name}/reset` | none | the updated entry | Clears the breaker and quota cooldown. |
| `GET /plugins/{name}`, `GET /plugins/chains`, `PUT /plugins/{name}/priority`, `PATCH /plugins/{name}/settings`, `POST /plugins/reload` | | | not used by the PWA yet |

## Schedules (cron)

Lives in the Plugins tab ("Switchboard | Schedules" toggle). Times are UTC 5-field cron expressions.

| method + path | request | response | notes |
|---|---|---|---|
| `GET /cron/providers` | none | `{providers: [{name, enabled, healthy, description}]}` | Filter chips; only `enabled` providers are offered in the create form. |
| `GET /cron/jobs?provider=` | optional `provider` | `{jobs: CronJob[], errors: {provider: message}}` | `CronJob` = `{id, name, schedule, target, method, headers, body: str\|null, enabled, provider, last_run_at, next_run_at}`. Polled every 60 s. |
| `POST /cron/jobs` | `{provider, name, schedule, target, method?, headers?, body?}` | 201 + `CronJob` | A JSON body is validated client-side and sent as a string with `headers: {"Content-Type": "application/json"}`. |
| `DELETE /cron/jobs/{provider}/{id}` | none | 204 | Confirmed in-page. |
| `POST /cron/jobs/{provider}/{id}/trigger` | none | 202 | "Run now". |

## Personal apps (round 3: `hub_api/routers/{notes,expenses,links,rag,budget}.py`)

Client: `HubClient` methods in `src/lib/api.ts`, types in `src/lib/types.ts`, tests in `src/lib/__tests__/apps-api.test.ts`.

| method + path | client | used by |
|---|---|---|
| `GET /notes?notebook=&q=&limit=` | `notes()` | Notes list (search, notebook chips), Home recent notes, notebook pickers |
| `POST /notes`, `GET/PATCH/DELETE /notes/{id}` | `createNote()`, `note()`, `updateNote()`, `deleteNote()` | Note editor (autosave 800 ms after the last keystroke: create on first text, then PATCH of changed fields), swipe pin/delete |
| `POST /notes/voice` (multipart `file`, `mode`, `notebook?`, `language?`) | `voiceNote()` | Voice capture sheet; 422 (empty transcript) is shown with a retry |
| `GET /tasks?status=&due_before=`, `POST /tasks`, `PATCH/DELETE /tasks/{id}` | `tasks()`, `createTask()`, `updateTask()`, `deleteTask()` | Tasks panel, Home "Up next", tasks linked to a note |
| `POST /expenses/receipt` (multipart `file`, `note?`) | `scanReceipt()` | Camera capture then review; `confidence: low` is shown as a draft to fill in |
| `GET /expenses?month=`, `GET /expenses/{id}`, `POST /expenses`, `PATCH/DELETE /expenses/{id}` | `expenses()`, `expense()`, `createExpense()`, `updateExpense()`, `deleteExpense()` | Expenses month view, editor, Home monthly total |
| `GET/POST /links`, `DELETE /links/{code}` | `links()`, `createLink()`, `deleteLink()` | Links (QR generated client-side in `src/lib/qr.ts`); 409 shows "code taken" |
| `GET/POST /drops`, `DELETE /drops/{code}` | `drops()`, `createDrop()`, `deleteDrop()` | Drive > Drops, file actions "Share as a drop" |
| `POST /drops/folder` `{prefix, provider?, expires_in_hours?, password?, max_downloads?}` | `createFolderDrop()` | Drive folder view "Share folder" (link, QR, expiry); `GET /drops` rows carry `kind: file\|folder` and `file_count`. The public `/d/{code}` page lists the files with 10-minute presigned links and, below 25 MB, a signed "Download all as ZIP" link (`/d/{code}/zip?t=`) |
| `GET /rag/docs`, `POST /rag/index`, `DELETE /rag/docs/{provider}/{key}`, `POST /rag/ask` | `ragDocs()`, `ragIndex()`, `ragDelete()`, `ragAsk()` | Drive > Ask: one `/rag/index` call per file, in sequence, with per-file progress; a 404 on delete counts as already removed |
| `GET /budget` | `budget()` | More > Free-tier budget, Home warnings (warn/over items) |
| `GET /inbox/receipts/status` | `inboxStatus()` | Expenses "Import from email" row: setup / failing badges, mailbox count, last import; per-mailbox list in the sheet |
| `POST /inbox/receipts/sync` `{days?, max?, dry_run?, account?}` | `syncGmailReceipts()` | Expenses "Import from email": result sheet (per mailbox, added / skipped by reason / errors / left for next run); 409 = no mailbox configured. Expenses with `source: "email"` get an "email" badge whose tooltip (and the expense sheet) shows the mailbox from `source_ref` (`<mailbox>#<Message-ID>`) |
| `GET /digest?period=week\|month&refresh=1` | `digest()` | More > Summary (period switch, refresh button forces `refresh=1`), Home "This week" card |
| `GET /money/overview?month=` | `moneyOverview()` (normalised by `normalizeMoneyOverview` in `src/lib/money.ts`) | More > Money: income / spent / left in MAD, savings rate, budgets vs spend, 6-month cash flow, upcoming recurring items, accounts, incomes and recurring lists |
| `POST /money/voice` (multipart `file`, `kind=auto\|expense\|income`, `language?`) | `voiceMoney()` | Voice sheet "Money" mode (from the capture menu, Expenses and Money): created expenses (`source: "voice"`) and incomes are listed with edit and undo. `POST /notes/voice` in `auto` mode may also return `expenses` / `incomes` |
| `POST /money/incomes`, `PATCH/DELETE /money/incomes/{id}` | `createIncome()`, `updateIncome()`, `deleteIncome()` | Money income sheet (one-off or monthly with an optional end month) |
| `POST /money/recurring`, `PATCH/DELETE /money/recurring/{id}` | `createRecurring()`, `updateRecurring()`, `deleteRecurring()` | Money recurring-expense sheet (rent, subscriptions) |
| `POST /money/accounts`, `PATCH/DELETE /money/accounts/{id}` | `createAccount()`, `updateAccount()`, `deleteAccount()` | Money accounts (balances set by hand) |
| `PUT/DELETE /money/budgets/{category}` | `setBudget()`, `deleteBudget()` | Money budget sheet (monthly limit per expense category) |

### Offline mode (`src/lib/offline/`)

`OfflineHubClient` (the client `HubProvider` builds) keeps the last good body of `GET /tasks`, `/notes`, `/notes/{id}`,
`/expenses`, `/money/overview`, `/money/incomes`, `/gcal/events`, `/chats` and `/chats/{id}` in IndexedDB and serves it when
the hub cannot be reached (or does not answer within 12 s), with the "Offline — showing saved data from HH:MM" banner.
`POST/PATCH/DELETE` on `/tasks`, `/notes`, `/expenses`, `/money/incomes` and `POST /notes/voice`, `/money/voice` are queued
when offline (or on a network failure) and replayed in order. Creates send a client id in the body (`id`, 8-64 of
`A-Z a-z 0-9 _ -`); voice uploads send an `idempotency_key` form field. The hub answers a replay of the same id / key with
200 and `Idempotent-Replayed: true` instead of creating twice (an `Idempotency-Key` header works too, for non-browser
clients: CORS does not allow it from the PWA). Replay outcomes: 2xx (and 404 on DELETE) = synced; 404/409/422 and other
4xx = conflict shown in the sync sheet (retry / discard); network errors, 408/429 = retry later; a 5xx backs that row
off (30 s doubling, "Sync now" skips the wait) while other rows go on, and five 5xx make it a conflict; 401/403 = stop,
with a "Sync paused — the hub rejected the token" banner (not "Offline"). Only ids are kept for key replays: a voice
replay answers `{"replayed": true, ...}` with the created rows as `{"id"}` and an empty transcript, and a key replay of a
create deleted since answers 410. Saved data and queued writes carry the hub URL: another hub never sees them.
| `GET /money/sms/setup`, `GET /money/sms/log?limit=` | `smsSetup()`, `smsLog()` | Money > Bank SMS sheet: forwarder setup (webhook URL with the SMS token behind a reveal toggle, header name, JSON template, sender filter) and the last 20 SMS with their status |
| `POST /money/sms?dry_run=true` `{from, text}` | `testSms()` | Bank SMS "Test an SMS" box (nothing saved). The same route without `dry_run` is the webhook the phone app calls (hub token, `X-OmniFree-Token` header or `?token=`) |
| `GET /money/subscriptions/suggestions` | `subscriptionSuggestions()` (normalised by `normalizeSuggestions` in `src/lib/moneyIntel.ts`) | Money > Subscriptions: merchants charged weekly / monthly / yearly, with confidence, average amount and next date (current month only) |
| `POST /money/subscriptions/accept` `{key, name?, amount?, currency?, category?, day?}`, `POST /money/subscriptions/dismiss` `{key}` | `acceptSubscription()`, `dismissSubscription()` | "Track it" (monthly -> recurring expense, weekly / yearly -> tracked subscription) and "Not a subscription" |
| `GET /money/subscriptions`, `DELETE /money/subscriptions/{id}` | `trackedSubscriptions()`, `forgetSubscription()` | Tracked weekly / yearly subscriptions with "stop tracking" |
| `GET/POST /money/goals`, `GET/PATCH/DELETE /money/goals/{id}`, `POST /money/goals/{id}/contribute` `{amount, note?}` | `goals()`, `goal()`, `createGoal()`, `updateGoal()`, `deleteGoal()`, `contributeGoal()` | Money > Savings goals: progress bars, per-month pace, goal and add / take-out sheets with history. `GET /money/overview` also carries `forecast` (hero line + explanation sheet) |
| `GET /money/rules`, `POST /money/rules` (upsert by pattern + match), `PATCH/DELETE /money/rules/{id}`, `POST /money/rules/{id}/apply?dry_run=` | `merchantRules()`, `saveMerchantRule()`, `updateMerchantRule()`, `deleteMerchantRule()`, `applyMerchantRule()` | Money > Rules page, and the "Always do this for <merchant>?" / "Apply to N past expenses?" questions after an expense edit changes its category or name. Rules run on every new expense (all sources) |
| `GET /money/debts?status=open\|settled\|all`, `POST /money/debts`, `GET/PATCH/DELETE /money/debts/{id}`, `POST /money/debts/{id}/repay`, `DELETE /money/debts/{id}/payments/{pid}` | `debts()` (normalised by `normalizeDebts` in `src/lib/moneyTools.ts`), `createDebt()`, `updateDebt()`, `deleteDebt()`, `repayDebt()`, `deleteDebtPayment()` | Money "Owed" section and the Money > Debts page (loan sheet with partial repayments). `POST /money/voice` (and `/notes/voice` in auto mode) also returns `debts` outcomes for "j'ai prêté 200 dh à Ayoub" / "Ayoub m'a rendu 100" (`action: duplicate` when another recording stored the same one a moment ago; `payment_ids` for Undo) |
| `GET/POST /money/splits`, `GET/PATCH/DELETE /money/splits/{id}`, `POST /money/splits/{id}/expenses`, `PATCH/DELETE /money/splits/{id}/expenses/{eid}`, `POST /money/splits/{id}/settle` `{from, to, amount}` | `splitGroups()`, `splitGroup()`, `createSplitGroup()`, `updateSplitGroup()`, `deleteSplitGroup()`, `addSplitEntry()`, `updateSplitEntry()`, `deleteSplitEntry()`, `settleSplit()` | Money > Split page (groups, shared expenses split equally or by amounts, balances, settle-up). Every shared expense adds only the owner's share to the expenses (`source: "split"`), whoever paid; settlements are transfers, never expenses |
| `GET /expenses/search?q=&receipts_only=&limit=` | `searchExpenses()` | Expenses search box: merchant, note, items and the receipts' text; hits carry a presigned `receipt_url` (else the receipt is downloaded through `/drive/files`) |

Drive additions used by the redesign:

| method + path | client | notes |
|---|---|---|
| `GET /drive/locate/{key}` | `driveLocate()` | Voice-note playback: `audio_key` has no provider, so locate it, then presign. |
| `GET /drive/presign/{provider}/{key}?expires_s=` | `drivePresign(key, provider, signal, expiresS)` | Thumbnails and previews (image, PDF iframe, audio, video); "Copy a 24-hour link" uses `expires_s=86400`. Text previews download through the hub (files up to 1 MB). |
| `POST /drive/upload` / `POST /drive/upload-url` with `key` (+ `provider`) | `driveUploadFile()` | The upload sheet sets the final key (sanitised name inside the chosen folder). The hub has no rename/move/copy endpoint, so names and folders can only be chosen at upload time. |

## WebSocket `/ws`

* URL: `ws(s)://<hub>/ws?types=router.*,quota.*,breaker.*,health.*,registry.*,storage.*,instinct.*,dns.*,compute.*`
  (`types` is comma-separated `EventBus` patterns).
* Server -> client: one JSON text frame per event, `Event.model_dump(mode="json")`: `{id, type, ts, provider, kind, data}`.
* Reconnects with exponential backoff (1 s up to 30 s).
* Consumed event types:
  * `quota.*`, `router.success` -> refresh `/llm/quotas` (debounced)
  * `registry.*`, `breaker.*`, `health.*`, `quota.exhausted` -> refresh `/plugins` (debounced)
  * `storage.*` -> refresh the drive listing and usage
  * `dns.*` -> refresh `/dns/status`. `compute.state_changed` (provider `tailscale`) -> refresh `/dns/nodes`
  * `instinct.task.created | step | screenshot | completed | failed` with `data.task_id` (Instinct posts them to `POST /agent/events`, the hub republishes them):
    * step: `{step: int, status, action?: {...}, thought?, result?, error?, url?, routed_via?}`, rendered as one line of text
    * screenshot: `data.image_b64` (JPEG base64, without the `data:` prefix)
    * completed: `data.result`
    * failed: `data.error`

## Reconciliation log (integrator)

Changed in the PWA to match hub_api: `/health` -> `/healthz`; SSE `event: chunk`/`event: done` handling;
`/llm/quotas` and `/llm/models` wrapped shapes; `/drive/files` `{items}` shape and `/drive/search` for non-empty
queries; `/drive/capacity` -> `/drive/usage`; download and delete moved to `/drive/files/{provider}/{key}`;
`/dns/querylog` -> `/dns/queries`; reorder body `order` -> `names`; Instinct step payloads rendered as text;
service-worker `navigateFallbackDenylist` now covers `/healthz`, `/events`, `/compute` and `/api`.

Changed in hub_api: `/db/providers` entries now include `display_name` from `class_meta()`.

Already matching: `/dns/nodes`, `POST /plugins/{name}/enable|disable|health|reset`, `/agent/tasks*`, `/ws` (subprotocol `bearer` auth),
CORS exposing `X-Routed-Via`.

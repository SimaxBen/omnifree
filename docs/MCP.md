# OmniFree as an MCP server

The hub serves a [Model Context Protocol](https://modelcontextprotocol.io) server at **`/mcp`**,
so Claude Code, Cursor, Claude Desktop and other MCP clients can use OmniFree directly: the LLM pool,
the Drive, Ask my Drive, notes, tasks, expenses, short links, file drops, schedules and the free-tier budget.

- URL: `https://api.ilyasouhnine.tech/mcp` (local: `http://localhost:8080/mcp`)
- Transport: Streamable HTTP (MCP spec 2025-11-25), **stateless, JSON responses**. Every request is a
  single `POST`, with no `Mcp-Session-Id` and no SSE stream, so it works on the Vercel serverless hub
  (60 s per request) as well as on Cloud Run or Docker. `GET` and `DELETE` answer `405`.
- Auth: `Authorization: Bearer <token>`. The token is the hub token or the separate `MCP_API_TOKEN`.
- Implementation: official Python SDK `mcp==2.3.0` (`MCPServer`), see `hub_api/mcp_server.py`.

## 1. Pick a token

Either token works:

| Token | Where it comes from | Opens |
|---|---|---|
| Hub token | `OMNIFREE_HUB_API_TOKEN` env or vault `HUB_API_TOKEN` | the whole hub API **and** `/mcp` |
| MCP token (recommended for agents) | vault (or env) `MCP_API_TOKEN` | `/mcp` only |

Give agents the **MCP token**. You can revoke or rotate it without touching the hub token that the PWA uses.

```bash
# generate a random token and store it in the encrypted vault (value is read from the prompt/stdin)
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m core.vault set MCP_API_TOKEN

# check it
python -m core.vault get MCP_API_TOKEN
```

The hosted hub reads secrets from its environment, so after setting or rotating the token, redeploy
the hub: `deploy-hub` pushes `MCP_API_TOKEN` along with the other hub secrets. For example:

```bash
python -m automation.deploy deploy-hub --target vercel
```

To **revoke** the agent token, run `python -m core.vault delete MCP_API_TOKEN`, delete the
`MCP_API_TOKEN` env var on the host (Vercel project > Settings > Environment Variables), then redeploy.
Alternatively, set a new value and redeploy.

If neither token is configured, `/mcp` answers `503` (fail closed). A missing or wrong token answers
`401` with `WWW-Authenticate: Bearer`.

## 2. Connect a client

Replace `<token>` with the MCP token (or the hub token).

### Claude Code

```bash
claude mcp add --transport http omnifree https://api.ilyasouhnine.tech/mcp --header "Authorization: Bearer <token>"
```

Add `--scope user` to make it available in every project. Use `--scope project` to write it to the
repo's `.mcp.json`, but don't commit the token. Check the connection with `claude mcp list`, or with
`/mcp` inside a session.

### Cursor

Edit `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per project):

```json
{
  "mcpServers": {
    "omnifree": {
      "url": "https://api.ilyasouhnine.tech/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

### Claude Desktop

Claude Desktop's config file starts local (stdio) servers, so bridge to the HTTP endpoint with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote). This needs Node.js 18 or later.
Edit `claude_desktop_config.json`: on Windows it's at `%APPDATA%\Claude\claude_desktop_config.json`,
on macOS at `~/Library/Application Support/Claude/claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "omnifree": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "https://api.ilyasouhnine.tech/mcp",
        "--transport", "http-only",
        "--header", "Authorization:${OMNIFREE_AUTH}"
      ],
      "env": { "OMNIFREE_AUTH": "Bearer <token>" }
    }
  }
}
```

The `Authorization:${OMNIFREE_AUTH}` form, with no space and the value in `env`, avoids an argument
quoting problem that Claude Desktop and Cursor have on Windows. Restart Claude Desktop after editing.

### Other clients and quick checks

Any client that supports Streamable HTTP works with the URL plus the `Authorization` header. To try it
by hand:

```bash
curl -s https://api.ilyasouhnine.tech/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Inspector: `npx @modelcontextprotocol/inspector`, then pick transport "Streamable HTTP", enter the URL,
and add the `Authorization` header.

## 3. What is exposed

The tools are thin wrappers over the same Python functions that the HTTP routes use. **Nothing deletes
data.** `tasks_complete` is the only tool that changes an existing record.

| Tool | What it does |
|---|---|
| `llm_chat(prompt \| messages, system?, model?, provider?, temperature?, max_tokens?)` | Chat through the LLM pool with failover; returns `content`, `model`, `routed_via` |
| `llm_models()` | Models per provider and the failover chain |
| `drive_list(prefix?, provider?, limit?)` | List Drive files across every storage backend |
| `drive_search(query, prefix?, limit?)` | Substring search on keys and metadata |
| `drive_read_text(provider, key, max_bytes?)` | Read a text-like file (default 100 kB, max 1 MB returned, files over 5 MB refused) |
| `drive_upload_text(key, content, content_type?, provider?)` | Write a text file (backend with the most free space unless pinned) |
| `drive_share_link(provider, key, expires_s?)` | Presigned download URL (60 s to 7 days) |
| `ask_drive(question, top_k?)` | Ask my Drive: answer with `[n]` citations and sources |
| `rag_index(provider, key)` | Index or re-index one Drive file for `ask_drive` |
| `notes_search(q?, notebook?, limit?)` | Search notes (previews, plus notebook counts) |
| `notes_get(id)` | One note with its full body |
| `notes_create(body, title?, notebook?, tags?)` | New markdown note |
| `tasks_list(status?, limit?)` | `open` (default), `done` or `all` |
| `tasks_add(title, due?, priority?, details?)` | `due` takes YYYY-MM-DD or "tomorrow", "friday", "in 3 days", "demain" ... |
| `tasks_complete(id)` | Mark a task done |
| `expenses_month(month?)` | Expenses of a month plus totals per currency and category |
| `expense_add(total, merchant?, currency?, category?, date?, note?, items?)` | Record an expense (MAD and today by default) |
| `links_create(url, code?, title?, expires_in_days?)` | Short link `/s/<code>` |
| `drops_create(provider, key, expires_in_hours?, password?, max_downloads?)` | Public file drop page `/d/<code>` |
| `schedules_list(provider?)` | Scheduled HTTP jobs (secret headers are masked) |
| `schedule_create(name, schedule, target, method?, headers?, body?, provider?)` | New cron job (5 fields, UTC) |
| `budget_status()` | Free-tier usage vs limits with ok / warn / over status |

Resources:

- `omnifree://budget`: free-tier budget (JSON)
- `omnifree://tasks/open`: open tasks (JSON)
- `omnifree://notes/{id}`: one note as markdown

Prompts:

- `weekly_review(focus?)`: tasks done and due, this month's expenses, budget alerts, and a plan for next week
- `inbox_triage(notebook?)`: go through a notebook, extract actions, propose filing, then create tasks once you confirm

Errors from a tool come back as an MCP tool result with `isError: true` and a readable reason, for
example `note not found (HTTP 404)` or `quota_exhausted: ... [provider groq]`.

## 4. Security notes

- Bearer token only. The hub's `?access_token=` query fallback is **not** accepted on `/mcp`.
- A request with an `Origin` header must come from one of the hub CORS origins (`OMNIFREE_CORS_ORIGINS`),
  otherwise it gets `403`. This blocks DNS-rebinding and cross-site browser calls.
- Optional Host allow-list: set `OMNIFREE_MCP_ALLOWED_HOSTS=api.ilyasouhnine.tech,localhost:*` to turn on
  the SDK's Host header validation.
- Request bodies are capped at 4 MiB by the SDK. `drive_upload_text` is also capped by the hub's
  `max_upload_bytes`.

/**
 * OmniFree edge router (Cloudflare Worker).
 *
 * - `${API_PREFIX}/*` is proxied to HUB_ORIGIN with the prefix stripped. Method, body and headers
 *   (Authorization, Upgrade for WebSockets) pass through; CORS is added and `X-Routed-Via` is exposed
 *   so the PWA can display which LLM provider answered.
 * - `/edge/health` reports the worker and the hub `/healthz`.
 * - Everything else is served from the PWA origins, tried in order: a network error or 5xx falls
 *   through to the next mirror; a 404 for an HTML navigation is answered with `/index.html` (SPA).
 */

export interface Env {
  HUB_ORIGIN: string;
  PWA_ORIGINS: string;
  API_PREFIX: string;
  ALLOWED_ORIGINS: string;
}

const EXPOSED_HEADERS = "X-Routed-Via, X-Request-Id, Retry-After, Content-Disposition";
const HOP_BY_HOP = ["connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer"];

export function splitOrigins(value: string | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((s) => s.trim().replace(/\/+$/, ""))
    .filter((s) => s.length > 0);
}

export function corsHeaders(request: Request, env: Env): Headers {
  const headers = new Headers();
  const origin = request.headers.get("Origin");
  const allowed = splitOrigins(env.ALLOWED_ORIGINS || "*");
  if (origin && (allowed.includes("*") || allowed.includes(origin))) {
    headers.set("Access-Control-Allow-Origin", allowed.includes("*") ? "*" : origin);
    headers.set("Vary", "Origin");
  }
  headers.set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS");
  headers.set(
    "Access-Control-Allow-Headers",
    request.headers.get("Access-Control-Request-Headers") ?? "Authorization, Content-Type",
  );
  headers.set("Access-Control-Expose-Headers", EXPOSED_HEADERS);
  headers.set("Access-Control-Max-Age", "86400");
  return headers;
}

function json(data: unknown, status = 200, extra?: Headers): Response {
  const headers = new Headers(extra);
  headers.set("Content-Type", "application/json");
  headers.set("Cache-Control", "no-store");
  return new Response(JSON.stringify(data), { status, headers });
}

export function hubUrl(request: Request, env: Env): URL | null {
  const hub = splitOrigins(env.HUB_ORIGIN)[0];
  if (!hub) return null;
  const incoming = new URL(request.url);
  const prefix = (env.API_PREFIX || "/api").replace(/\/+$/, "");
  const rest = incoming.pathname.slice(prefix.length) || "/";
  const target = new URL(hub);
  target.pathname = target.pathname.replace(/\/+$/, "") + (rest.startsWith("/") ? rest : `/${rest}`);
  target.search = incoming.search;
  return target;
}

async function proxyToHub(request: Request, env: Env): Promise<Response> {
  const target = hubUrl(request, env);
  if (!target) return json({ error: "HUB_ORIGIN is not configured" }, 503, corsHeaders(request, env));

  const headers = new Headers(request.headers);
  for (const h of HOP_BY_HOP) headers.delete(h);
  headers.set("X-Forwarded-Host", new URL(request.url).host);
  headers.set("X-Forwarded-Proto", "https");
  headers.set("X-OmniFree-Edge", "1");

  const isWebSocket = (request.headers.get("Upgrade") ?? "").toLowerCase() === "websocket";
  const init: RequestInit = { method: request.method, headers, redirect: "manual" };
  if (!["GET", "HEAD"].includes(request.method)) init.body = request.body;

  let upstream: Response;
  try {
    upstream = await fetch(target.toString(), init);
  } catch (err) {
    return json({ error: "hub unreachable", detail: String(err) }, 502, corsHeaders(request, env));
  }
  if (isWebSocket || upstream.status === 101) return upstream;

  const out = new Headers(upstream.headers);
  corsHeaders(request, env).forEach((value, key) => out.set(key, value));
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
}

function isNavigation(request: Request): boolean {
  return request.method === "GET" && (request.headers.get("Accept") ?? "").includes("text/html");
}

async function servePwa(request: Request, env: Env): Promise<Response> {
  const origins = splitOrigins(env.PWA_ORIGINS);
  if (origins.length === 0) return json({ error: "PWA_ORIGINS is not configured" }, 503);
  const incoming = new URL(request.url);
  let last: Response | null = null;

  for (const origin of origins) {
    const base = new URL(origin);
    const candidates = [incoming.pathname];
    if (isNavigation(request) && incoming.pathname !== "/index.html") candidates.push("/index.html");
    for (const path of candidates) {
      const target = new URL(path + incoming.search, base);
      try {
        const resp = await fetch(target.toString(), {
          method: request.method === "HEAD" ? "HEAD" : "GET",
          headers: { Accept: request.headers.get("Accept") ?? "*/*" },
          cf: { cacheTtlByStatus: { "200-299": 300, "404": 10, "500-599": 0 } },
        });
        if (resp.status >= 500) {
          last = resp;
          break; // origin unhealthy: next mirror
        }
        if (resp.status === 404 && path !== candidates[candidates.length - 1]) continue; // SPA fallback
        const out = new Headers(resp.headers);
        out.set("X-Served-By", base.host);
        return new Response(resp.body, { status: resp.status, headers: out });
      } catch {
        break; // network error: next mirror
      }
    }
  }
  return last ?? json({ error: "all PWA origins failed" }, 502);
}

async function health(request: Request, env: Env): Promise<Response> {
  const hub = splitOrigins(env.HUB_ORIGIN)[0];
  let hubOk = false;
  let hubStatus = 0;
  if (hub) {
    try {
      const resp = await fetch(`${hub}/healthz`, { headers: { Accept: "application/json" } });
      hubStatus = resp.status;
      hubOk = resp.ok;
    } catch {
      hubOk = false;
    }
  }
  return json(
    {
      ok: true,
      hub: { configured: Boolean(hub), ok: hubOk, status: hubStatus },
      pwa_origins: splitOrigins(env.PWA_ORIGINS).length,
    },
    200,
    corsHeaders(request, env),
  );
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const prefix = (env.API_PREFIX || "/api").replace(/\/+$/, "");
    if (url.pathname === "/edge/health") return health(request, env);
    if (url.pathname === prefix || url.pathname.startsWith(`${prefix}/`)) {
      if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(request, env) });
      return proxyToHub(request, env);
    }
    return servePwa(request, env);
  },
} satisfies ExportedHandler<Env>;

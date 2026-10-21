import { HubError } from './api';

/** Why the Instinct browser agent cannot be used, from the hub's /agent answers. */
export interface AgentUnavailable {
  reason: 'not_configured' | 'unreachable';
  detail: string;
}

/**
 * The hub proxies /agent to the Instinct service: 503 "instinct_url not configured" when no server is
 * set, 502 "instinct unreachable" / 504 "instinct timeout" when it is set but down. Anything else
 * (auth, a real Instinct error) is a plain error and returns null.
 */
export function agentUnavailable(err: unknown): AgentUnavailable | null {
  if (!(err instanceof HubError) || !/instinct/i.test(err.message)) return null;
  if (err.status === 503) return { reason: 'not_configured', detail: err.message };
  if (err.status === 502 || err.status === 504) return { reason: 'unreachable', detail: err.message };
  return null;
}

// Opening one split group from another page (the Debts page's "from Split" link): the group to show is handed over
// here, then Money › Split opens on it. Kept in memory only: a reload lands on the group list, which is fine.

let pending: string | null = null;

/** The next Money › Split screen opens on this group. */
export function openSplitGroupNext(groupId: string): void {
  pending = groupId;
}

/** The group handed over by {@link openSplitGroupNext}, else null (read it in a state initialiser: pure, StrictMode
 * calls it twice; then {@link clearSplitGroup} in an effect). */
export function peekSplitGroup(): string | null {
  return pending;
}

export function clearSplitGroup(): void {
  pending = null;
}

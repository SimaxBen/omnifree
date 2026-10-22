// Types of the money tools: merchant rules, debts, split groups, receipt search and the accounting export.

import type { Expense, ExpenseCategory } from './types';

// ------------------------------------------------------------------ merchant rules (/money/rules)

export type RuleMatch = 'contains' | 'equals';

export interface MerchantRule {
  id: string;
  /** Normalised merchant text (lower case, no accents or punctuation). */
  pattern: string;
  match: RuleMatch;
  category: ExpenseCategory | null;
  /** Display name for matching merchants. */
  rename: string | null;
  /** Past expenses the rule matches and would still change. */
  pending: number;
  created_at: string;
  updated_at: string;
}

export interface RuleCreate {
  pattern: string;
  match?: RuleMatch;
  category?: ExpenseCategory | null;
  rename?: string | null;
}

export type RulePatch = Partial<RuleCreate>;

export interface RuleApplyResult {
  matched: number;
  updated: number;
  dry_run: boolean;
}

// ------------------------------------------------------------------ debts (/money/debts)

export type DebtDirection = 'lent' | 'borrowed';

export interface DebtPayment {
  id: string;
  amount: number;
  date: string;
  note: string | null;
  origin: string | null;
  created_at: string;
}

export interface Debt {
  id: string;
  person: string;
  direction: DebtDirection;
  amount: number;
  currency: string;
  date: string;
  due_date: string | null;
  note: string | null;
  settled: boolean;
  settled_at: string | null;
  origin: string | null;
  paid: number;
  remaining: number;
  overdue: boolean;
  age_days: number;
  payments: DebtPayment[];
  created_at: string;
  updated_at: string;
}

export interface DebtCreate {
  person: string;
  direction: DebtDirection;
  amount: number;
  currency?: string;
  date?: string;
  due_date?: string | null;
  note?: string | null;
}

export type DebtPatch = Partial<DebtCreate> & { settled?: boolean };

export interface DebtSummary {
  currency: string;
  owed_to_me: number;
  i_owe: number;
  net: number;
  by_person: { person: string; owes_me: number; i_owe: number; net: number; count: number }[];
  unconverted: string[];
}

export interface DebtListing {
  debts: Debt[];
  summary: DebtSummary;
}

/** One loan or repayment heard by `POST /money/voice`. */
export interface VoiceDebtOutcome {
  type: 'lend' | 'borrow' | 'repaid' | 'repay';
  /** `duplicate`: the same loan or repayment was stored from another recording a moment ago; nothing new stored. */
  action: 'created' | 'repaid' | 'unmatched' | 'duplicate';
  person: string;
  amount: number;
  currency: string;
  debts: Debt[];
  /** The repayments this entry stored, one per loan in `debts` (what Undo removes). */
  payment_ids?: string[];
  applied: number;
  unapplied: number;
}

// ------------------------------------------------------------------ split groups (/money/splits)

export interface SplitMember {
  id: string;
  name: string;
  me: boolean;
}

export interface SplitEntry {
  id: string;
  group_id: string;
  kind: 'expense' | 'settlement';
  date: string;
  description: string;
  amount: number;
  paid_by: string;
  split: 'equal' | 'custom';
  shares: Record<string, number>;
  category: ExpenseCategory;
  note: string | null;
  /** The owner's-share expense in app_expenses (whoever paid; null when the owner has no share). */
  expense_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface SplitTotals {
  spent: number;
  my_share: number;
  my_net: number;
  count: number;
}

export interface SplitGroupSummary {
  id: string;
  name: string;
  currency: string;
  members: SplitMember[];
  archived: boolean;
  me: string;
  totals: SplitTotals;
  settled: boolean;
  created_at: string;
  updated_at: string;
}

export interface SplitBalance {
  member: string;
  name: string;
  paid: number;
  share: number;
  net: number;
}

export interface SettleSuggestion {
  from: string;
  to: string;
  from_name: string;
  to_name: string;
  amount: number;
}

export interface SplitGroup extends Omit<SplitGroupSummary, 'settled'> {
  entries: SplitEntry[];
  balances: SplitBalance[];
  settle: SettleSuggestion[];
}

export interface SplitGroupCreate {
  name: string;
  members: string[];
  currency?: string;
  me_name?: string;
}

export interface SplitGroupPatch {
  name?: string;
  currency?: string;
  members?: { id?: string; name: string }[];
  archived?: boolean;
}

export interface SplitEntryCreate {
  description: string;
  amount: number;
  paid_by: string;
  date?: string;
  split: 'equal' | 'custom';
  participants?: string[];
  shares?: Record<string, number>;
  category?: ExpenseCategory;
  note?: string | null;
}

export type SplitEntryPatch = Partial<SplitEntryCreate>;

// ------------------------------------------------------------------ receipt search and export

export interface ExpenseSearchHit extends Expense {
  /** Presigned URL (1 h) of the receipt image or PDF, null when it cannot be presigned. */
  receipt_url: string | null;
  receipt_provider: string | null;
  receipt_kind: 'image' | 'pdf' | null;
  snippet: string | null;
}

export interface ExpenseSearchResult {
  q: string;
  terms: string[];
  results: ExpenseSearchHit[];
  count: number;
}

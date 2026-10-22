import type { ReactNode } from 'react';
import type { MorePage } from '../MoreView';
import DebtsView from './DebtsView';
import RulesView from './RulesView';
import SplitView from './SplitView';

/** Sub-pages of Money (More › Money › ...): reached from the Money page, not listed in the More menu. */
export const MONEY_TOOL_PAGES: MorePage[] = [
  { id: 'money-split', title: 'Split', short: 'Split', subtitle: 'Shared expenses with friends: who owes whom', icon: 'cash', group: 'money' },
  { id: 'money-debts', title: 'Debts', short: 'Debts', subtitle: 'Money lent and borrowed, with repayments', icon: 'wallet', group: 'money' },
  { id: 'money-rules', title: 'Rules', short: 'Rules', subtitle: 'Always file a merchant under the same category', icon: 'tag', group: 'money' },
];

export default function MoneyToolsView({ id }: { id: string }): ReactNode {
  if (id === 'money-split') return <SplitView />;
  if (id === 'money-debts') return <DebtsView />;
  if (id === 'money-rules') return <RulesView />;
  return null;
}

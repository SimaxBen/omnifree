import { useCallback } from 'react';
import { useConfirm } from '../../components/Sheet';
import { announce } from '../../lib/bus';
import { errorText, useHub } from '../../lib/hub';
import { ruleActionLabel, ruleSuggestion } from '../../lib/moneyTools';
import type { ExpenseCategory } from '../../lib/types';

type Shape = { merchant: string; category: ExpenseCategory };

/**
 * After an expense edit that changed its category or name: "Always do this for <merchant>?" -> a merchant rule,
 * then "Apply to N past expenses?" -> the bulk update. Both are questions; saying no changes nothing.
 */
export function useRuleOffer(): (before: Shape, after: Shape) => Promise<void> {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  return useCallback(
    async (before: Shape, after: Shape) => {
      const s = ruleSuggestion(before, after);
      if (!s) return;
      const yes = await confirm({
        title: `Always do this for “${s.pattern}”?`,
        body: `Every new expense from ${s.pattern} (receipts, email, bank SMS, voice) will ${ruleActionLabel(s)}. You can change it in Money › Rules.`,
        confirmLabel: 'Always do this',
        tone: 'primary',
      });
      if (!yes) return;
      try {
        const rule = await client.saveMerchantRule({ pattern: s.pattern, match: 'contains', category: s.category, rename: s.rename });
        if (rule.pending > 0) {
          const past = await confirm({
            title: `Apply to ${rule.pending} past expense${rule.pending === 1 ? '' : 's'}?`,
            body: `They match “${rule.pattern}” and would ${ruleActionLabel(rule)}.`,
            confirmLabel: 'Apply to past expenses',
            tone: 'primary',
          });
          if (past) {
            const res = await client.applyMerchantRule(rule.id);
            announce('expenses', 'money');
            notify(`Rule saved, ${res.updated} past expense${res.updated === 1 ? '' : 's'} updated`, 'ok');
            return;
          }
        }
        notify('Rule saved', 'ok');
      } catch (err) {
        notify(errorText(err), 'error');
      }
    },
    [client, confirm, notify],
  );
}

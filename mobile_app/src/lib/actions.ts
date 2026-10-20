import { createContext, useContext } from 'react';
import type { Expense, Note, StoredObject } from './types';

/** Things any screen can start; the sheets live once, above the screens (src/AppSheets.tsx). */
export interface AppActions {
  openCapture: () => void;
  newNote: (opts?: { notebook?: string; body?: string }) => void;
  openNote: (note: Note) => void;
  recordVoice: () => void;
  scanReceipt: () => void;
  addExpense: () => void;
  editExpense: (e: Expense) => void;
  pickUpload: (folder?: string) => void;
  preview: (obj: StoredObject, siblings?: StoredObject[]) => void;
  fileActions: (obj: StoredObject) => void;
  shareDrop: (obj: StoredObject) => void;
}

export const ActionsContext = createContext<AppActions | null>(null);

export function useActions(): AppActions {
  const ctx = useContext(ActionsContext);
  if (!ctx) throw new Error('useActions must be used inside <AppSheets>');
  return ctx;
}

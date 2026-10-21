import { useEffect, useRef } from 'react';

// In-app change notifications: a screen that creates or changes data announces the topic, and
// every mounted list showing that topic reloads (Home, Notes, Drive...).

export type Topic = 'notes' | 'tasks' | 'drive' | 'expenses' | 'links' | 'drops' | 'rag' | 'budget' | 'money';

type Listener = (topic: Topic) => void;
const listeners = new Set<Listener>();

export function announce(...topics: Topic[]): void {
  for (const t of topics) for (const l of [...listeners]) l(t);
}

export function onTopic(fn: Listener): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Calls `handler` whenever one of `topics` is announced, for the component's lifetime. */
export function useTopic(topics: Topic[], handler: () => void): void {
  const ref = useRef(handler);
  ref.current = handler;
  const key = topics.join('|');
  useEffect(() => {
    const wanted = key.split('|');
    return onTopic((t) => {
      if (wanted.includes(t)) ref.current();
    });
  }, [key]);
}

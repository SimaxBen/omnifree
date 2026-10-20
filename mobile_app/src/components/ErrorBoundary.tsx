import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Catches render/commit errors so one broken view never blanks the whole app. */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error('View crashed', error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div role="alert" className="m-4 space-y-3 rounded-xl border border-danger-400/30 bg-danger-400/5 p-4 text-sm text-danger-400">
        <p className="font-medium">Something went wrong.</p>
        <p className="break-words font-mono text-xs text-ink-300">{error.message || String(error)}</p>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="rounded-lg border border-ink-700 bg-ink-800 px-3 py-1.5 text-ink-100 hover:bg-ink-700"
          >
            Try again
          </button>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-lg bg-signal px-3 py-1.5 font-medium text-ink-950 hover:bg-signal/90"
          >
            Reload
          </button>
        </div>
      </div>
    );
  }
}

import { AlertTriangle, RefreshCw } from "lucide-react";
import { Component, type ReactNode } from "react";
import { Spinner } from "@protolabsai/ui/data";

// A small render-prop error boundary (ADR 0013). Pairs with TanStack Query's
// <QueryErrorResetBoundary> so a failed `useSuspenseQuery` surfaces a contained
// retry card instead of crashing the app or routing through a global banner.
// `resetKeys` clears the error when any key changes (e.g. switching tabs).

type FallbackArgs = { error: Error; reset: () => void };

type Props = {
  children: ReactNode;
  fallback: (args: FallbackArgs) => ReactNode;
  onReset?: () => void;
  resetKeys?: unknown[];
};

type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(prev: Props) {
    // Auto-clear when a reset key changes so a stale error doesn't stick.
    if (this.state.error && !shallowEqual(prev.resetKeys, this.props.resetKeys)) {
      this.setState({ error: null });
    }
  }

  reset = () => {
    this.props.onReset?.();
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return this.props.fallback({ error: this.state.error, reset: this.reset });
    }
    return this.props.children;
  }
}

function shallowEqual(a?: unknown[], b?: unknown[]) {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  return a.every((v, i) => Object.is(v, b[i]));
}

/** Default in-panel error card with a retry button. */
export function PanelError({ error, reset, label = "panel" }: FallbackArgs & { label?: string }) {
  return (
    <div className="panel-error" role="alert">
      <AlertTriangle size={18} />
      <div>
        <strong>Couldn't load the {label}</strong>
        <span>{error.message}</span>
      </div>
      <button className="secondary-button" type="button" onClick={reset}>
        <RefreshCw size={14} /> Retry
      </button>
    </div>
  );
}

/** Default <Suspense> fallback for a panel that's loading. */
export function PanelSkeleton({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="panel-skeleton" aria-busy="true">
      <Spinner size={18} />
      <span>{label}</span>
    </div>
  );
}

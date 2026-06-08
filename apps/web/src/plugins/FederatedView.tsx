// Mount a `ui: react` plugin view as a Module Federation remote (ADR 0034).
//
// The plugin ships a pre-built federation *remote* (a remoteEntry.js exposing a component).
// We register it at runtime (URL from the plugin manifest), load the exposed module, and mount
// it into the host React tree — sharing the host's React + react-query singletons (configured
// in vite.config.ts), so the remote behaves like a component written inside the console.
//
// D6 (fail safe): a remote that fails to load/throw renders a bounded error card with retry —
// never a blank console or a crashed host.

import {
  Component,
  type ComponentType,
  type ErrorInfo,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import { AlertTriangle, Loader2, RotateCcw } from "lucide-react";
import {
  __federation_method_getRemote,
  __federation_method_setRemote,
  __federation_method_unwrapDefault,
} from "virtual:__federation__";

export type RemoteSpec = { url: string; module: string; scope?: string };

// Cache resolved remote components by scope+module so re-opening a view doesn't refetch.
const _cache = new Map<string, ComponentType<Record<string, unknown>>>();

async function loadRemoteComponent(spec: RemoteSpec): Promise<ComponentType<Record<string, unknown>>> {
  const scope = spec.scope || spec.module;
  const key = `${scope}::${spec.module}::${spec.url}`;
  const cached = _cache.get(key);
  if (cached) return cached;
  // vite-plugin-federation calls `url().then(...)`, so the resolver must return a Promise.
  __federation_method_setRemote(scope, { url: () => Promise.resolve(spec.url), format: "esm", from: "vite" });
  const mod = await __federation_method_getRemote(scope, spec.module);
  const resolved = (await __federation_method_unwrapDefault(mod)) as ComponentType<Record<string, unknown>>;
  if (typeof resolved !== "function") {
    throw new Error(`remote ${scope} ${spec.module} did not export a component`);
  }
  _cache.set(key, resolved);
  return resolved;
}

function ErrorCard({ label, detail, onRetry }: { label: string; detail?: string; onRetry: () => void }) {
  return (
    <div className="federated-error" role="alert">
      <AlertTriangle size={15} />
      <div>
        <strong>Couldn't load "{label}"</strong>
        {detail ? <p className="federated-error-detail">{detail}</p> : null}
        <button type="button" className="secondary-button" onClick={onRetry}>
          <RotateCcw size={14} /> Retry
        </button>
      </div>
    </div>
  );
}

// Class boundary catches render-time throws from the remote (the hook/runtime errors a
// promise can't). Resets via `resetKey` so Retry remounts the subtree.
class RemoteBoundary extends Component<
  { label: string; resetKey: number; onReset: () => void; children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[federated-view] remote render error", error, info);
  }
  componentDidUpdate(prev: { resetKey: number }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }
  render() {
    if (this.state.error) {
      return <ErrorCard label={this.props.label} detail={String(this.state.error.message || this.state.error)} onRetry={this.props.onReset} />;
    }
    return this.props.children;
  }
}

export function FederatedView({
  label,
  remote,
  props,
}: {
  label: string;
  remote: RemoteSpec;
  props?: Record<string, unknown>;
}) {
  const [Comp, setComp] = useState<ComponentType<Record<string, unknown>> | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [attempt, setAttempt] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    setComp(null);
    setError(null);
    loadRemoteComponent(remote)
      .then((c) => alive.current && setComp(() => c))
      .catch((e) => alive.current && setError(e instanceof Error ? e : new Error(String(e))));
    return () => {
      alive.current = false;
    };
  }, [remote.url, remote.module, remote.scope, attempt]);

  const retry = () => {
    _cache.delete(`${remote.scope || remote.module}::${remote.module}::${remote.url}`);
    setAttempt((n) => n + 1);
  };

  if (error) return <ErrorCard label={label} detail={String(error.message || error)} onRetry={retry} />;
  if (!Comp) {
    return (
      <div className="federated-loading">
        <Loader2 className="spin" size={16} /> <span>Loading {label}…</span>
      </div>
    );
  }
  return (
    <RemoteBoundary label={label} resetKey={attempt} onReset={retry}>
      <Comp {...(props ?? {})} />
    </RemoteBoundary>
  );
}

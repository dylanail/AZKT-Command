/* "Your role can't see this" as a calm, specific panel instead of a red failure.
   Anything that is not a 403 falls through to the normal error state. */
import { ApiError } from "../../../lib/api";
import { Button, EmptyState, ErrorState, GlassPanel } from "../../../ui";

export function isDenied(error: unknown): boolean {
  return error instanceof ApiError && error.isDenied;
}

export interface DeniedOrErrorProps {
  error: unknown;
  onRetry?: () => void;
  /** What the person cannot see, e.g. "import requests". */
  what: string;
  /** Where to go instead. */
  backTo?: string;
  backLabel?: string;
}

/** Renders a denied panel for a 403 and the normal error state for everything else. */
export function DeniedOrError({ error, onRetry, what, backTo, backLabel }: DeniedOrErrorProps) {
  if (!isDenied(error)) return <ErrorState error={error} onRetry={onRetry} />;
  return (
    <EmptyState
      title={`Your role can't see ${what}`}
      body={(error as ApiError).message || "Ask the owner if you need access to this part of the business."}
      action={backTo ? <Button size="sm" variant="soft" to={backTo}>{backLabel || "Go back"}</Button> : undefined}
    />
  );
}

/** The same, wrapped in a panel for use straight inside a page. */
export function DeniedOrErrorPanel(props: DeniedOrErrorProps) {
  return <GlassPanel clip><DeniedOrError {...props} /></GlassPanel>;
}

export default DeniedOrError;

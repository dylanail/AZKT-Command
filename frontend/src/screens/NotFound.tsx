import { Button, EmptyState, GlassPanel, PageHeader } from "../ui";

export default function NotFound() {
  return (
    <div className="page page-narrow">
      <PageHeader title="Not here" subtitle="That address doesn't match anything in AZKT." />
      <GlassPanel clip>
        <EmptyState title="Nothing at this address" action={<Button to="/" variant="glass">Go home</Button>} />
      </GlassPanel>
    </div>
  );
}

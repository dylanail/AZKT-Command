/* "What the Manager can do for you" — GET /api/agent/coverage, owner only.
   It is built from the live command registry, so it is the real list: every owner-editable command, the
   Manager tool that reaches it, and whether this person could run it right now. Gaps are named, not hidden. */
import { useMemo, useState } from "react";
import { ApiError } from "../../../lib/api";
import { humanize } from "../../../lib/links";
import { useQuery } from "../../../lib/useQuery";
import { Chip, EmptyState, ErrorState, Expander, GlassPanel, Loading, Notice, SegmentedControl, Table, Tr } from "../../../ui";
import { getCoverage } from "../api";
import type { CoverageResp } from "../types";

type View = "mine" | "all" | "gaps";

export function Coverage() {
  const q = useQuery<CoverageResp>((signal) => getCoverage(signal), []);
  const [view, setView] = useState<View>("mine");

  const rows = useMemo(() => {
    const all = q.data?.commands || [];
    if (view === "all") return all;
    if (view === "gaps") return all.filter((c) => !c.covered);
    return all.filter((c) => c.available);
  }, [q.data, view]);

  if (q.loading) return <GlassPanel clip><Loading label="Loading the capability map" rows={3} /></GlassPanel>;
  if (q.error) {
    const denied = q.error instanceof ApiError && q.error.status === 403;
    if (denied) {
      return (
        <Notice tone="neutral" lead="Owner only">
          The full capability map is the whole command surface of the app, so only the owner sees it. You can still ask the
          Manager anything — it only offers you what your role allows.
        </Notice>
      );
    }
    return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} title="Couldn't load the capability map" /></GlassPanel>;
  }

  const d = q.data;
  if (!d) return <GlassPanel clip><EmptyState title="No capability map returned" /></GlassPanel>;

  const counts = d.counts || { commands: 0, write_tools: 0, read_tools: 0, ui_commands: 0, excluded: 0 };
  const mine = counts.available_commands ?? d.commands.filter((c) => c.available).length;

  return (
    <div className="stack">
      <div className="fs14 t2" style={{ maxWidth: 680 }}>
        The Manager reaches your records through the same commands the screens use. {mine} of {counts.commands} of them are
        available to you right now; {counts.read_tools} read-only lookups back its answers.
      </div>

      {d.gaps.length ? (
        <Notice tone="risk" lead={`${d.gaps.length} not reachable yet`}>
          These are things you can do on a screen that the Manager cannot do for you: {d.gaps.map(humanize).join(", ")}.
        </Notice>
      ) : (
        <Notice tone="ok" lead="Nothing missing">
          Every command the screens offer has a Manager route.
        </Notice>
      )}

      <div className="row-wrap">
        <SegmentedControl<View>
          label="Which commands to show"
          size="sm"
          value={view}
          onChange={setView}
          options={[
            { value: "mine", label: "Available to you", count: mine },
            { value: "all", label: "Everything", count: counts.commands },
            { value: "gaps", label: "Not reachable", count: d.commands.filter((c) => !c.covered).length },
          ]}
        />
      </div>

      <GlassPanel clip>
        {rows.length === 0 ? (
          <EmptyState title="Nothing in this view" body={view === "gaps" ? "Every command has a Manager route." : "Your role has no commands in this view."} />
        ) : (
          <Table minWidth={660} caption={`${rows.length} commands`}>
            <thead>
              <tr>
                <th scope="col">What it does</th>
                <th scope="col">Where you do it</th>
                <th scope="col">Manager route</th>
                <th scope="col">You</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <Tr key={c.command}>
                  <td>
                    <div className="ag-cov__name">{humanize(c.command)}</div>
                    {c.description ? <div className="fs12 t3">{c.description}</div> : null}
                  </td>
                  <td className="fs13">{c.ui_action.length ? c.ui_action.map(humanize).join(", ") : <span className="t4">Not on a screen</span>}</td>
                  <td className="fs13">
                    {c.tool ? <code className="fs12">{c.tool}</code> : <span className="t4">{c.excluded_reason || "No Manager route"}</span>}
                  </td>
                  <td>
                    {c.available
                      ? <Chip size="sm" tone="ok">Can run</Chip>
                      : <Chip size="sm" tone="soft" title={c.perm ? `Needs ${c.perm}` : undefined}>{c.perm ? `Needs ${humanize(c.perm)}` : "Not for you"}</Chip>}
                  </td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </GlassPanel>

      <Expander title="Sources and technical details">
        <div className="stack-sm fs13">
          <div>Policy version <code>{d.policy_version}</code>. {counts.write_tools} write tools, {counts.read_tools} read tools, {counts.excluded} commands deliberately kept away from the Manager.</div>
          {d.excluded.length ? (
            <ul className="ag-card__list">
              {d.excluded.map((e) => <li key={e.command}><b style={{ fontWeight: 600 }}>{humanize(e.command)}</b> — {e.reason}</li>)}
            </ul>
          ) : null}
        </div>
      </Expander>
    </div>
  );
}

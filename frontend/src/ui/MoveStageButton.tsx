import { Button } from "./Button";
import { Menu } from "./Menu";
import { ChevronDown } from "./Icons";

export interface Stage { id: string; label: string; }
export interface MoveStageButtonProps {
  stages: Stage[];
  current: string;
  /** Called with the target stage; the caller runs the gate check and reports the outcome. */
  onMove: (stageId: string) => void;
  /** Whole control disabled with a reason ("Only Dylan verifies finished work."). */
  disabledReason?: string;
  /** Restrict to adjacent moves (forward one, back one). Default: any other stage. */
  adjacentOnly?: boolean;
  size?: "sm" | "md" | "lg";
  label?: string;
  busy?: boolean;
}

/**
 * Keyboard-equivalent of dragging a card between fixed board columns.
 * A button opens a menu of target stages; arrow keys choose, Enter moves, Esc cancels.
 */
export function MoveStageButton({ stages, current, onMove, disabledReason, adjacentOnly = false, size = "md", label = "Move stage", busy }: MoveStageButtonProps) {
  const idx = stages.findIndex((s) => s.id === current);
  const items = stages
    .map((s, i) => ({ s, i }))
    .filter(({ s, i }) => s.id !== current && (!adjacentOnly || Math.abs(i - idx) === 1))
    .map(({ s, i }) => ({
      label: s.label,
      meta: i > idx ? "Forward" : "Back",
      onSelect: () => onMove(s.id),
    }));
  const disabled = !!disabledReason || items.length === 0;
  const reason = disabledReason || (items.length === 0 ? "No other stage to move to." : undefined);
  return (
    <Menu
      label={label}
      heading={idx >= 0 ? <span>Now: <b style={{ fontWeight: 500 }}>{stages[idx].label}</b></span> : undefined}
      items={items}
      trigger={<Button size={size} variant="glass" disabled={disabled} disabledReason={reason} loading={busy} iconRight={<ChevronDown />}>{label}</Button>}
    />
  );
}

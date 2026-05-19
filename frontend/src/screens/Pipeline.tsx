import { DndContext, DragEndEvent, PointerSensor, TouchSensor, useDraggable, useDroppable, useSensor, useSensors } from "@dnd-kit/core";
import { api } from "../api";
import { useAsync } from "../ui";

const LABELS: Record<string, string> = {
  sourced: "Sourced", bid: "Bid", won: "Won", in_transit_japan: "In Transit JP",
  customs: "Customs", arrived: "Arrived", reconditioning: "Recon",
  ready_for_sale: "Ready", sold: "Sold",
};

function Card({ v, next, onFlip }: { v: any; next?: string; onFlip: () => void }) {
  const { attributes, listeners, setNodeRef, transform } = useDraggable({ id: v.id });
  return (
    <div ref={setNodeRef} className="kcard"
      style={transform ? { transform: `translate(${transform.x}px,${transform.y}px)`, zIndex: 30 } : undefined}>
      <div {...listeners} {...attributes} style={{ touchAction: "none" }}>
        <strong>{v.title || "Untitled"}</strong>
        {v.sold_price_usd != null && (
          <div className="muted" style={{ fontSize: 12 }}>${v.sold_price_usd}</div>
        )}
      </div>
      {next && (
        <button style={{ width: "100%", marginTop: 8, padding: "8px" }} onClick={onFlip}>
          → {LABELS[next]}
        </button>
      )}
    </div>
  );
}

function Col({ stage, items, onFlip }: { stage: string; items: any[]; onFlip: (v: any, to: string) => void }) {
  const { setNodeRef, isOver } = useDroppable({ id: stage });
  const order = Object.keys(LABELS);
  const next = order[order.indexOf(stage) + 1];
  return (
    <div ref={setNodeRef} className="kcol"
      style={isOver ? { borderColor: "var(--green)" } : undefined}>
      <h4>{LABELS[stage]} · {items.length}</h4>
      {items.map((v) => (
        <Card key={v.id} v={v} next={next} onFlip={() => onFlip(v, next)} />
      ))}
    </div>
  );
}

export default function Pipeline() {
  const k = useAsync<any>(() => api.get("/api/vehicles/kanban"));
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 180, tolerance: 6 } })
  );
  const move = async (id: string, to: string) => {
    await api.post(`/api/vehicles/${id}/stage`, { stage: to, source: "dashboard" });
    k.reload();
  };
  const onDragEnd = (e: DragEndEvent) => {
    if (e.over && e.active) move(String(e.active.id), String(e.over.id));
  };

  return (
    <>
      <div className="topbar"><span>Pipeline</span></div>
      <div className="scroll">
        {k.loading && <div className="muted">Loading…</div>}
        {k.data && (
          <DndContext sensors={sensors} onDragEnd={onDragEnd}>
            <div className="kanban">
              {k.data.stages.map((s: string) => (
                <Col key={s} stage={s} items={k.data.board[s] || []}
                  onFlip={(v, to) => to && move(v.id, to)} />
              ))}
            </div>
          </DndContext>
        )}
        <p className="muted" style={{ fontSize: 13 }}>
          Drag a card between columns, or tap → for a one-tap stage flip.
          Each move writes back to Notion and stamps a transition timestamp.
        </p>
      </div>
    </>
  );
}

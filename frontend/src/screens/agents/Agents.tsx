/* Agents: fixed left rail (desktop) / fixed-width cards (phone) of six agents with health, live status lines,
   streamed replies, voice input. Agents propose; a person applies. Proposals land in Activity.
   TODO(screen builder): GET /api/agents ; GET /api/agents/{id}/messages ; POST /api/agents/{id}/messages (SSE stream) ;
   POST /api/agents/{id}/proposals/{pid}/apply|discard. */
import { useParams } from "react-router-dom";
import { Scaffold } from "../scaffold";

export default function Agents() {
  const { agentId } = useParams();
  return (
    <Scaffold
      title="Agents"
      subtitle={agentId ? `Talking to ${agentId}` : "Manager, sourcing, shipping, listings, money and customer replies. They propose; you apply."}
      probe="/api/agents"
      emptyTitle="Agents aren't reporting yet"
      emptyBody="Each agent shows its health, what it's doing, and a chat thread once the agent runtime is connected."
      wide
    />
  );
}

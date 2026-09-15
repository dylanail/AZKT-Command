/* Team (owner) / People (manager) as its own page under /settings/team. The section itself lives in
   components/TeamSection.tsx so Settings can embed it too. */
import { TeamSection } from "./components/TeamSection";

export default function Team() {
  return (
    <div className="page">
      <TeamSection level={1} />
    </div>
  );
}

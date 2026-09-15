import { initialOf } from "../lib/format";

export function Avatar({ name, size = "md", className = "" }: { name: string | null | undefined; size?: "sm" | "md"; className?: string }) {
  return (
    <span className={["avatar", size === "sm" ? "avatar--sm" : "", className].filter(Boolean).join(" ")} aria-hidden="true">
      {initialOf(name)}
    </span>
  );
}

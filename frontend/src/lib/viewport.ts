import { useEffect, useState } from "react";

const MOBILE_QUERY = "(max-width: 767px)";

function subscribe(query: string, cb: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mq = window.matchMedia(query);
  if (typeof mq.addEventListener === "function") {
    mq.addEventListener("change", cb);
    return () => mq.removeEventListener("change", cb);
  }
  mq.addListener(cb);
  return () => mq.removeListener(cb);
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    const mq = window.matchMedia(query);
    const update = () => setMatches(mq.matches);
    update();
    return subscribe(query, update);
  }, [query]);
  return matches;
}

/** True under 768px: full-page views with the floating pill nav. */
export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}

export function useIsCompactDesktop(): boolean {
  return useMediaQuery("(min-width: 768px) and (max-width: 1023px)");
}

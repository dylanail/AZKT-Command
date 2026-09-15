import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Theme = "light" | "dark";
export type Glass = "on" | "off";
export type Motion = "system" | "reduced";

export const THEME_KEY = "azkt-theme";
export const GLASS_KEY = "azkt-glass";
export const MOTION_KEY = "azkt-motion";

const CANVAS_LIGHT = "#dfe6f3";
const CANVAS_DARK = "#07080d";

function readStored<T extends string>(key: string, allowed: readonly T[]): T | null {
  try {
    const v = localStorage.getItem(key);
    return v && (allowed as readonly string[]).includes(v) ? (v as T) : null;
  } catch {
    return null;
  }
}
function writeStored(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* private mode etc. */ }
}
function prefers(query: string): boolean {
  return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia(query).matches;
}

/** Apply attributes to <html> (also done by the inline boot script in index.html to avoid a flash). */
export function applyThemeAttributes(theme: Theme, glass: Glass, motion: Motion) {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-glass", glass);
  if (motion === "reduced") root.setAttribute("data-motion", "reduced");
  else root.removeAttribute("data-motion");
  root.style.colorScheme = theme;
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]:not([media])');
  if (meta) meta.content = theme === "dark" ? CANVAS_DARK : CANVAS_LIGHT;
}

interface ThemeContextValue {
  theme: Theme;
  glass: Glass;
  motion: Motion;
  /** Effective reduced-motion (system preference or user override). */
  reducedMotion: boolean;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
  setGlass: (g: Glass) => void;
  toggleGlass: () => void;
  setMotion: (m: Motion) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  // Light is the default (v2 brief). No system-dark auto-switch; dark is an explicit, persisted toggle.
  const [theme, setThemeState] = useState<Theme>(() => readStored(THEME_KEY, ["light", "dark"] as const) ?? "light");
  const [glass, setGlassState] = useState<Glass>(() =>
    readStored(GLASS_KEY, ["on", "off"] as const) ?? (prefers("(prefers-reduced-transparency: reduce)") ? "off" : "on"),
  );
  const [motion, setMotionState] = useState<Motion>(() => readStored(MOTION_KEY, ["system", "reduced"] as const) ?? "system");
  const [systemReduced, setSystemReduced] = useState<boolean>(() => prefers("(prefers-reduced-motion: reduce)"));

  useEffect(() => {
    applyThemeAttributes(theme, glass, motion);
  }, [theme, glass, motion]);

  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const fn = () => setSystemReduced(mq.matches);
    mq.addEventListener?.("change", fn);
    return () => mq.removeEventListener?.("change", fn);
  }, []);

  const setTheme = useCallback((t: Theme) => { setThemeState(t); writeStored(THEME_KEY, t); }, []);
  const setGlass = useCallback((g: Glass) => { setGlassState(g); writeStored(GLASS_KEY, g); }, []);
  const setMotion = useCallback((m: Motion) => { setMotionState(m); writeStored(MOTION_KEY, m); }, []);
  const toggleTheme = useCallback(() => setTheme(theme === "light" ? "dark" : "light"), [theme, setTheme]);
  const toggleGlass = useCallback(() => setGlass(glass === "on" ? "off" : "on"), [glass, setGlass]);

  const value = useMemo<ThemeContextValue>(() => ({
    theme, glass, motion,
    reducedMotion: motion === "reduced" || systemReduced,
    setTheme, toggleTheme, setGlass, toggleGlass, setMotion,
  }), [theme, glass, motion, systemReduced, setTheme, toggleTheme, setGlass, toggleGlass, setMotion]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}

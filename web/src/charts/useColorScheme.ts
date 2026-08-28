import { useEffect, useState } from "react";

/**
 * The active colour scheme, tracked so charts can re-resolve their palette.
 *
 * ECharts holds the colours it was given, so a chart drawn before the OS
 * switched to dark keeps painting the light palette while the rest of the app
 * repaints around it. The tokens are read from CSS at render time, so all this
 * needs to do is tell React that a render is due.
 */
export function useColorScheme(): "light" | "dark" {
  const [scheme, setScheme] = useState<"light" | "dark">(() =>
    typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light",
  );

  useEffect(() => {
    const query = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!query) return;
    const update = (event: MediaQueryListEvent) => setScheme(event.matches ? "dark" : "light");
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return scheme;
}

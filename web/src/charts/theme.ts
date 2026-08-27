/**
 * The ECharts palette is derived from the same tokens as the rest of the app,
 * so a chart never looks bolted on.
 */
export interface ChartTheme {
  accent: string;
  series: string[];
  axis: string;
  grid: string;
  text: string;
  muted: string;
}

function token(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name);
  return value.trim() || fallback;
}

export function chartTheme(): ChartTheme {
  const accent = token("--accent", "#2F6F4F");
  return {
    accent,
    // One accent, then desaturated neighbours -- a categorical rainbow would
    // imply a distinction between series that the data does not have.
    series: [accent, "#7C9885", "#B5C4B1", "#4A6670", "#8B7E74", "#A8998A"],
    axis: token("--border", "#d8d6d1"),
    grid: token("--border-subtle", "#eae8e4"),
    text: token("--text", "#1d1c1a"),
    muted: token("--text-muted", "#6b6862"),
  };
}

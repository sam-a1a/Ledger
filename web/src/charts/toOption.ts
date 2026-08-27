import type { ChartPayload } from "../api/types";
import { chartTheme } from "./theme";

/**
 * The only file that knows what ECharts is.
 *
 * The model emits a neutral spec -- a chart kind and which result columns map
 * to which axis -- and this turns it into a rendering option. That boundary is
 * why the model cannot hallucinate a library option, and why swapping the
 * charting library touches exactly one file.
 */
export interface ChartSpec {
  kind: "bar" | "line" | "area" | "scatter" | "pie" | "heatmap";
  x: string;
  y: string[];
  series?: string | null;
  title: string;
  x_label?: string | null;
  y_label?: string | null;
  stacked?: boolean;
  sort?: "none" | "x_asc" | "y_desc";
}

type Row = unknown[];

export function specToOption(payload: ChartPayload): Record<string, unknown> {
  const spec = payload.spec as unknown as ChartSpec;
  const theme = chartTheme();
  const names = payload.columns.map((c) => c.name);
  const index = (column: string) => names.indexOf(column);

  const rows = sortRows(payload.rows as Row[], spec, index);
  const categories = rows.map((row) => format(row[index(spec.x)]));

  const base = {
    title: {
      text: spec.title,
      left: 0,
      textStyle: { fontSize: 14, fontWeight: 600, color: theme.text },
    },
    color: theme.series,
    grid: { left: 8, right: 16, bottom: 8, top: 48, containLabel: true },
    tooltip: { trigger: spec.kind === "pie" ? "item" : "axis" },
    legend:
      spec.y.length > 1
        ? { top: 24, right: 0, textStyle: { color: theme.muted, fontSize: 11 } }
        : undefined,
    textStyle: { fontFamily: "inherit" },
  };

  if (spec.kind === "pie") {
    const valueIndex = index(spec.y[0] ?? "");
    return {
      ...base,
      tooltip: { trigger: "item" },
      series: [
        {
          type: "pie",
          radius: ["45%", "70%"],
          itemStyle: { borderColor: "#fff", borderWidth: 2 },
          label: { color: theme.muted, fontSize: 11 },
          data: rows.map((row) => ({
            name: format(row[index(spec.x)]),
            value: row[valueIndex],
          })),
        },
      ],
    };
  }

  if (spec.kind === "scatter") {
    const yIndex = index(spec.y[0] ?? "");
    const xIndex = index(spec.x);
    return {
      ...base,
      xAxis: axis(spec.x_label ?? spec.x, theme, "value"),
      yAxis: axis(spec.y_label ?? spec.y[0] ?? "", theme, "value"),
      series: [
        {
          type: "scatter",
          symbolSize: 6,
          data: rows.map((row) => [row[xIndex], row[yIndex]]),
        },
      ],
    };
  }

  const isTime = payload.columns[index(spec.x)]?.type === "timestamp";
  return {
    ...base,
    xAxis: {
      ...axis(spec.x_label ?? spec.x, theme, "category"),
      data: categories,
      axisLabel: {
        color: theme.muted,
        fontSize: 11,
        // Long zone names overlap otherwise; dates never need rotating.
        rotate: !isTime && categories.some((c) => c.length > 10) ? 35 : 0,
        hideOverlap: true,
      },
    },
    yAxis: axis(spec.y_label ?? "", theme, "value"),
    series: spec.y.map((column) => ({
      name: column,
      type: spec.kind === "area" ? "line" : spec.kind,
      smooth: false,
      showSymbol: rows.length <= 60,
      stack: spec.stacked ? "total" : undefined,
      areaStyle: spec.kind === "area" ? { opacity: 0.15 } : undefined,
      itemStyle: { borderRadius: spec.kind === "bar" ? [3, 3, 0, 0] : 0 },
      data: rows.map((row) => row[index(column)]),
    })),
  };
}

function axis(name: string, theme: ReturnType<typeof chartTheme>, type: string) {
  return {
    type,
    name: name || undefined,
    nameTextStyle: { color: theme.muted, fontSize: 11 },
    axisLine: { lineStyle: { color: theme.axis } },
    axisTick: { show: false },
    splitLine: { lineStyle: { color: theme.grid } },
    axisLabel: { color: theme.muted, fontSize: 11 },
  };
}

function sortRows(rows: Row[], spec: ChartSpec, index: (c: string) => number): Row[] {
  if (spec.sort === "x_asc") {
    return rows.slice().sort((a, b) => compare(a[index(spec.x)], b[index(spec.x)]));
  }
  if (spec.sort === "y_desc") {
    const key = index(spec.y[0] ?? "");
    return rows.slice().sort((a, b) => compare(b[key], a[key]));
  }
  return rows;
}

function compare(a: unknown, b: unknown): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b));
}

function format(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" && value.includes("T")) return value.slice(0, 10);
  return String(value);
}

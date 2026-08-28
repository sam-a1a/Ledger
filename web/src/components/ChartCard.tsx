import { Suspense, lazy } from "react";
import type { ChartPayload } from "../api/types";

/**
 * The chart boundary, loaded on demand.
 *
 * ECharts is by far the largest thing in the bundle, and most conversations
 * never draw a chart -- a meta-question, a count, a refused request. Loading it
 * eagerly makes every one of those wait for code they will not use. Split out,
 * the initial download drops by roughly two thirds and the chart chunk is
 * fetched the first time a `plot` call resolves.
 *
 * The fallback reserves the chart's height rather than collapsing to nothing,
 * so the transcript does not jump when it arrives.
 */
const ChartCanvas = lazy(() => import("./ChartCanvas"));

export function ChartCard({ payload }: { payload: ChartPayload }) {
  return (
    <Suspense
      fallback={
        <div className="chart-card chart-loading" data-testid="chart-loading" style={{ height: 300 }} />
      }
    >
      <ChartCanvas payload={payload} />
    </Suspense>
  );
}

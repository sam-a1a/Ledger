// The ESM build, not `lib/` -- the CJS subpath comes through Vite's interop as
// `{ default: fn }` rather than the component, and React reports only a
// minified "invalid element type" with no clue which import caused it.
import ReactEChartsCore from "echarts-for-react/esm/core";
import type { ChartPayload } from "../api/types";
import echarts from "../charts/echarts";
import { specToOption } from "../charts/toOption";

export function ChartCard({ payload }: { payload: ChartPayload }) {
  return (
    <div className="chart-card" data-testid="chart-card">
      <ReactEChartsCore
        echarts={echarts}
        option={specToOption(payload)}
        style={{ height: 300, width: "100%" }}
        notMerge
        opts={{ renderer: "canvas" }}
      />
    </div>
  );
}

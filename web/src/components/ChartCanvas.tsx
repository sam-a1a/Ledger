// The ESM build, not `lib/` -- the CJS subpath comes through Vite's interop as
// `{ default: fn }` rather than the component, and React reports only a
// minified "invalid element type" with no clue which import caused it.
import ReactEChartsCore from "echarts-for-react/esm/core";
import { useMemo } from "react";
import type { ChartPayload } from "../api/types";
import echarts from "../charts/echarts";
import { specToOption } from "../charts/toOption";
import { useColorScheme } from "../charts/useColorScheme";

export default function ChartCanvas({ payload }: { payload: ChartPayload }) {
  // The palette is read from CSS custom properties inside `specToOption`, so
  // recomputing on a scheme change is all it takes to repaint. Keyed on the
  // payload as well, so an unrelated render does not rebuild the option.
  const scheme = useColorScheme();
  const option = useMemo(() => specToOption(payload), [payload, scheme]);

  return (
    <div className="chart-card" data-testid="chart-card" data-scheme={scheme}>
      <ReactEChartsCore
        echarts={echarts}
        option={option}
        style={{ height: 300, width: "100%" }}
        notMerge
        opts={{ renderer: "canvas" }}
      />
    </div>
  );
}

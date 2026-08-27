/**
 * A minimal ECharts build.
 *
 * Importing `echarts` whole pulls in every chart type and both renderers --
 * about 1.3 MB before gzip, most of it for charts this app never draws.
 * Registering only what `toOption.ts` can actually emit keeps the bundle
 * proportional to the product.
 */
import { BarChart, HeatmapChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  HeatmapChart,
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

export default echarts;

"use client";

import { useEffect, useRef, useState } from "react";
import { Chip, Panel } from "./ui";
import { price } from "@/lib/format";
import type { Candle, MarketPayload } from "@/lib/types";

/** Candles rendered with lightweight-charts (dynamically imported, client-side only). */
export default function ChartPanel({
  market,
  asset,
  onLoad,
  loading,
  error,
}: {
  market: MarketPayload | null;
  asset: string;
  onLoad: (asset: string) => void;
  loading: boolean;
  error: string | null;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<{ remove: () => void; applyOptions: (options: object) => void } | null>(null);
  const [renderError, setRenderError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const container = containerRef.current;
    const candles: Candle[] = market?.candles ?? [];

    const destroy = () => {
      try {
        chartRef.current?.remove();
      } catch {
        /* chart already disposed */
      }
      chartRef.current = null;
    };

    if (!container) return;
    if (candles.length === 0) {
      destroy();
      container.innerHTML = "";
      return;
    }

    (async () => {
      try {
        const lib = await import("lightweight-charts");
        if (cancelled || !containerRef.current) return;
        destroy();
        containerRef.current.innerHTML = "";

        const chart = lib.createChart(containerRef.current, {
          layout: { background: { color: "transparent" }, textColor: "#6b7793", fontSize: 10 },
          grid: {
            vertLines: { color: "rgba(255,255,255,0.04)" },
            horzLines: { color: "rgba(255,255,255,0.04)" },
          },
          rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
          timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true, secondsVisible: false },
          crosshair: { mode: lib.CrosshairMode.Normal },
          height: 260,
          width: containerRef.current.clientWidth,
        });
        chartRef.current = chart;

        const series = chart.addCandlestickSeries({
          upColor: "#12d18e",
          downColor: "#ff4d6d",
          borderUpColor: "#12d18e",
          borderDownColor: "#ff4d6d",
          wickUpColor: "#12d18e",
          wickDownColor: "#ff4d6d",
        });
        series.setData(
          candles.map((candle) => ({
            // lightweight-charts expects UNIX seconds for intraday series
            time: candle.epoch as unknown as string,
            open: candle.open,
            high: candle.high,
            low: candle.low,
            close: candle.close,
          })),
        );
        chart.timeScale().fitContent();
        setRenderError(null);
      } catch (err) {
        setRenderError((err as Error).message);
      }
    })();

    const resize = () => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", resize);
    return () => {
      cancelled = true;
      window.removeEventListener("resize", resize);
      destroy();
    };
  }, [market]);

  const feed = market?.feed;

  return (
    <Panel
      title={`${asset} · M1 candles`}
      subtitle="Closed candles only. The in-progress candle is shown as a live price, never as data the model has seen."
      right={
        <div className="flex items-center gap-2">
          {market?.live_price !== null && market?.live_price !== undefined && (
            <Chip tone="slate">live {price(market.live_price, 5)}</Chip>
          )}
          <button
            type="button"
            onClick={() => onLoad(asset)}
            disabled={loading}
            className="btn-ghost !px-3 !py-1.5 !text-[11px]"
          >
            {loading ? "loading…" : "load chart"}
          </button>
        </div>
      }
    >
      {error && <p className="text-[12px] text-rose-300">{error}</p>}
      {!error && !market && !loading && (
        <p className="text-[12px] text-ink-500">Load the chart to inspect the candles the engine is analysing.</p>
      )}
      {renderError && <p className="text-[12px] text-rose-300">Chart render error: {renderError}</p>}
      <div ref={containerRef} className="h-[260px] w-full" />
      {feed && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-500">
          <span>
            feed: <span className="font-mono text-ink-300">{feed.source_label}</span>
          </span>
          <span>
            badge: <span className="font-mono text-ink-300">{feed.badge}</span>
          </span>
          <span>
            candles: <span className="font-mono text-ink-300">{feed.candles}</span>
          </span>
        </div>
      )}
    </Panel>
  );
}

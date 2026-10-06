"use client";

import { Chip, Panel } from "./ui";
import { price, relativeTime } from "@/lib/format";
import type { Feed, FeaturesPayload } from "@/lib/types";

export default function MarketConditionsPanel({
  features,
  feed,
}: {
  features: FeaturesPayload | null;
  feed: Feed | null;
}) {
  const extras = features?.extras;
  const sr = extras?.support_resistance;

  return (
    <Panel
      title="Market conditions"
      subtitle="Computed from the newest closed M1 candle — ATR-normalised so the numbers are comparable across assets."
      right={
        feed ? (
          <Chip tone={feed.simulated ? "amber" : feed.realtime ? "green" : "amber"} title={feed.notes}>
            {feed.badge} · {feed.provider}
          </Chip>
        ) : undefined
      }
    >
      {!extras && <p className="text-[12px] text-ink-500">No market snapshot yet.</p>}

      {extras && (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Metric label="price" value={price(features?.price, 5)} />
            <Metric label="ATR(14)" value={`${extras.atr_pct ?? "—"}%`} hint={`abs ${extras.atr14 ?? "—"}`} />
            <Metric label="RSI 14" value={String(extras.rsi14 ?? "—")} hint={`RSI 7 ${extras.rsi7 ?? "—"}`} />
            <Metric label="ADX 14" value={String(extras.adx14 ?? "—")} hint={`±DI ${extras.plus_di ?? "—"}/${extras.minus_di ?? "—"}`} />
            <Metric label="EMA stack" value={String(extras.ema_stack ?? "—")} hint={`9 ${extras.ema9 ?? "—"}`} />
            <Metric label="MACD hist" value={String(extras.macd_hist ?? "—")} />
            <Metric label="Efficiency 10" value={String(extras.efficiency_ratio_10 ?? "—")} />
            <Metric
              label="Volume z"
              value={extras.has_volume ? String(extras.volume_z ?? "—") : "n/a"}
              hint={extras.has_volume ? "vs 20-candle mean" : "feed has no volume"}
            />
          </div>

          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <div className="panel-tight px-3 py-2.5">
              <div className="label mb-1.5">last closed candle · {extras.candle_time_ist ?? "—"} IST</div>
              <ul className="space-y-1 text-[12px] text-ink-300">
                <li className="flex justify-between">
                  <span>O / H / L / C</span>
                  <span className="font-mono text-ink-100">
                    {price(extras.last_candle?.open, 5)} · {price(extras.last_candle?.high, 5)} ·{" "}
                    {price(extras.last_candle?.low, 5)} · {price(extras.last_candle?.close, 5)}
                  </span>
                </li>
                <li className="flex justify-between">
                  <span>direction</span>
                  <span className="font-mono text-ink-100">{extras.last_candle?.direction ?? "—"}</span>
                </li>
                <li className="flex justify-between">
                  <span>body ratio</span>
                  <span className="font-mono text-ink-100">{extras.last_candle?.body_ratio ?? "—"}</span>
                </li>
                <li className="flex justify-between">
                  <span>wick up / down</span>
                  <span className="font-mono text-ink-100">
                    {extras.last_candle?.upper_wick_ratio ?? "—"} / {extras.last_candle?.lower_wick_ratio ?? "—"}
                  </span>
                </li>
                <li className="flex justify-between">
                  <span>consecutive run</span>
                  <span className="font-mono text-ink-100">{extras.consecutive_run ?? "—"}</span>
                </li>
              </ul>
            </div>

            <div className="panel-tight px-3 py-2.5">
              <div className="label mb-1.5">structure (confirmed pivots)</div>
              <ul className="space-y-1 text-[12px] text-ink-300">
                <li className="flex justify-between">
                  <span>support</span>
                  <span className="font-mono text-ink-100">{price(sr?.nearest_support, 5)}</span>
                </li>
                <li className="flex justify-between">
                  <span>resistance</span>
                  <span className="font-mono text-ink-100">{price(sr?.nearest_resistance, 5)}</span>
                </li>
                <li className="flex justify-between">
                  <span>distance to support</span>
                  <span className="font-mono text-ink-100">{sr?.support_distance_atr ?? "—"} ATR</span>
                </li>
                <li className="flex justify-between">
                  <span>distance to resistance</span>
                  <span className="font-mono text-ink-100">{sr?.resistance_distance_atr ?? "—"} ATR</span>
                </li>
                <li className="flex justify-between">
                  <span>range position</span>
                  <span className="font-mono text-ink-100">{sr?.range_position ?? "—"}</span>
                </li>
              </ul>
            </div>
          </div>

          {feed && (
            <div className="mt-3 border-t border-white/5 pt-3 text-[11px] text-ink-500">
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                <span>
                  source: <span className="font-mono text-ink-300">{feed.source_label}</span>
                </span>
                <span>
                  mode: <span className="font-mono text-ink-300">{feed.mode}</span>
                </span>
                <span>
                  candles: <span className="font-mono text-ink-300">{feed.candles}</span>
                </span>
                <span>
                  data age: <span className="font-mono text-ink-300">{relativeTime(feed.latest_candle_close_utc)}</span>
                </span>
              </div>
              {feed.notes && <p className="mt-1.5 leading-relaxed">{feed.notes}</p>}
              {feed.fallback_chain && feed.fallback_chain.length > 1 && (
                <p className="mt-1 leading-relaxed">
                  provider chain:{" "}
                  {feed.fallback_chain
                    .map((step) => `${step.provider} ${step.status}${step.reason ? ` (${step.reason})` : ""}`)
                    .join(" → ")}
                </p>
              )}
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="panel-tight px-3 py-2">
      <div className="label">{label}</div>
      <div className="mt-0.5 truncate font-mono text-[13px] text-ink-100">{value}</div>
      {hint && <div className="truncate text-[10px] text-ink-500">{hint}</div>}
    </div>
  );
}

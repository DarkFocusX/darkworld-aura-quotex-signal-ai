"use client";

import { useEffect, useState } from "react";
import { Bar, Chip, Notice, Spinner } from "./ui";
import { countdown, directionArrow, pct, price, qualityBadgeClass, qualityColor, qualityLabel } from "@/lib/format";
import type { GenerateResponse, Signal } from "@/lib/types";

type Phase = "idle" | "scanning" | "done" | "error";

export default function SignalCard({
  phase,
  result,
  signal,
  error,
  secondsToEntry,
  stageIndex,
  stages,
  minConfidence,
}: {
  phase: Phase;
  result: GenerateResponse | null;
  signal: Signal | null;
  error: string | null;
  secondsToEntry: number | null;
  stageIndex: number;
  stages: string[];
  minConfidence: number;
}) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);

  const status = result?.status;
  const isSignal = status === "SIGNAL";
  const noTrade = status && status !== "SIGNAL" && phase === "done";
  const direction = (signal?.direction || result?.direction || "") as string;

  const glow = isSignal ? (direction === "PUT" ? "signal-glow-put" : "signal-glow-call") : "signal-glow-none";

  const copyText = isSignal && signal
    ? [
        "🚨 QUOTEX M1 PAPER SIGNAL",
        "",
        `${signal.flag ?? "🔔"} ${signal.asset}`,
        `💎 ${signal.timeframe}`,
        `🕐 ${signal.entry_time_ist} IST`,
        `${directionArrow(direction)} ${direction}`,
        "",
        `🎯 MODEL CONFIDENCE: ${signal.confidence.toFixed(1)}%`,
        `⏳ EXPIRY: ${signal.expiry_minutes} MIN`,
        `📡 DATA: ${signal.is_simulated ? "SIMULATED" : signal.is_replay ? "REPLAY" : "REAL"}`,
        `MODEL: ${signal.model_version ?? "—"}`,
        `STATUS: ${qualityLabel(signal.quality)}`,
      ].join("\n")
    : "";

  return (
    <section
      className={`panel relative overflow-hidden px-4 py-5 sm:px-7 sm:py-7 ${glow} animate-riseIn`}
      aria-live="polite"
    >
      {/* status row */}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {phase === "scanning" && (
            <Chip tone="blue">
              <Spinner /> SCANNING MARKET…
            </Chip>
          )}
          {phase === "done" && isSignal && <Chip tone="green">✅ SIGNAL</Chip>}
          {phase === "done" && noTrade && (
            <Chip tone={status === "LOCKED" ? "amber" : "red"}>🚫 {status?.replace("_", " ")}</Chip>
          )}
          {phase === "idle" && <Chip tone="slate">READY — press GENERATE SIGNAL</Chip>}
          {phase === "error" && <Chip tone="red">⚠️ REQUEST FAILED</Chip>}
          {isSignal && signal?.is_simulated && <Chip tone="amber">⚠️ SIMULATED FEED — NOT MARKET DATA</Chip>}
          {isSignal && !signal?.is_simulated && signal?.feed_mode === "REPLAY" && (
            <Chip tone="amber">DELAYED / REPLAY FEED</Chip>
          )}
        </div>
        {isSignal && signal && (
          <button
            type="button"
            onClick={() => {
              navigator.clipboard?.writeText(copyText);
              setCopied(true);
            }}
            className="btn-ghost !px-3 !py-1.5 !text-[11px]"
          >
            {copied ? "copied ✓" : "copy signal"}
          </button>
        )}
      </div>

      {/* idle */}
      {phase === "idle" && (
        <div className="py-6 text-center">
          <div className="text-4xl">🛰️</div>
          <h2 className="mt-3 text-lg font-semibold text-ink-100">No active signal</h2>
          <p className="mx-auto mt-2 max-w-md text-[12px] leading-relaxed text-ink-500">
            The engine only produces a signal when live data, the model ensemble, the market regime and the
            confidence threshold all pass. Press <span className="text-ink-300">GENERATE SIGNAL</span> to run the
            pipeline for the next M1 candle.
          </p>
        </div>
      )}

      {/* scanning stages */}
      {phase === "scanning" && (
        <div className="py-3">
          <div className="mb-3 text-center">
            <div className="font-mono text-3xl font-bold tracking-tight text-ink-100">
              {result?.asset ?? "AUTO SELECT"}
            </div>
            <div className="label mt-1">pipeline running</div>
          </div>
          <ol className="mx-auto grid max-w-md gap-1.5">
            {stages.map((stage, index) => {
              const state = index < stageIndex ? "done" : index === stageIndex ? "active" : "pending";
              return (
                <li
                  key={stage}
                  className={`flex items-center gap-2 rounded-lg px-3 py-1.5 text-[12px] transition
                    ${state === "done" ? "text-emerald-300" : state === "active" ? "bg-white/5 text-ink-100" : "text-ink-500"}`}
                >
                  <span className="w-4 text-center">
                    {state === "done" ? "✓" : state === "active" ? <Spinner className="h-3 w-3" /> : "·"}
                  </span>
                  {stage}
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {/* error */}
      {phase === "error" && (
        <Notice tone="red" title="Signal engine unreachable">
          {error || "The backend did not respond. No signal was produced — nothing is fabricated on failure."}
        </Notice>
      )}

      {/* signal */}
      {phase === "done" && isSignal && signal && (
        <div className="animate-riseIn">
          <div className="grid items-center gap-5 sm:grid-cols-[1.15fr_0.85fr]">
            <div>
              <div className="font-mono text-[26px] font-bold leading-tight text-ink-100 sm:text-3xl">
                {signal.flag} {signal.asset}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-3 text-[13px] text-ink-300">
                <span className="font-mono">💎 {signal.timeframe}</span>
                <span className="font-mono">🕐 ENTRY {signal.entry_time_ist} IST</span>
              </div>

              <div className="mt-5 flex items-end gap-4">
                <div className={`text-5xl font-black leading-none sm:text-6xl ${direction === "PUT" ? "text-put" : "text-call"}`}>
                  {direction === "PUT" ? "🔻" : "🔺"} {direction}
                </div>
              </div>

              <div className="mt-4 grid grid-cols-2 gap-3 sm:max-w-sm">
                <div>
                  <div className="label">expiry</div>
                  <div className="font-mono text-lg text-ink-100">{signal.expiry_minutes} MIN</div>
                  <div className="text-[11px] text-ink-500">closes {signal.expiry_time_ist} IST</div>
                </div>
                <div>
                  <div className="label">entry countdown</div>
                  <div className="font-mono text-lg text-ink-100">
                    {secondsToEntry !== null && secondsToEntry > 0 ? countdown(secondsToEntry) : "NOW"}
                  </div>
                  <div className="text-[11px] text-ink-500">next candle boundary</div>
                </div>
              </div>
            </div>

            {/* confidence dial */}
            <div className="panel-tight p-4">
              <div className="label">🤖 model confidence (a calculated score — not accuracy)</div>
              <div className={`mt-1 font-mono text-5xl font-black leading-none ${qualityColor(signal.quality)}`}>
                {pct(signal.confidence, 1)}
              </div>
              <div className="mt-2">
                <Bar
                  value={signal.confidence}
                  tone={signal.quality === "VERY_HIGH" || signal.quality === "HIGH" ? "emerald" : "amber"}
                  height="h-2"
                />
              </div>
              <div className={`chip mt-3 ${qualityBadgeClass(signal.quality)}`}>
                {signal.quality === "VERY_HIGH" ? "✅" : "✔"} {qualityLabel(signal.quality)}
              </div>
              <dl className="mt-4 space-y-1.5 text-[12px]">
                <Row label="model agreement" value={pct((signal.model_agreement ?? 0) * 100, 0)} />
                <Row
                  label="data"
                  value={
                    signal.is_real_market_data
                      ? signal.is_replay
                        ? "REAL (REPLAY)"
                        : "REAL (LIVE)"
                      : signal.feed_badge ?? "SIMULATED"
                  }
                />
                <Row label="direction probability" value={pct((signal.model_probability ?? 0) * 100, 1)} />
                <Row label="price at signal" value={price(signal.price, 5)} />
              </dl>
            </div>
          </div>

          <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-white/5 pt-3 text-[11px] text-ink-500">
            <span>
              data timestamp: <span className="font-mono text-ink-300">{signal.data_timestamp_ist ?? "—"}</span>
            </span>
            <span>
              feed: <span className="font-mono text-ink-300">{signal.feed_source ?? "—"}</span>
            </span>
            <span>
              mode: <span className="font-mono text-ink-300">{signal.scan_mode ?? "NOW"}</span>
            </span>
            <span>
              status: <span className="font-mono text-ink-300">PAPER · {signal.result ?? "PENDING"}</span>
            </span>
          </div>

          <p className="mt-3 text-[11px] leading-relaxed text-ink-500">
            Confidence is a calculated model score — not a guarantee of profit. Paper trading only; no order is sent
            to any broker.
          </p>
        </div>
      )}

      {/* no trade */}
      {phase === "done" && noTrade && (
        <div className="animate-riseIn">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <div className="text-4xl font-black tracking-tight text-amber-300 sm:text-5xl">NO TRADE</div>
              <div className="mt-2 font-mono text-sm text-ink-300">
                {result?.confidence !== null && result?.confidence !== undefined
                  ? `Confidence: ${pct(result.confidence, 1)} (threshold ${pct(result.min_confidence ?? minConfidence, 0)})`
                  : "No qualified setup"}
              </div>
            </div>
            {result?.next_valid_scan_ist && (
              <div className="panel-tight px-4 py-3">
                <div className="label">next valid scan</div>
                <div className="font-mono text-lg text-ink-100">{result.next_valid_scan_ist} IST</div>
              </div>
            )}
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <Notice tone="amber" title={`Reason — ${result?.reason ?? "validation gate failed"}`}>
              {result?.reason_detail || "The setup did not pass the validation gates."}
            </Notice>
            {status === "LOCKED" && (
              <Notice tone="blue" title="Signal locked">
                A signal already exists for this candle and asset. Duplicate signals for the same candle are blocked.
              </Notice>
            )}
            {status === "DATA_UNAVAILABLE" && (
              <Notice tone="red" title="Data unavailable">
                No verified candles were available, so no signal could be evaluated. The engine never invents prices.
              </Notice>
            )}
            {status === "MODEL_NOT_READY" && (
              <Notice tone="red" title="Model not ready">
                Train the models first: <span className="font-mono">python -m backend.research.train</span>
              </Notice>
            )}
          </div>

          {result?.rejections && result.rejections.length > 0 && (
            <div className="mt-4">
              <div className="label mb-2">asset scan results</div>
              <ul className="grid gap-1.5 sm:grid-cols-2">
                {result.rejections.map((rejection) => (
                  <li
                    key={rejection.asset}
                    className="panel-tight flex items-center justify-between gap-2 px-3 py-2 text-[12px]"
                  >
                    <span className="font-mono text-ink-300">{rejection.asset}</span>
                    <span className="truncate text-ink-500" title={rejection.reason}>
                      {rejection.confidence !== null && rejection.confidence !== undefined
                        ? `${pct(rejection.confidence, 1)} · `
                        : ""}
                      {rejection.reason}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-ink-500">{label}</dt>
      <dd className="font-mono text-ink-100">{value}</dd>
    </div>
  );
}

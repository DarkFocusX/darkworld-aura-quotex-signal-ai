"use client";

import { Chip, Notice, Panel } from "./ui";
import { directionArrow, pct } from "@/lib/format";
import type { GenerateResponse } from "@/lib/types";

export default function DailyPanel({
  daily,
  loading,
  onRefresh,
  disabled,
}: {
  daily: GenerateResponse | null;
  loading: boolean;
  onRefresh: () => void;
  disabled?: boolean;
}) {
  const emitted = daily?.emitted_today;
  const study = daily?.study;
  const status = daily?.status;

  return (
    <Panel
      title="Best signal of the day (DAILY mode)"
      subtitle="One strongest qualified setup per IST trading day. If nothing qualifies, the day simply has no trade."
      right={
        <button type="button" onClick={onRefresh} disabled={loading || disabled} className="btn-ghost !px-3 !py-1.5 !text-[11px]">
          {loading ? "scanning…" : "refresh daily"}
        </button>
      }
    >
      {!daily && !loading && (
        <p className="text-[12px] text-ink-500">
          Load the daily status to replay today&apos;s closed candles through the pipeline.
        </p>
      )}

      {loading && <p className="text-[12px] text-ink-500">Replaying today&apos;s candles through the pipeline…</p>}

      {daily && !loading && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone={status === "SIGNAL" ? "green" : status === "STUDY_ONLY" ? "blue" : "amber"}>
              {status === "SIGNAL" ? "TODAY'S SIGNAL" : status === "STUDY_ONLY" ? "STUDY ONLY" : "NO TRADE"}
            </Chip>
            <span className="text-[11px] text-ink-500">
              day {daily.day_ist} IST · threshold {pct(daily.min_confidence ?? 85, 0)}
            </span>
          </div>

          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <div className="panel-tight px-3 py-3">
              <div className="label mb-1.5">emitted signals today</div>
              <div className="font-mono text-2xl text-ink-100">{emitted?.count ?? 0}</div>
              {emitted?.best ? (
                <ul className="mt-2 space-y-1 text-[12px] text-ink-300">
                  <li className="flex justify-between">
                    <span>best</span>
                    <span className="font-mono text-ink-100">
                      {emitted.best.asset} {directionArrow(emitted.best.direction)} {emitted.best.direction}
                    </span>
                  </li>
                  <li className="flex justify-between">
                    <span>confidence</span>
                    <span className="font-mono text-ink-100">{pct(emitted.best.confidence, 1)}</span>
                  </li>
                  <li className="flex justify-between">
                    <span>entry</span>
                    <span className="font-mono text-ink-100">{emitted.best.entry_time_ist} IST</span>
                  </li>
                  <li className="flex justify-between">
                    <span>result</span>
                    <span className="font-mono text-ink-100">{emitted.best.result}</span>
                  </li>
                </ul>
              ) : (
                <p className="mt-2 text-[11px] text-ink-500">No live signal emitted today.</p>
              )}
            </div>

            <div className="panel-tight px-3 py-3">
              <div className="label mb-1.5">study replay (already closed candles)</div>
              {study?.best ? (
                <ul className="space-y-1 text-[12px] text-ink-300">
                  <li className="flex justify-between">
                    <span>setup</span>
                    <span className="font-mono text-ink-100">
                      {study.asset} {directionArrow(study.best.direction)} {study.best.direction}
                    </span>
                  </li>
                  <li className="flex justify-between">
                    <span>confidence</span>
                    <span className="font-mono text-ink-100">{pct(study.best.confidence, 1)}</span>
                  </li>
                  <li className="flex justify-between">
                    <span>candle time</span>
                    <span className="font-mono text-ink-100">{study.best.entry_time_ist} IST</span>
                  </li>
                  {study.best.outcome && (
                    <li className="flex justify-between">
                      <span>would have</span>
                      <span className="font-mono text-ink-100">{study.best.outcome.result}</span>
                    </li>
                  )}
                </ul>
              ) : (
                <p className="text-[11px] text-ink-500">
                  No candle today met the gates — the honest outcome. Nothing is forced.
                </p>
              )}
              {study?.in_sample !== null && study?.in_sample !== undefined && (
                <p className={`mt-2 text-[10px] ${study.in_sample ? "text-amber-300" : "text-emerald-300"}`}>
                  {study.in_sample ? "⚠️ " : ""}
                  {study.in_sample_note}
                </p>
              )}
            </div>
          </div>

          {status === "NO_TRADE" && (
            <Notice tone="amber" title="DAILY STATUS — no qualified setup found yet" className="mt-3">
              {daily.reason}
              {daily.study?.scanned_assets?.length ? ` (scanned: ${daily.study.scanned_assets.join(", ")})` : ""}
            </Notice>
          )}

          {study?.note && <p className="mt-3 text-[11px] leading-relaxed text-ink-500">{study.note}</p>}
        </>
      )}
    </Panel>
  );
}

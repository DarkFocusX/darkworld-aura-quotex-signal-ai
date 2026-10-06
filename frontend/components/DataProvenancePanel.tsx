"use client";

import { Chip, Panel } from "./ui";
import type { DataStatusPayload, DatasetStatus } from "@/lib/types";

/** DATA PROVENANCE panel — where every candle came from (spec §12).
 *
 * Shows, per asset: the four-state badge (LIVE / REPLAY / SIMULATED / DATA
 * UNAVAILABLE), row count, newest candle, freshness, the validation report and
 * the operator import record. Nothing here is estimated: an unavailable asset
 * lists the providers that were tried and the reason each one failed.
 */
export default function DataProvenancePanel({
  status,
  error,
}: {
  status: DataStatusPayload | null;
  error?: string | null;
}) {
  const counts = status?.counts;
  const chipTone =
    !counts ? "slate" : counts.real_market > 0 ? "green" : counts.available > 0 ? "amber" : "red";

  return (
    <Panel
      title="Data provenance"
      subtitle="Datasets actually on disk — provider, timeframe, validation and import record per asset."
      right={
        <Chip tone={chipTone}>
          {counts
            ? `${counts.real_market} real · ${counts.simulated} simulated · ${counts.unavailable} unavailable`
            : "—"}
        </Chip>
      }
    >
      {error && <p className="text-[12px] text-rose-300">{error}</p>}
      {!status && !error && <p className="text-[12px] text-ink-500">Loading dataset provenance…</p>}

      {status?.assets?.map((row) => (
        <DatasetRow key={row.asset} row={row} />
      ))}

      {status && (
        <div className="mt-3 text-[11px] text-ink-500">
          datasets folder <span className="font-mono text-ink-300">{status.datasets_dir ?? "—"}</span> · mode{" "}
          <span className="font-mono text-ink-300">{status.data_source_mode ?? "—"}</span> · environment{" "}
          <span className="font-mono text-ink-300">{status.environment ?? "—"}</span>
        </div>
      )}
    </Panel>
  );
}

const BADGE_TONE: Record<string, "green" | "blue" | "amber" | "red" | "slate"> = {
  LIVE: "green",
  REPLAY: "blue",
  SIMULATED: "amber",
  "DATA UNAVAILABLE": "red",
};

function DatasetRow({ row }: { row: DatasetStatus }) {
  const unavailable = row.status === "DATA_UNAVAILABLE";
  const badge = row.badge ?? row.display_status ?? (unavailable ? "DATA UNAVAILABLE" : "—");
  const validation = row.validation;
  const tried = row.providers_tried ?? row.tried ?? [];

  return (
    <div className="mb-2 rounded-xl border border-white/5 bg-white/[0.02] p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[13px] text-ink-100">{row.asset}</span>
          <Chip tone={BADGE_TONE[badge] ?? "slate"}>{badge}</Chip>
          {row.source_class && <Chip tone="slate">{row.source_class}</Chip>}
          {row.timeframe && <Chip tone="slate">{row.timeframe}</Chip>}
        </div>
        <div className="text-[11px] text-ink-500">
          {row.provider ?? "—"} · {row.status}
        </div>
      </div>

      {unavailable ? (
        <div className="mt-2">
          <div className="text-[11px] text-ink-300">{row.reason}</div>
          {row.detail && <div className="text-[10px] text-ink-500">{row.detail}</div>}
          {tried.length > 0 && (
            <div className="mt-1 font-mono text-[10px] text-ink-500">
              tried: {tried.map((step) => `${step.provider} (${step.status})`).join(" → ")}
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-3">
            <Field label="rows" value={row.rows?.toLocaleString() ?? "—"} sub="validated candles" />
            <Field
              label="latest candle"
              value={(row.latest_candle_open_utc ?? "—").replace("T", " ").slice(0, 16) + "Z"}
              sub="closed"
            />
            <Field
              label="freshness"
              value={row.freshness_seconds === null || row.freshness_seconds === undefined
                ? "—"
                : `${Math.round(row.freshness_seconds)}s`}
              sub="vs 180s replay budget"
            />
            <Field
              label="validation"
              value={validation?.status ?? "—"}
              sub={validation?.interval_minutes ? `${validation.interval_minutes}-min grid` : undefined}
            />
            <Field
              label="missing"
              value={validation ? String(validation.missing_candles) : "—"}
              sub={validation ? `${(validation.missing_ratio * 100).toFixed(2)}% inside sessions` : undefined}
            />
            <Field
              label="flat candles"
              value={validation ? `${((validation.flat_ratio ?? 0) * 100).toFixed(1)}%` : "—"}
              sub="open = high = low = close"
            />
          </div>

          {row.provenance?.filename && (
            <div className="mt-2 text-[10px] text-ink-500">
              file <span className="font-mono text-ink-300">{row.provenance.filename}</span>
              {row.provenance.imported_at_utc && (
                <>
                  {" "}· imported{" "}
                  <span className="font-mono text-ink-300">
                    {row.provenance.imported_at_utc.replace("T", " ").slice(0, 19)}
                  </span>{" "}
                  UTC
                </>
              )}
              {row.provenance.sha256 && (
                <>
                  {" "}· sha256 <span className="font-mono text-ink-300">{row.provenance.sha256.slice(0, 12)}…</span>
                </>
              )}
              {row.provenance.operator_label && <> · {row.provenance.operator_label}</>}
            </div>
          )}

          {validation?.errors && validation.errors.length > 0 && (
            <ul className="mt-2 list-disc pl-4 text-[10px] text-rose-300">
              {validation.errors.slice(0, 3).map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          )}
          {validation?.warnings && validation.warnings.length > 0 && (
            <div className="mt-1 text-[10px] text-amber-300">warnings: {validation.warnings.slice(0, 2).join(" · ")}</div>
          )}
        </>
      )}
    </div>
  );
}

function Field({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-ink-500">{label}</div>
      <div className="font-mono text-[12px] text-ink-100">{value}</div>
      {sub && <div className="text-[10px] text-ink-500">{sub}</div>}
    </div>
  );
}

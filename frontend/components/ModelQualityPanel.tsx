"use client";

import { Chip, KeyValue, Notice, Panel, Stat } from "./ui";
import type { ModelStatus, PerformancePayload, ResearchLatest } from "@/lib/types";

/** MODEL QUALITY panel (§15 of the research phase).
 *
 * Answers four separate questions and never blurs them:
 *   1. What artifact is loaded (version, data, rows, period, last retrain)?
 *   2. What did offline validation measure (walk-forward + untouched holdout)?
 *   3. What has live paper trading actually done so far?
 *   4. Did the research run find any confidence gate worth using?
 *
 * The headline never reads "profitable". It reads MODEL STATUS (READY_MARKET /
 * READY_DEMO / NOT_READY) and, underneath, PERFORMANCE: NOT VALIDATED FOR
 * PROFITABILITY — until enough decided paper trades say otherwise.
 */
export default function ModelQualityPanel({
  model,
  performance,
  research,
  error,
}: {
  model: ModelStatus | null;
  performance: PerformancePayload | null;
  research: ResearchLatest | null;
  error?: string | null;
}) {
  const state = model?.state ?? null;
  const holdout = (model?.holdout_metrics ?? {}) as Record<string, number>;
  const validation = (model?.validation_metrics ?? {}) as Record<string, number>;
  const paper = performance?.signal_performance;
  const decided = paper?.decided ?? 0;
  const bound = wilsonLowerBound(paper?.wins ?? 0, decided);
  const evidenceCells = research?.experiments_with_evidence?.length ?? null;
  const notValidated =
    decided < 100 || bound === null || bound <= 0.5 || (evidenceCells !== null && evidenceCells === 0);

  const verdict = notValidated
    ? {
        tone: "red" as const,
        text: "NOT VALIDATED FOR PROFITABILITY",
        why:
          decided < 100
            ? `only ${decided} live paper trade(s) decided so far — far too few observations to judge anything`
            : bound !== null && bound <= 0.5
              ? `paper win-rate Wilson 95% lower bound is ${(bound * 100).toFixed(1)}% (≤ 50%) — no evidence of an edge`
              : "the research run found no confidence gate clearing the evidence bar on validation data",
      }
    : {
        tone: "amber" as const,
        text: "PARTIAL EVIDENCE — STILL NOT A PROFITABILITY CLAIM",
        why: `${decided} decided paper trades, Wilson lower bound ${(bound! * 100).toFixed(1)}% — encouraging at best, never a guarantee`,
      };

  const period = (model?.data_period ?? {}) as { start_utc?: string; end_utc?: string };
  const assets = (model?.asset_list ?? []).join(", ") || "—";
  const fmt = (v: number | undefined | null, digits = 4) =>
    v === undefined || v === null ? "—" : v.toFixed(digits);
  const pct = (v: number | null | undefined) =>
    v === undefined || v === null ? "—" : `${(v * 100).toFixed(1)}%`;

  return (
    <Panel
      title="Model quality"
      subtitle="Artifact provenance, offline validation, live paper results and research evidence — reported separately, never blended."
      right={
        <Chip tone={state === "READY_MARKET" ? "green" : state === "READY_DEMO" ? "amber" : "red"}>
          {state ?? "—"}
        </Chip>
      }
    >
      {error && <p className="text-[12px] text-rose-300">{error}</p>}

      <Notice tone={verdict.tone === "red" ? "red" : "amber"}>
        <div className="font-semibold">
          MODEL STATUS: {state ?? "—"} · version <span className="font-mono">{model?.version ?? "—"}</span>
        </div>
        <div className="font-semibold">PERFORMANCE: {verdict.text}</div>
        <div className="mt-1 text-[11px] opacity-80">{verdict.why}</div>
      </Notice>

      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
        <Stat label="Training rows" value={(model?.row_count ?? model?.training_rows ?? 0).toLocaleString()} />
        <Stat
          label="Training period"
          value={`${(period.start_utc ?? "—").slice(0, 10)} → ${(period.end_utc ?? "—").slice(0, 10)}`}
          sub="UTC · operator data"
        />
        <Stat label="Assets" value={assets} />
        <Stat label="Last retrain" value={(model?.trained_at_utc ?? "—").replace("T", " ").slice(0, 16)} sub="UTC" />
        <Stat label="Paper trades decided" value={String(decided)} sub={`${paper?.pending ?? 0} pending · ${paper?.unresolved ?? 0} unresolved`} />
        <Stat label="Paper win rate" value={paper?.win_rate === null || paper?.win_rate === undefined ? "—" : pct(paper.win_rate)} sub="from verified candles only" />
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <div className="label mb-1">Walk-forward validation (offline)</div>
          <KeyValue
            items={[
              ["accuracy", fmt(validation.accuracy)],
              ["macro F1", fmt(validation.macro_f1)],
              ["ROC-AUC", fmt(validation.roc_auc_macro)],
              ["directional win rate", validation.directional_win_rate !== undefined ? `${fmt(validation.directional_win_rate, 1)}%` : "—"],
            ]}
          />
        </div>
        <div>
          <div className="label mb-1">Untouched holdout (offline)</div>
          <KeyValue
            items={[
              ["accuracy", fmt(holdout.accuracy)],
              ["macro F1", fmt(holdout.macro_f1)],
              ["ROC-AUC", fmt(holdout.roc_auc_macro)],
              ["samples", holdout.samples !== undefined ? holdout.samples.toLocaleString() : "—"],
            ]}
          />
        </div>
      </div>

      <div className="mt-4">
        <div className="label mb-1">Research lab — model &amp; target quality (no ranking produced)</div>
        {research?.available ? (
          <>
            <KeyValue
              items={[
                ["experiments", String(research.summary?.experiment_count ?? "—")],
                ["cells with a qualifying gate", String(research.summary?.cells_with_a_selected_threshold ?? "—")],
                ["cells without evidence of edge", String(research.summary?.cells_without_evidence_of_edge ?? "—")],
                ["dataset version", String(research.dataset?.dataset_version ?? "—")],
                ["run", String(research.run_id ?? "—")],
                ["offline paper signals (all cells)", String(research.summary?.paper_signals_total ?? "—")],
              ]}
            />
            {research.experiments_with_evidence && research.experiments_with_evidence.length > 0 ? (
              <ul className="mt-2 space-y-1 text-[11px] text-ink-300">
                {research.experiments_with_evidence.slice(0, 6).map((cell) => (
                  <li key={cell.experiment_id} className="font-mono">
                    {cell.experiment_id} → gate {cell.selected_threshold} · holdout {cell.holdout_gate.signals} signals
                    {cell.holdout_gate.hit_rate !== null ? ` · hit ${(cell.holdout_gate.hit_rate * 100).toFixed(1)}%` : ""}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-[11px] text-ink-500">
                No confidence gate cleared the validation evidence bar, so the correct output of the running system
                remains NO TRADE.
              </p>
            )}
          </>
        ) : (
          <p className="text-[11px] text-ink-500">
            {research?.reason ?? "No research run has been executed yet — model quality is measured only on the validation/holdout numbers above."}
          </p>
        )}
      </div>

      <p className="mt-3 text-[10px] text-ink-500">
        Model confidence is a calculated score, never an accuracy claim. Offline validation is historical evaluation and
        is never combined with live paper results. Nothing on this panel is a forecast.
      </p>
    </Panel>
  );
}

function wilsonLowerBound(successes: number, total: number): number | null {
  if (!total || total <= 0) return null;
  const z = 1.96;
  const phat = successes / total;
  const denominator = 1 + (z * z) / total;
  const centre = phat + (z * z) / (2 * total);
  const margin = z * Math.sqrt((phat * (1 - phat) + (z * z) / (4 * total)) / total);
  return Math.max(0, (centre - margin) / denominator);
}

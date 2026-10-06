"use client";

import { Chip, Notice, Panel } from "./ui";
import { metricOrDash, pct } from "@/lib/format";
import type { PerformancePayload } from "@/lib/types";

export default function PerformancePanel({ data }: { data: PerformancePayload | null }) {
  const perf = data?.signal_performance;
  const validation = data?.model_validation;

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel
        title="Model validation (offline)"
        subtitle="Walk-forward and final-holdout evaluation of the trained models. No random shuffling; embargo ≥ horizon."
        right={<Chip tone={validation?.available ? "green" : "red"}>{validation?.available ? validation.version : "NOT TRAINED"}</Chip>}
      >
        {!validation?.available && (
          <Notice tone="red" title="Model not trained">
            No validation metrics exist yet. Train with{" "}
            <span className="font-mono">python -m backend.research.train</span>. Nothing is displayed that was not
            actually computed.
          </Notice>
        )}

        {validation?.available && (
          <>
            {validation.data_provenance?.is_simulated && (
              <Notice tone="amber" title="Trained on simulated data" className="mb-3">
                {String(validation.data_provenance?.notes ?? "")}
              </Notice>
            )}
            <div className="grid grid-cols-2 gap-2">
              <Metric label="walk-forward accuracy" value={metricOrDash(validation.out_of_fold?.accuracy, 4)} />
              <Metric label="walk-forward macro F1" value={metricOrDash(validation.out_of_fold?.macro_f1, 4)} />
              <Metric label="ROC-AUC (macro)" value={metricOrDash(validation.out_of_fold?.roc_auc_macro, 4)} />
              <Metric label="Brier score" value={metricOrDash(validation.out_of_fold?.brier, 4)} />
              <Metric
                label="calibration error"
                value={metricOrDash(validation.out_of_fold?.expected_calibration_error, 4)}
                hint="expected calibration error"
              />
              <Metric label="validation samples" value={String(validation.out_of_fold?.samples ?? "—")} />
              <Metric label="holdout accuracy" value={metricOrDash(validation.holdout?.accuracy, 4)} />
              <Metric label="holdout macro F1" value={metricOrDash(validation.holdout?.macro_f1, 4)} />
              <Metric label="holdout samples" value={String(validation.holdout?.samples ?? "—")} />
              <Metric
                label="max losing streak (oof)"
                value={String(validation.out_of_fold?.max_losing_streak ?? "—")}
                hint="consecutive losing directional calls"
              />
            </div>
            <dl className="mt-3 space-y-1 text-[11px] text-ink-500">
              <div className="flex justify-between gap-3">
                <dt>trained</dt>
                <dd className="font-mono text-ink-300">{validation.trained_at_utc ?? "—"}</dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt>data source</dt>
                <dd className="font-mono text-ink-300">{String(validation.data_provenance?.source ?? "—")}</dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt>rows</dt>
                <dd className="font-mono text-ink-300">{String(validation.data_provenance?.rows ?? "—")}</dd>
              </div>
            </dl>
            <p className="mt-2 text-[11px] leading-relaxed text-ink-500">{validation.note}</p>
          </>
        )}
      </Panel>

      <Panel
        title="Signal performance (this engine)"
        subtitle="Outcomes of signals actually emitted and resolved from verified candles."
        right={<Chip tone="blue">paper</Chip>}
      >
        {!perf && <p className="text-[12px] text-ink-500">Loading…</p>}
        {perf && perf.generated === 0 && (
          <p className="text-[12px] text-ink-500">
            No signals emitted yet — so there is no win rate to show, and none is invented.
          </p>
        )}
        {perf && perf.generated > 0 && (
          <div className="grid grid-cols-2 gap-2">
            <Metric label="signals generated" value={String(perf.generated)} />
            <Metric label="decided" value={String(perf.decided)} />
            <Metric label="wins" value={String(perf.wins)} />
            <Metric label="losses" value={String(perf.losses)} />
            <Metric label="win rate" value={perf.win_rate === null ? "—" : pct(perf.win_rate, 1)} hint="of decided signals only" />
            <Metric label="pending" value={String(perf.pending)} />
            <Metric label="voids" value={String(perf.voids)} />
            <Metric label="max losing streak" value={String(perf.max_losing_streak)} />
          </div>
        )}
        {data?.separation_note && (
          <Notice tone="blue" title="Why these two panels are separate" className="mt-3">
            {data.separation_note}
          </Notice>
        )}
      </Panel>
    </div>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="panel-tight px-3 py-2.5">
      <div className="label">{label}</div>
      <div className="mt-0.5 font-mono text-[15px] text-ink-100">{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-ink-500">{hint}</div>}
    </div>
  );
}

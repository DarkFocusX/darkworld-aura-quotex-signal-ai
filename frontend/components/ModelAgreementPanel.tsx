"use client";

import { Bar, Chip, Panel } from "./ui";
import { pct } from "@/lib/format";
import type { Ensemble, ModelStatus } from "@/lib/types";

export default function ModelAgreementPanel({
  ensemble,
  modelStatus,
}: {
  ensemble: Ensemble | null;
  modelStatus: ModelStatus | null;
}) {
  const votes = ensemble?.votes ?? [];
  const unusable = modelStatus?.unavailable ?? [];

  return (
    <Panel
      title="Model ensemble"
      subtitle="Each model outputs PUT / NEUTRAL / CALL probabilities. The ensemble is a documented weighted average — a neutral-heavy vote set cannot produce a signal."
      right={
        ensemble ? (
          <Chip tone={ensemble.agreement >= 0.6 ? "green" : "red"} title="Weighted share of models agreeing with the ensemble direction">
            agreement {pct(ensemble.agreement * 100, 0)}
          </Chip>
        ) : undefined
      }
    >
      {!ensemble && (
        <p className="text-[12px] text-ink-500">
          No ensemble result yet. Model status: <span className="font-mono">{modelStatus?.state ?? "—"}</span>
        </p>
      )}

      {ensemble && (
        <>
          <div className="mb-3 grid grid-cols-3 gap-2">
            {(["p_put", "p_neutral", "p_call"] as const).map((key) => (
              <div key={key} className="panel-tight px-3 py-2">
                <div className="label">{key.replace("p_", "").toUpperCase()}</div>
                <div
                  className={`font-mono text-lg ${
                    key === "p_call" ? "text-call" : key === "p_put" ? "text-put" : "text-ink-300"
                  }`}
                >
                  {pct(ensemble[key] * 100, 1)}
                </div>
              </div>
            ))}
          </div>

          <div className="space-y-2">
            {votes.map((vote) => (
              <div key={vote.model} className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[12px] text-ink-100">{vote.model}</span>
                  <span className="flex items-center gap-2">
                    <span className="text-[10px] text-ink-500">w {vote.weight.toFixed(2)}</span>
                    <span
                      className={`text-[11px] font-semibold ${
                        vote.status !== "OK"
                          ? "text-ink-500"
                          : vote.vote === "CALL"
                          ? "text-call"
                          : vote.vote === "PUT"
                          ? "text-put"
                          : "text-amber-300"
                      }`}
                    >
                      {vote.status !== "OK" ? vote.status : vote.vote}
                    </span>
                  </span>
                </div>
                <div className="mt-1.5 grid grid-cols-3 gap-1.5">
                  <Mini label="put" value={vote.p_put} tone="rose" />
                  <Mini label="neutral" value={vote.p_neutral} tone="slate" />
                  <Mini label="call" value={vote.p_call} tone="emerald" />
                </div>
                {vote.detail && vote.status !== "OK" && (
                  <div className="mt-1 text-[10px] text-ink-500">{vote.detail}</div>
                )}
              </div>
            ))}
          </div>

          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-500">
            <span>
              spread: <span className="font-mono text-ink-300">{ensemble.spread.toFixed(3)}</span>
            </span>
            <span>
              p(direction): <span className="font-mono text-ink-300">{pct(ensemble.direction_probability * 100, 1)}</span>
            </span>
            <span>
              normalised: <span className="font-mono text-ink-300">{pct(ensemble.directional_probability_normalised * 100, 1)}</span>
            </span>
            <span>
              anomaly veto: <span className="font-mono text-ink-300">{ensemble.anomaly_flag ? "YES" : "no"}</span>
            </span>
            {ensemble.temperature !== undefined && (
              <span>
                calibration T: <span className="font-mono text-ink-300">{ensemble.temperature.toFixed(3)}</span>
              </span>
            )}
          </div>
        </>
      )}

      {unusable.length > 0 && (
        <div className="mt-3 border-t border-white/5 pt-3">
          <div className="label mb-1">models not contributing</div>
          <ul className="space-y-1 text-[11px] text-ink-500">
            {unusable.map((item) => (
              <li key={item.model}>
                <span className="font-mono text-ink-300">{item.model}</span> — {item.detail}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}

function Mini({ label, value, tone }: { label: string; value: number; tone: "emerald" | "rose" | "slate" }) {
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-[9px] uppercase tracking-wider text-ink-500">{label}</span>
        <span className="font-mono text-[10px] text-ink-300">{(value * 100).toFixed(0)}%</span>
      </div>
      <Bar value={value * 100} tone={tone} height="h-1" />
    </div>
  );
}

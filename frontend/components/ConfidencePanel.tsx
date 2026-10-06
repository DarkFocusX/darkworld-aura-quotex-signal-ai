"use client";

import { Bar, Panel } from "./ui";
import { pct } from "@/lib/format";
import type { Components } from "@/lib/types";

const LABELS: Record<string, string> = {
  model: "Model probability",
  model_agreement: "Model agreement",
  trend: "Trend agreement",
  momentum: "Momentum agreement",
  candle: "Candle confirmation",
  structure: "Support / resistance",
  volatility: "Volatility suitability",
  regime: "Market regime",
  freshness: "Data freshness",
};

export default function ConfidencePanel({
  components,
  threshold,
}: {
  components: Components | null;
  threshold: number;
}) {
  if (!components) {
    return (
      <Panel title="Confidence engine" subtitle="Weighted score computed from nine independent components.">
        <p className="text-[12px] text-ink-500">
          Run a scan to see the component breakdown. The engine never displays a confidence value it did not compute.
        </p>
      </Panel>
    );
  }

  const entries = Object.entries(components.components || {});

  return (
    <Panel
      title="Confidence engine"
      subtitle={`Weighted sum of nine components · threshold ${pct(threshold, 0)} · tradable floor 85%`}
      right={
        <div className="text-right">
          <div className="label">total</div>
          <div className="font-mono text-lg text-ink-100">{pct(components.confidence, 1)}</div>
        </div>
      }
    >
      <ul className="space-y-2.5">
        {entries.map(([key, component]) => (
          <li key={key}>
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <span className="text-[12px] text-ink-300">
                {LABELS[key] ?? key}
                <span className="ml-1.5 text-[10px] text-ink-500">
                  w {component.weight.toFixed(2)} · {component.aligned ? "aligned" : "opposing"}
                </span>
              </span>
              <span className="font-mono text-[12px] text-ink-100">
                {component.score.toFixed(0)}
                <span className="ml-1 text-[10px] text-ink-500">(+{component.contribution.toFixed(1)})</span>
              </span>
            </div>
            <Bar
              value={component.score}
              tone={component.score >= 75 ? "emerald" : component.score >= 50 ? "amber" : "rose"}
            />
          </li>
        ))}
      </ul>

      {components.notes && components.notes.length > 0 && (
        <div className="mt-3 border-t border-white/5 pt-3">
          <div className="label mb-1">weakest links</div>
          <ul className="list-inside list-disc space-y-0.5 text-[11px] text-amber-300/90">
            {components.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-3 text-[11px] leading-relaxed text-ink-500">
        Uncertainty is never converted into confidence: a neutral-heavy ensemble lowers the model score and pushes the
        total below the threshold, which produces NO TRADE.
      </p>
    </Panel>
  );
}

"use client";

import { Panel } from "./ui";
import type { ChecksBundle } from "@/lib/types";

export default function ChecksPanel({ checks }: { checks: ChecksBundle | null }) {
  return (
    <Panel
      title="Signal validation"
      subtitle="Six gates run before anything is emitted. A single blocking failure means NO TRADE."
      right={
        checks ? (
          <span className={`chip ${checks.passed ? "border-emerald-400/30 bg-emerald-500/10 text-emerald-300" : "border-rose-400/30 bg-rose-500/10 text-rose-300"}`}>
            {checks.passed ? "ALL PASSED" : `${checks.blocking.length} BLOCKING`}
          </span>
        ) : undefined
      }
    >
      {!checks && <p className="text-[12px] text-ink-500">Run a scan to execute the validation suite.</p>}

      {checks && (
        <>
          <ul className="space-y-1.5">
            {checks.checks.map((check) => (
              <li
                key={check.check}
                className={`rounded-lg border px-3 py-2 text-[12px] ${
                  check.passed ? "border-emerald-500/15 bg-emerald-500/[0.04]" : "border-rose-500/25 bg-rose-500/[0.06]"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <span className="flex items-center gap-2 font-medium text-ink-100">
                    <span>{check.passed ? "✅" : "⛔"}</span>
                    {check.check}
                  </span>
                  <span className="shrink-0 text-[10px] uppercase tracking-wider text-ink-500">{check.severity}</span>
                </div>
                <p className="mt-1 text-[11px] leading-relaxed text-ink-300">{check.detail}</p>
              </li>
            ))}
          </ul>

          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {checks.execution && (
              <div className="panel-tight px-3 py-2">
                <div className="label">execution</div>
                <div className="mt-0.5 font-mono text-[12px] text-ink-100">
                  {checks.execution.adapter} · {checks.execution.status}
                </div>
                <p className="mt-0.5 text-[10px] text-ink-500">{checks.execution.detail}</p>
              </div>
            )}
            {checks.telegram && (
              <div className="panel-tight px-3 py-2">
                <div className="label">telegram</div>
                <div className="mt-0.5 font-mono text-[12px] text-ink-100">{checks.telegram.status}</div>
                <p className="mt-0.5 text-[10px] text-ink-500">{checks.telegram.detail}</p>
              </div>
            )}
          </div>
        </>
      )}
    </Panel>
  );
}

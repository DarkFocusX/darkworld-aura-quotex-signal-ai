"use client";

import { Panel } from "./ui";
import { directionArrow, pct, resultBadgeClass } from "@/lib/format";
import type { HistoryRow } from "@/lib/types";

export default function HistoryTable({ rows }: { rows: HistoryRow[] }) {
  return (
    <Panel
      title="Signal history"
      subtitle="WIN / LOSS is only recorded once the verified closing candle exists in the feed. Until then the row stays PENDING."
      right={<span className="chip border-white/10 bg-white/5 text-ink-300">{rows.length} rows</span>}
    >
      {rows.length === 0 ? (
        <p className="text-[12px] text-ink-500">No signals generated yet.</p>
      ) : (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[640px] border-collapse text-left text-[12px]">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-ink-500">
                <th className="px-2 py-2 font-medium">Time (IST)</th>
                <th className="px-2 py-2 font-medium">Asset</th>
                <th className="px-2 py-2 font-medium">Direction</th>
                <th className="px-2 py-2 font-medium">Confidence</th>
                <th className="px-2 py-2 font-medium">Result</th>
                <th className="px-2 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.signal_uid} className="border-t border-white/5 hover:bg-white/[0.02]">
                  <td className="whitespace-nowrap px-2 py-2 font-mono text-ink-100">{row.time_ist}</td>
                  <td className="whitespace-nowrap px-2 py-2 font-mono text-ink-100">
                    {row.asset}
                    {row.is_simulated && <span className="ml-1 text-[9px] text-amber-300/80">SIM</span>}
                  </td>
                  <td className={`whitespace-nowrap px-2 py-2 font-mono ${row.direction === "PUT" ? "text-put" : "text-call"}`}>
                    {directionArrow(row.direction)} {row.direction}
                  </td>
                  <td className="whitespace-nowrap px-2 py-2 font-mono text-ink-100">{pct(row.confidence, 1)}</td>
                  <td className="whitespace-nowrap px-2 py-2">
                    <span className={`chip ${resultBadgeClass(row.result)}`}>{row.result}</span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-2 text-ink-300" title={row.result_reason ?? undefined}>
                    {row.status}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

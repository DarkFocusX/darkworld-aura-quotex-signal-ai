"use client";

import { Panel } from "./ui";
import { pct } from "@/lib/format";
import type { ScanRow } from "@/lib/types";

export default function ScanLogPanel({ scans }: { scans: ScanRow[] }) {
  return (
    <Panel
      title="Recent scans (transparency log)"
      subtitle="Every scan is recorded — including the rejected ones — so you can audit exactly why a signal was withheld."
    >
      {scans.length === 0 ? (
        <p className="text-[12px] text-ink-500">No scans recorded yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {scans.map((scan) => {
            const tone =
              scan.status === "SIGNAL"
                ? "border-emerald-500/20 bg-emerald-500/[0.05]"
                : scan.status === "NO_TRADE"
                ? "border-amber-500/20 bg-amber-500/[0.04]"
                : "border-white/5 bg-white/[0.02]";
            return (
              <li key={scan.id} className={`rounded-lg border px-3 py-2 ${tone}`}>
                <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
                  <span className="flex items-center gap-2">
                    <span className="font-mono text-ink-100">{scan.scanned_at_ist}</span>
                    <span className="font-mono text-ink-300">{scan.asset ?? "—"}</span>
                    <span className="font-semibold text-ink-100">{scan.status}</span>
                  </span>
                  <span className="flex items-center gap-2 text-ink-500">
                    {scan.confidence !== null && scan.confidence !== undefined && (
                      <span className="font-mono">{pct(scan.confidence, 1)}</span>
                    )}
                    {scan.is_simulated && scan.is_simulated !== 0 ? <span className="text-amber-300/80">SIM</span> : null}
                    <span>{scan.scan_mode ?? "NOW"}</span>
                  </span>
                </div>
                {scan.reason && <p className="mt-1 text-[11px] leading-relaxed text-ink-500">{scan.reason}</p>}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

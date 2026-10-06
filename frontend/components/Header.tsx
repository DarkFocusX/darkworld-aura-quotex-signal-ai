"use client";

import { Chip } from "./ui";
import type { SystemStatus } from "@/lib/types";

export default function Header({
  system,
  istClock,
  istDate,
  online,
}: {
  system: SystemStatus | null;
  istClock: string;
  istDate: string;
  online: boolean;
}) {
  const model = system?.model;
  const data = system?.data;
  const modelTone =
    !model ? "slate"
      : model.state === "READY_MARKET" ? "green"
      : model.state === "READY_DEMO" ? "amber" : "red";
  const dataTone =
    !data ? "slate" : data.status === "OK" ? "green" : data.status === "PARTIAL" ? "amber" : data.status === "UNAVAILABLE" ? "red" : "amber";
  const realCount = data?.real_market_count ?? 0;
  const dataBadge =
    !data ? "DATA —"
      : realCount > 0 && realCount === (data.available_count ?? 0) ? "REAL DATA"
      : (data.available_count && data.simulated_count === data.available_count) ? "SIMULATED"
      : data.available_count ? "PARTIAL" : "DATA UNAVAILABLE";
  const badgeTone = dataBadge === "REAL DATA" ? "green" : dataBadge === "DATA UNAVAILABLE" ? "red" : "amber";

  return (
    <header className="panel sticky top-0 z-30 mb-4 px-4 py-3 sm:px-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="relative grid h-10 w-10 place-items-center rounded-xl border border-emerald-400/30 bg-gradient-to-br from-emerald-500/20 to-sky-500/10">
            <span className="font-mono text-[15px] font-bold text-emerald-300">DA</span>
          </div>
          <div className="leading-tight">
            <div className="text-[13px] font-bold tracking-[0.18em] text-ink-100">DARKWORLD AURA</div>
            <div className="text-[11px] text-ink-500">
              Quotex Signal AI <span className="text-ink-500/70">· M1 engine</span>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Chip tone={online ? "green" : "red"} title={online ? "Backend reachable" : "Backend unreachable"}>
            <span className={`h-1.5 w-1.5 rounded-full ${online ? "bg-emerald-400" : "bg-rose-400"}`} />
            {online ? "LIVE" : "OFFLINE"}
          </Chip>
          <Chip tone="violet" title="No real money execution is enabled in this build">
            🧪 {system?.mode === "SIGNAL_ONLY" ? "SIGNAL ONLY" : "PAPER MODE"}
          </Chip>
          <Chip tone={modelTone} title={model?.message}>
            🤖 {model?.state ?? "MODEL —"}
          </Chip>
          <Chip
            tone={badgeTone}
            title={
              (data?.message ?? "") +
              (model?.state === "READY_DEMO"
                ? " · model is READY_DEMO — the production policy blocks signals from demo data/models."
                : "")
            }
          >
            📡 {dataBadge}
          </Chip>
          <Chip tone={dataTone} title={data?.message}>
            feed {data?.status ?? "—"}
          </Chip>
          <Chip tone="blue" title="All signalling times use the Asia/Kolkata timezone">
            🕐 IST {istClock}
          </Chip>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-500">
        <span>{istDate}</span>
        <span className="text-ink-500/60">timezone Asia/Kolkata</span>
        {system?.scheduler?.running && <span>scheduler: every M1 candle</span>}
        {system?.telegram && (
          <span>
            telegram: {system.telegram.enabled ? (system.telegram.configured ? "enabled" : "enabled (not configured)") : "disabled"}
          </span>
        )}
        {system?.data && (
          <span>
            data: {system.data.available_count}/{system.data.total_count} assets
            {system.data.real_market_count ? ` · ${system.data.real_market_count} real` : ""}
            {system.data.simulated_count > 0 ? ` · ${system.data.simulated_count} simulated` : ""}
            {system.data.environment ? ` · ${system.data.environment}` : ""}
          </span>
        )}
      </div>
    </header>
  );
}

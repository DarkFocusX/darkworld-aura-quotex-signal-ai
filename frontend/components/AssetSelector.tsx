"use client";

import type { AssetInfo } from "@/lib/types";

export default function AssetSelector({
  assets,
  selected,
  onSelect,
  disabled,
  autoSelect,
}: {
  assets: AssetInfo[];
  selected: string;
  onSelect: (symbol: string) => void;
  disabled?: boolean;
  autoSelect: boolean;
}) {
  return (
    <div className="panel px-3 py-3 sm:px-4">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="label">asset selector</span>
        <span className="text-[10px] text-ink-500">
          AUTO SELECT scans every enabled asset and takes the highest scoring qualified setup
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onSelect("AUTO")}
          className={`rounded-lg border px-3 py-2 text-[12px] font-semibold transition disabled:opacity-50
            ${selected === "AUTO"
              ? "border-emerald-400/40 bg-emerald-500/10 text-emerald-300"
              : "border-white/10 bg-white/5 text-ink-300 hover:bg-white/10"}`}
        >
          🌐 AUTO SELECT {autoSelect ? "" : "(off)"}
        </button>
        {assets.map((asset) => {
          const active = selected === asset.symbol;
          return (
            <button
              key={asset.symbol}
              type="button"
              disabled={disabled}
              onClick={() => onSelect(asset.symbol)}
              title={asset.notes || undefined}
              className={`rounded-lg border px-3 py-2 text-[12px] font-medium transition disabled:opacity-50
                ${active
                  ? "border-emerald-400/40 bg-emerald-500/10 text-emerald-300"
                  : "border-white/10 bg-white/5 text-ink-300 hover:bg-white/10"}`}
            >
              {asset.flag} {asset.symbol}
              {asset.kind === "otc" && <span className="ml-1 text-[9px] text-ink-500">OTC</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

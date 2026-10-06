/** Display helpers. NOTE: all trading timestamps arrive already formatted in IST by
 *  the backend — the browser timezone is never used for trading calculations. */

export const IST_LABEL = "Asia/Kolkata";

export function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

export function countdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}

export function secondsToClock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${Number(value).toFixed(digits)}%`;
}

export function ratio(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(Number(value) * 100).toFixed(0)}%`;
}

export function price(value: number | null | undefined, decimals = 5): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toFixed(decimals);
}

export function clockFromSeconds(totalSeconds: number, baseIso?: string): string {
  if (!baseIso) return countdown(totalSeconds);
  const base = new Date(baseIso);
  const target = new Date(base.getTime() + totalSeconds * 1000);
  return target.toLocaleTimeString("en-GB", { timeZone: IST_LABEL, hour12: false });
}

export function qualityColor(quality?: string | null): string {
  switch ((quality || "").toUpperCase()) {
    case "VERY_HIGH":
      return "text-emerald-300";
    case "HIGH":
      return "text-emerald-400";
    case "MODERATE":
      return "text-amber-300";
    default:
      return "text-slate-400";
  }
}

export function qualityLabel(quality?: string | null): string {
  switch ((quality || "").toUpperCase()) {
    case "VERY_HIGH":
      return "VERY HIGH";
    case "HIGH":
      return "HIGH CONFIDENCE";
    case "MODERATE":
      return "MODERATE";
    case "NO_TRADE":
      return "NO TRADE";
    default:
      return "—";
  }
}

export function qualityBadgeClass(quality?: string | null): string {
  switch ((quality || "").toUpperCase()) {
    case "VERY_HIGH":
      return "border-emerald-400/40 bg-emerald-500/10 text-emerald-300";
    case "HIGH":
      return "border-emerald-400/30 bg-emerald-500/5 text-emerald-400";
    case "MODERATE":
      return "border-amber-400/30 bg-amber-500/10 text-amber-300";
    default:
      return "border-slate-600/40 bg-slate-700/20 text-slate-300";
  }
}

export function resultBadgeClass(result?: string | null): string {
  switch ((result || "").toUpperCase()) {
    case "WIN":
      return "border-emerald-400/40 bg-emerald-500/10 text-emerald-300";
    case "LOSS":
      return "border-rose-400/40 bg-rose-500/10 text-rose-300";
    case "VOID":
      return "border-slate-500/40 bg-slate-600/10 text-slate-300";
    default:
      return "border-amber-400/30 bg-amber-500/10 text-amber-300";
  }
}

export function directionClass(direction?: string | null): string {
  return (direction || "").toUpperCase() === "PUT" ? "text-rose-400" : "text-emerald-400";
}

export function directionArrow(direction?: string | null): string {
  return (direction || "").toUpperCase() === "PUT" ? "🔻" : "🔺";
}

export function relativeTime(iso?: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Math.max(0, Date.now() - then) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function metricOrDash(value: unknown, digits = 4): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return value.toFixed(digits);
  return String(value);
}

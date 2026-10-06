"use client";

import React from "react";

export function Panel({
  title,
  subtitle,
  right,
  children,
  className = "",
  id,
}: {
  title?: string;
  subtitle?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section id={id} className={`panel p-4 sm:p-5 ${className}`}>
      {(title || right) && (
        <header className="mb-3 flex items-start justify-between gap-3">
          <div>
            {title && <h2 className="text-sm font-semibold tracking-wide text-ink-100">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-[11px] leading-relaxed text-ink-500">{subtitle}</p>}
          </div>
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function Chip({
  children,
  tone = "slate",
  className = "",
  title,
}: {
  children: React.ReactNode;
  tone?: "slate" | "green" | "red" | "amber" | "blue" | "violet";
  className?: string;
  title?: string;
}) {
  const tones: Record<string, string> = {
    slate: "border-white/10 bg-white/5 text-ink-300",
    green: "border-emerald-400/30 bg-emerald-500/10 text-emerald-300",
    red: "border-rose-400/30 bg-rose-500/10 text-rose-300",
    amber: "border-amber-400/30 bg-amber-500/10 text-amber-300",
    blue: "border-sky-400/30 bg-sky-500/10 text-sky-300",
    violet: "border-violet-400/30 bg-violet-500/10 text-violet-300",
  };
  return (
    <span title={title} className={`chip ${tones[tone]} ${className}`}>
      {children}
    </span>
  );
}

export function Stat({
  label,
  value,
  sub,
  className = "",
  mono = true,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  className?: string;
  mono?: boolean;
}) {
  return (
    <div className={`panel-tight px-3 py-2.5 ${className}`}>
      <div className="label">{label}</div>
      <div className={`mt-1 text-[15px] text-ink-100 ${mono ? "font-mono" : "font-semibold"}`}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-ink-500">{sub}</div>}
    </div>
  );
}

export function Bar({
  value,
  max = 100,
  tone = "emerald",
  height = "h-1.5",
}: {
  value: number;
  max?: number;
  tone?: "emerald" | "rose" | "amber" | "sky" | "slate";
  height?: string;
}) {
  const tones: Record<string, string> = {
    emerald: "bg-emerald-400",
    rose: "bg-rose-400",
    amber: "bg-amber-400",
    sky: "bg-sky-400",
    slate: "bg-slate-500",
  };
  const width = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className={`w-full overflow-hidden rounded-full bg-white/5 ${height}`}>
      <div
        className={`h-full rounded-full ${tones[tone]} transition-[width] duration-500 ease-out`}
        style={{ width: `${width}%` }}
      />
    </div>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={`h-4 w-4 animate-spin ${className}`} aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
    </svg>
  );
}

export function Notice({
  tone = "slate",
  title,
  children,
  className = "",
}: {
  tone?: "slate" | "red" | "amber" | "green" | "blue";
  title?: string;
  children: React.ReactNode;
  className?: string;
}) {
  const tones: Record<string, string> = {
    slate: "border-white/10 bg-white/5",
    red: "border-rose-500/30 bg-rose-500/10",
    amber: "border-amber-500/30 bg-amber-500/10",
    green: "border-emerald-500/30 bg-emerald-500/10",
    blue: "border-sky-500/30 bg-sky-500/10",
  };
  return (
    <div className={`rounded-xl border px-3.5 py-3 text-[12px] leading-relaxed ${tones[tone]} ${className}`}>
      {title && <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider">{title}</div>}
      <div className="text-ink-300">{children}</div>
    </div>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  hint?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`flex w-full items-center justify-between gap-3 rounded-xl border px-3 py-2.5 text-left transition
        ${checked ? "border-emerald-400/30 bg-emerald-500/5" : "border-white/10 bg-white/5"}
        ${disabled ? "opacity-50" : "hover:bg-white/10"}`}
    >
      <span>
        <span className="block text-[13px] font-medium text-ink-100">{label}</span>
        {hint && <span className="mt-0.5 block text-[11px] text-ink-500">{hint}</span>}
      </span>
      <span
        className={`relative h-5 w-9 shrink-0 rounded-full transition ${checked ? "bg-emerald-400" : "bg-slate-600"}`}
      >
        <span
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${checked ? "left-[18px]" : "left-0.5"}`}
        />
      </span>
    </button>
  );
}

export function KeyValue({ items, columns = 2 }: { items: [string, React.ReactNode][]; columns?: number }) {
  return (
    <dl className={`grid gap-x-4 gap-y-2 ${columns === 2 ? "grid-cols-2" : "grid-cols-1"}`}>
      {items.map(([key, value]) => (
        <div key={key} className="min-w-0">
          <dt className="label">{key}</dt>
          <dd className="truncate font-mono text-[13px] text-ink-100">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

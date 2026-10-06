"use client";

import { useEffect, useState } from "react";
import { Notice, Panel, Toggle } from "./ui";
import { api, ApiError } from "@/lib/api";
import type { AssetInfo, ConfigPayload, DataImportResponse } from "@/lib/types";

export default function SettingsPanel({
  config,
  assets,
  onSave,
  onReloadModel,
  onEvaluate,
  onImported,
  saving,
  message,
}: {
  config: ConfigPayload | null;
  assets: AssetInfo[];
  onSave: (patch: Record<string, unknown>) => Promise<void>;
  onReloadModel: () => Promise<void>;
  onEvaluate: () => Promise<void>;
  onImported?: () => Promise<void> | void;
  saving: boolean;
  message: string | null;
}) {
  const [minConfidence, setMinConfidence] = useState(85);
  const [expiry, setExpiry] = useState(1);
  const [mode, setMode] = useState("PAPER");
  const [dataMode, setDataMode] = useState("auto");
  const [telegramEnabled, setTelegramEnabled] = useState(false);
  const [telegramSimulated, setTelegramSimulated] = useState(false);
  const [autoSelect, setAutoSelect] = useState(true);
  const [dailyMode, setDailyMode] = useState(true);
  const [scheduler, setScheduler] = useState(true);
  const [evaluateEnabled, setEvaluateEnabled] = useState(true);
  const [enabled, setEnabled] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [environment, setEnvironment] = useState("production");
  const [agreement, setAgreement] = useState(0.6);
  const [importAsset, setImportAsset] = useState("");
  const [importClass, setImportClass] = useState("MARKET");
  const [importText, setImportText] = useState("");
  const [importForce, setImportForce] = useState(false);
  const [importResult, setImportResult] = useState<DataImportResponse | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [importBusy, setImportBusy] = useState(false);

  useEffect(() => {
    if (!config) return;
    const s = config.settings;
    setMinConfidence(s.min_confidence);
    setExpiry(s.expiry_minutes);
    setMode(s.mode);
    setDataMode(s.data_source_mode);
    setTelegramEnabled(s.telegram_enabled);
    setTelegramSimulated(s.telegram_allow_simulated);
    setAutoSelect(s.auto_select);
    setDailyMode(s.daily_mode);
    setScheduler(s.scheduler_enabled);
    setEvaluateEnabled(s.outcome_evaluation_enabled);
    setEnabled(s.enabled_assets);
    if (s.environment) setEnvironment(s.environment);
    if (typeof s.min_model_agreement === "number") setAgreement(s.min_model_agreement);
  }, [config]);

  useEffect(() => {
    if (!importAsset && assets.length > 0) setImportAsset(assets[0].symbol);
  }, [assets, importAsset]);

  const runImport = async (dryRun: boolean, fromFile = false) => {
    setImportBusy(true);
    setImportError(null);
    try {
      const payload = await api.importCsv({
        asset: importAsset,
        source_class: importClass,
        dry_run: dryRun,
        force: importForce,
        ...(fromFile ? { from_file: true } : { csv_text: importText }),
      });
      setImportResult(payload);
      if (onImported && !dryRun) await onImported();
    } catch (err) {
      setImportError(err instanceof ApiError ? err.message : "import failed");
    } finally {
      setImportBusy(false);
    }
  };

  const pickFile = (file: File | undefined) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setImportText(String(reader.result ?? ""));
    reader.readAsText(file);
  };

  const submit = async (patch: Record<string, unknown>) => {
    setBusy(true);
    try {
      await onSave(patch);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title="Settings"
      subtitle="Signal policy lives here. Secrets are read from environment variables only — never from this panel."
      right={message ? <span className="chip border-emerald-400/30 bg-emerald-500/10 text-emerald-300">{message}</span> : undefined}
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-3">
          <div>
            <div className="mb-1 flex items-baseline justify-between">
              <span className="text-[12px] text-ink-300">Confidence threshold</span>
              <span className="font-mono text-[13px] text-ink-100">{minConfidence.toFixed(0)}%</span>
            </div>
            <input
              type="range"
              min={50}
              max={99}
              step={1}
              value={minConfidence}
              onChange={(event) => setMinConfidence(Number(event.target.value))}
              onMouseUp={() => submit({ min_confidence: minConfidence })}
              onTouchEnd={() => submit({ min_confidence: minConfidence })}
              className="w-full accent-emerald-400"
            />
            <p className="mt-1 text-[11px] text-ink-500">
              Only ≥ 85% is tradable (VERY HIGH ≥ 90, HIGH ≥ 85, MODERATE 75–84, below 75 rejected).
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="label">expiry</span>
              <select
                value={expiry}
                onChange={(event) => {
                  const value = Number(event.target.value);
                  setExpiry(value);
                  submit({ expiry_minutes: value });
                }}
                className="mt-1 w-full rounded-lg border border-white/10 bg-base-700 px-3 py-2 text-[13px] text-ink-100"
              >
                {[1, 2, 3, 4, 5].map((minute) => (
                  <option key={minute} value={minute}>
                    {minute} minute{minute > 1 ? "s" : ""}
                  </option>
                ))}
              </select>
            </label>

            <label className="block">
              <span className="label">mode</span>
              <select
                value={mode}
                onChange={(event) => {
                  setMode(event.target.value);
                  submit({ mode: event.target.value });
                }}
                className="mt-1 w-full rounded-lg border border-white/10 bg-base-700 px-3 py-2 text-[13px] text-ink-100"
              >
                <option value="PAPER">Paper trading</option>
                <option value="SIGNAL_ONLY">Signal only</option>
                <option value="LIVE" disabled>
                  Live execution (not implemented)
                </option>
              </select>
            </label>

            <label className="block">
              <span className="label">data source</span>
              <select
                value={dataMode}
                onChange={(event) => {
                  setDataMode(event.target.value);
                  submit({ data_source_mode: event.target.value });
                }}
                className="mt-1 w-full rounded-lg border border-white/10 bg-base-700 px-3 py-2 text-[13px] text-ink-100"
              >
                <option value="auto">Auto (csv → broker → public → simulated)</option>
                <option value="live_only">Live only (no synthetic fallback)</option>
                <option value="csv">CSV files only (operator import)</option>
                <option value="real_only">Real only (no simulated fallback)</option>
                <option value="simulated">Simulated demo only</option>
              </select>
            </label>

            <label className="block">
              <span className="label">environment</span>
              <select
                value={environment}
                onChange={(event) => {
                  setEnvironment(event.target.value);
                  submit({ environment: event.target.value });
                }}
                className="mt-1 w-full rounded-lg border border-white/10 bg-base-700 px-3 py-2 text-[13px] text-ink-100"
              >
                <option value="production">production (real data + READY_MARKET required)</option>
                <option value="development">development (simulated allowed, labelled)</option>
              </select>
              <p className="mt-0.5 text-[10px] text-ink-500">
                Production refuses simulated feeds and demo models.
              </p>
            </label>

            <label className="block">
              <span className="label">minimum model agreement</span>
              <input
                type="number"
                min={0.5}
                max={1}
                step={0.05}
                value={agreement}
                onChange={(event) => setAgreement(Number(event.target.value))}
                onBlur={() => submit({ min_model_agreement: agreement })}
                className="mt-1 w-full rounded-lg border border-white/10 bg-base-700 px-3 py-2 font-mono text-[13px] text-ink-100"
              />
              <p className="mt-0.5 text-[10px] text-ink-500">
                How much the ensemble must agree before a CALL/PUT is emitted (default 0.60).
              </p>
            </label>

            <div className="panel-tight px-3 py-2">
              <div className="label">timezone</div>
              <div className="mt-1 font-mono text-[13px] text-ink-100">Asia/Kolkata (IST)</div>
              <p className="mt-0.5 text-[10px] text-ink-500">Browser timezone is never used.</p>
            </div>
          </div>

          <div className="grid gap-2 sm:grid-cols-2">
            <Toggle
              checked={autoSelect}
              onChange={(value) => {
                setAutoSelect(value);
                submit({ auto_select: value });
              }}
              label="Auto asset selection"
              hint="Scan all enabled assets, keep the best"
            />
            <Toggle
              checked={dailyMode}
              onChange={(value) => {
                setDailyMode(value);
                submit({ daily_mode: value });
              }}
              label="Daily signal mode"
              hint="Best setup of the IST trading day"
            />
            <Toggle
              checked={scheduler}
              onChange={(value) => {
                setScheduler(value);
                submit({ scheduler_enabled: value });
              }}
              label="Background scheduler"
              hint="Scan + evaluate every M1 candle"
            />
            <Toggle
              checked={evaluateEnabled}
              onChange={(value) => {
                setEvaluateEnabled(value);
                submit({ outcome_evaluation_enabled: value });
              }}
              label="Outcome evaluation"
              hint="Resolve WIN/LOSS from verified candles"
            />
          </div>
        </div>

        <div className="space-y-3">
          <div className="space-y-2">
            <Toggle
              checked={telegramEnabled}
              onChange={(value) => {
                setTelegramEnabled(value);
                submit({ telegram_enabled: value });
              }}
              label="Telegram notifications"
              hint={
                config?.telegram.configured
                  ? "Credentials detected in the environment"
                  : "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env to enable delivery"
              }
            />
            <Toggle
              checked={telegramSimulated}
              onChange={(value) => {
                setTelegramSimulated(value);
                submit({ telegram_allow_simulated: value });
              }}
              label="Send simulated-feed signals too"
              hint="Off by default — demo signals are study material"
            />
            <div className="panel-tight px-3 py-2 text-[11px] text-ink-500">
              token: <span className="font-mono text-ink-300">{config?.telegram.token_set ? "set" : "missing"}</span> · chat:
              <span className="font-mono text-ink-300"> {config?.telegram.chat_id_set ? "set" : "missing"}</span>
            </div>
          </div>

          <div>
            <div className="label mb-1.5">enabled assets ({enabled.length})</div>
            <div className="flex flex-wrap gap-1.5">
              {assets.map((asset) => {
                const active = enabled.includes(asset.symbol);
                return (
                  <button
                    key={asset.symbol}
                    type="button"
                    onClick={() => {
                      const next = active ? enabled.filter((s) => s !== asset.symbol) : [...enabled, asset.symbol];
                      if (next.length === 0) return;
                      setEnabled(next);
                      submit({ enabled_assets: next });
                    }}
                    className={`rounded-lg border px-2.5 py-1.5 text-[11px] transition ${
                      active
                        ? "border-emerald-400/40 bg-emerald-500/10 text-emerald-300"
                        : "border-white/10 bg-white/5 text-ink-500 hover:bg-white/10"
                    }`}
                  >
                    {asset.flag} {asset.symbol}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="panel-tight space-y-2 p-3">
            <div className="flex items-baseline justify-between">
              <span className="text-[12px] font-semibold text-ink-100">Operator data import</span>
              <span className="text-[10px] text-ink-500">CSV: time,open,high,low,close,volume</span>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="block">
                <span className="label">asset</span>
                <select
                  value={importAsset}
                  onChange={(event) => setImportAsset(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-white/10 bg-base-700 px-3 py-2 text-[12px] text-ink-100"
                >
                  {assets.map((asset) => (
                    <option key={asset.symbol} value={asset.symbol}>
                      {asset.symbol}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="label">data class</span>
                <select
                  value={importClass}
                  onChange={(event) => setImportClass(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-white/10 bg-base-700 px-3 py-2 text-[12px] text-ink-100"
                >
                  <option value="MARKET">MARKET — real operator data</option>
                  <option value="SIMULATED">SIMULATED — synthetic demo</option>
                  <option value="FIXTURE">FIXTURE — test only</option>
                </select>
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <label className="btn-ghost cursor-pointer !text-[11px]">
                choose file
                <input
                  type="file"
                  accept=".csv,text/csv"
                  className="hidden"
                  onChange={(event) => pickFile(event.target.files?.[0])}
                />
              </label>
              <button
                type="button"
                disabled={importBusy}
                onClick={() => runImport(false, true)}
                className="btn-ghost !text-[11px]"
                title="Register a file that already exists in backend/data/csv"
              >
                register existing file
              </button>
              <label className="flex items-center gap-1.5 text-[11px] text-ink-500">
                <input
                  type="checkbox"
                  checked={importForce}
                  onChange={(event) => setImportForce(event.target.checked)}
                  className="accent-emerald-400"
                />
                force overwrite
              </label>
            </div>
            <textarea
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
              placeholder="…or paste CSV rows here (header row expected)"
              rows={4}
              className="w-full rounded-lg border border-white/10 bg-base-800 px-3 py-2 font-mono text-[11px] text-ink-100"
            />
            <div className="flex flex-wrap gap-2">
              <button type="button" disabled={importBusy} onClick={() => runImport(true)} className="btn-ghost !text-[11px]">
                validate only (dry run)
              </button>
              <button
                type="button"
                disabled={importBusy || !importText.trim()}
                onClick={() => runImport(false)}
                className="btn-ghost !text-[11px]"
              >
                import + register
              </button>
            </div>
            {importError && <p className="text-[11px] text-rose-300">{importError}</p>}
            {importResult && (
              <div className="text-[11px]">
                <span
                  className={
                    importResult.status === "IMPORTED" || importResult.status === "VALIDATED"
                      ? "chip border-emerald-400/30 bg-emerald-500/10 text-emerald-300"
                      : "chip border-rose-400/30 bg-rose-500/10 text-rose-300"
                  }
                >
                  {importResult.status}
                </span>
                {importResult.reason && <span className="ml-2 text-ink-300">{importResult.reason}</span>}
                {importResult.validation && (
                  <div className="mt-1 font-mono text-[10px] text-ink-500">
                    rows {importResult.validation.rows} · {importResult.validation.status} ·{" "}
                    {importResult.validation.interval_minutes ?? "—"} min · missing {importResult.validation.missing_candles} ·
                    flat {((importResult.validation.flat_ratio ?? 0) * 100).toFixed(1)}%
                  </div>
                )}
                {importResult.validation?.errors?.slice(0, 3).map((message) => (
                  <div key={message} className="mt-1 text-[10px] text-rose-300">
                    {message}
                  </div>
                ))}
              </div>
            )}
            <p className="text-[10px] text-ink-500">
              Imported data is stored as validated M1 candles in <span className="font-mono">backend/data/csv</span>, kept in
              UTC and displayed in IST. A dataset that fails validation is registered but never fed to the model.
            </p>
          </div>

          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={() => onReloadModel()} disabled={busy} className="btn-ghost !text-[12px]">
              reload model artifacts
            </button>
            <button type="button" onClick={() => onEvaluate()} disabled={busy} className="btn-ghost !text-[12px]">
              evaluate open signals
            </button>
          </div>

          {(saving || busy) && <p className="text-[11px] text-ink-500">saving…</p>}

          <Notice tone="blue" title="Anti-fabrication rules enforced by the backend">
            No random outcomes, no hard-coded confidence, no synthetic prices for live assets. If data or models are
            unavailable the engine reports it instead of producing a signal.
          </Notice>
        </div>
      </div>
    </Panel>
  );
}

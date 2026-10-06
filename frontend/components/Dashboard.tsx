"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import AssetSelector from "./AssetSelector";
import ChecksPanel from "./ChecksPanel";
import DataProvenancePanel from "./DataProvenancePanel";
import LivePaperPanel from "./LivePaperPanel";
import ModelQualityPanel from "./ModelQualityPanel";
import ChartPanel from "./ChartPanel";
import ConfidencePanel from "./ConfidencePanel";
import DailyPanel from "./DailyPanel";
import Header from "./Header";
import HistoryTable from "./HistoryTable";
import MarketConditionsPanel from "./MarketConditionsPanel";
import ModelAgreementPanel from "./ModelAgreementPanel";
import PerformancePanel from "./PerformancePanel";
import ScanLogPanel from "./ScanLogPanel";
import SettingsPanel from "./SettingsPanel";
import SignalCard from "./SignalCard";
import { Chip, Notice } from "./ui";
import { api, ApiError } from "@/lib/api";
import { countdown } from "@/lib/format";
import type {
  AssetInfo,
  PaperCard,
  PaperEvidence,
  PaperLedger,
  PaperSummary,
  ConfigPayload,
  DataStatusPayload,
  GenerateResponse,
  HistoryRow,
  MarketPayload,
  PerformancePayload,
  ResearchLatest,
  ScanRow,
  Signal,
  SystemStatus,
} from "@/lib/types";

const STAGES = [
  "Fetching latest market data",
  "Data freshness & candle checks",
  "Computing M1 technical features",
  "Running model ensemble (LightGBM · RF · LogReg)",
  "Scoring confidence components",
  "Validation gates: regime · volatility · consistency",
  "Selecting the single best qualified setup",
];

type Phase = "idle" | "scanning" | "done" | "error";

function TimingItem({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value?: string | null;
  sub?: string;
  accent?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-ink-500">{label}</div>
      <div className={`font-mono text-[13px] ${accent ? "text-emerald-300" : "text-ink-100"}`}>{value ?? "—"}</div>
      {sub && <div className="text-[10px] text-ink-500">{sub}</div>}
    </div>
  );
}

export default function Dashboard() {
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [assets, setAssets] = useState<AssetInfo[]>([]);
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [selectedAsset, setSelectedAsset] = useState("AUTO");
  const [phase, setPhase] = useState<Phase>("idle");
  const [stageIndex, setStageIndex] = useState(0);
  const [result, setResult] = useState<GenerateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [performance, setPerformance] = useState<PerformancePayload | null>(null);
  const [scans, setScans] = useState<ScanRow[]>([]);
  const [daily, setDaily] = useState<GenerateResponse | null>(null);
  const [dailyLoading, setDailyLoading] = useState(false);
  const [market, setMarket] = useState<MarketPayload | null>(null);
  const [chartAsset, setChartAsset] = useState<string | null>(null);
  const [chartLoading, setChartLoading] = useState(false);
  const [chartError, setChartError] = useState<string | null>(null);
  const [dataStatus, setDataStatus] = useState<DataStatusPayload | null>(null);
  const [research, setResearch] = useState<ResearchLatest | null>(null);
  const [qualityError, setQualityError] = useState<string | null>(null);
  const [dataError, setDataError] = useState<string | null>(null);
  const [istClock, setIstClock] = useState("--:--:--");
  const [secondsToCandle, setSecondsToCandle] = useState(60);
  const [secondsToEntry, setSecondsToEntry] = useState<number | null>(null);
  const [online, setOnline] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [autoScan, setAutoScan] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [paper, setPaper] = useState<PaperSummary | null>(null);
  const [paperCard, setPaperCard] = useState<PaperCard | null>(null);
  const [paperEvidence, setPaperEvidence] = useState<PaperEvidence | null>(null);
  const [paperLedger, setPaperLedger] = useState<PaperLedger | null>(null);
  const [paperError, setPaperError] = useState<string | null>(null);

  const clockOffsetRef = useRef<number>(0);
  const lastScannedRef = useRef<string | null>(null);

  // ---------------------------------------------------------------- bootstrapping
  const loadQuality = useCallback(async () => {
    try {
      const payload = await api.researchLatest();
      setResearch(payload);
      setQualityError(null);
    } catch (err) {
      setQualityError((err as Error).message);
    }
  }, []);

  const loadPaper = useCallback(async () => {
    try {
      const [summary, card, evidence, ledger] = await Promise.all([
        api.paperSummary(),
        api.paperCard().catch(() => null),
        api.paperEvidence().catch(() => null),
        api.paperLedger(25).catch(() => null),
      ]);
      setPaper(summary);
      setPaperCard(card);
      setPaperEvidence(evidence);
      setPaperLedger(ledger);
      setPaperError(null);
    } catch (err) {
      setPaperError((err as Error).message);
    }
  }, []);

  const loadDataStatus = useCallback(async () => {
    try {
      const payload = await api.dataStatus();
      setDataStatus(payload);
      setDataError(null);
    } catch (err) {
      setDataError(err instanceof ApiError ? err.message : "Data status unavailable");
    }
  }, []);

  const loadSystem = useCallback(async () => {
    try {
      const payload = await api.system();
      setSystem(payload);
      setOnline(true);
      const serverTime = new Date(payload.time.ist).getTime();
      clockOffsetRef.current = serverTime - Date.now();
      setSecondsToCandle(payload.time.seconds_to_next_candle ?? 60);
    } catch {
      setOnline(false);
    }
  }, []);

  const loadLists = useCallback(async () => {
    try {
      const [historyPayload, perfPayload, scanPayload] = await Promise.all([
        api.history(25),
        api.performance(),
        api.scans(10),
      ]);
      setHistory(historyPayload.signals);
      setPerformance(perfPayload);
      setScans(scanPayload.scans);
    } catch {
      /* transient — the header already shows connectivity */
    }
  }, []);

  useEffect(() => {
    (async () => {
      await loadSystem();
      try {
        const [assetPayload, configPayload] = await Promise.all([api.assets(), api.config()]);
        setAssets(assetPayload.assets);
        setConfig(configPayload);
        setSelectedAsset(configPayload.settings.selected_asset || "AUTO");
      } catch {
        /* ignore */
      }
      await Promise.all([loadLists(), loadDataStatus(), loadQuality(), loadPaper()]).catch(() => null);
      try {
        const latest = await api.latest();
        if (latest.signal) {
          setResult({
            status: "SIGNAL",
            signal: latest.signal,
            confidence: latest.signal.confidence,
            quality: latest.signal.quality,
            min_confidence: 85,
          });
          setPhase("done");
        }
      } catch {
        /* ignore */
      }
    })();
  }, [loadSystem, loadLists, loadDataStatus, loadQuality, loadPaper]);

  // ------------------------------------------------------------------- IST clock
  useEffect(() => {
    const tick = () => {
      const now = Date.now() + clockOffsetRef.current;
      const ist = new Date(now);
      setIstClock(
        ist.toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour12: false }),
      );
      const seconds = ist.getUTCSeconds();
      setSecondsToCandle(60 - seconds);
      setSecondsToEntry((value) => (value === null ? value : Math.max(0, value - 1)));
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, []);

  // -------------------------------------------------------------------- polling
  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(() => {
      void loadSystem();
      void loadLists();
      void loadDataStatus();
      void loadQuality();
      void loadPaper();
    }, 12000);
    return () => clearInterval(timer);
  }, [autoRefresh, loadSystem, loadLists, loadDataStatus, loadQuality, loadPaper]);

  // entry countdown re-sync whenever a new signal appears
  useEffect(() => {
    if (phase === "done" && result?.status === "SIGNAL" && result.session) {
      setSecondsToEntry(Math.round(result.session.seconds_to_next_candle ?? 60));
    }
  }, [phase, result]);

  // pipeline stage animation
  useEffect(() => {
    if (phase !== "scanning") return;
    setStageIndex(0);
    const timer = setInterval(() => {
      setStageIndex((index) => Math.min(index + 1, STAGES.length - 1));
    }, 260);
    return () => clearInterval(timer);
  }, [phase]);

  // ------------------------------------------------------------------- actions
  const runScan = useCallback(
    async (mode: "NOW" | "DAILY" = "NOW") => {
      if (mode === "DAILY") {
        setDailyLoading(true);
        try {
          const payload = await api.daily(selectedAsset === "AUTO" ? null : selectedAsset);
          setDaily(payload);
        } catch (err) {
          setDaily({
            status: "ERROR",
            reason: (err as Error).message,
          } as GenerateResponse);
        } finally {
          setDailyLoading(false);
        }
        return;
      }

      setPhase("scanning");
      setError(null);
      try {
        const payload = await api.generate(selectedAsset === "AUTO" ? null : selectedAsset, "NOW");
        // let the stage animation finish for perceived smoothness
        await new Promise((resolve) => setTimeout(resolve, 420));
        setResult(payload);
        setPhase("done");
        if (payload.status === "SIGNAL" && payload.signal) {
          const asset = payload.signal.asset;
          if (chartAsset === null) {
            setChartAsset(asset);
            void loadChart(asset);
          }
        }
        void loadLists();
        void loadSystem();
      } catch (err) {
        const message =
          err instanceof ApiError
            ? err.status === 0
              ? "Backend unreachable — start the FastAPI server (uvicorn backend.main:app) and try again."
              : err.message
            : "Unexpected error";
        setError(message);
        setPhase("error");
        setResult(null);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selectedAsset, chartAsset, loadLists, loadSystem],
  );

  const loadChart = useCallback(async (asset: string) => {
    setChartLoading(true);
    setChartError(null);
    try {
      const payload = await api.market(asset, 180);
      if (payload.status !== "OK") {
        setMarket(null);
        setChartError(payload.reason || "Market data unavailable for this asset");
      } else {
        setMarket(payload);
      }
    } catch (err) {
      setMarket(null);
      setChartError(err instanceof ApiError ? err.message : "Failed to load candles");
    } finally {
      setChartLoading(false);
    }
  }, []);

  // auto scan: fire a scan shortly after each candle boundary
  useEffect(() => {
    if (!autoScan) return;
    if (secondsToCandle > 58) {
      const key = `${new Date().getHours()}:${new Date().getMinutes()}`;
      if (lastScannedRef.current !== key) {
        lastScannedRef.current = key;
        void runScan("NOW");
      }
    }
  }, [autoScan, secondsToCandle, runScan]);

  const saveConfig = useCallback(async (patch: Record<string, unknown>) => {
    try {
      const payload = await api.updateConfig(patch);
      setConfig((current) => (current ? { ...current, settings: payload.settings.settings } : current));
      setSaveMessage("saved");
      setTimeout(() => setSaveMessage(null), 1800);
      void loadSystem();
    } catch (err) {
      setSaveMessage(err instanceof ApiError ? err.message : "save failed");
    }
  }, [loadSystem]);

  const signal: Signal | null = useMemo(() => {
    if (phase === "done" && result?.status === "SIGNAL" && result.signal) return result.signal;
    return null;
  }, [phase, result]);

  const activeAsset = chartAsset ?? (selectedAsset !== "AUTO" ? selectedAsset : system?.data.assets.find((a) => a.available)?.asset ?? "EURUSD");

  return (
    <div className="mx-auto max-w-5xl px-3 pb-16 pt-3 sm:px-5">
      <Header system={system} istClock={istClock} istDate={system?.time.ist_date ?? ""} online={online} />

      {!online && (
        <div className="mb-4">
          <Notice tone="red" title="Backend offline">
            The dashboard cannot reach the signal API. Start it with{" "}
            <span className="font-mono">uvicorn backend.main:app --host 0.0.0.0 --port 8000</span> and reload.
          </Notice>
        </div>
      )}

      {/* IST clock block */}
      <div className="panel mb-4 grid-lines px-4 py-4 sm:px-5">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="label">🇮🇳 india time (asia/kolkata)</div>
            <div className="mt-1 font-mono text-4xl font-bold tracking-tight text-ink-100 sm:text-5xl">{istClock}</div>
            <div className="mt-1 text-[11px] text-ink-500">
              {system?.time.ist_date} · server-synchronised clock (browser timezone never used)
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="panel-tight px-3 py-2 text-center">
              <div className="label">next candle</div>
              <div className="font-mono text-lg text-ink-100">{countdown(secondsToCandle)}</div>
            </div>
            <div className="panel-tight px-3 py-2 text-center">
              <div className="label">entry in</div>
              <div className="font-mono text-lg text-ink-100">
                {secondsToEntry !== null ? countdown(secondsToEntry) : "—"}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="mb-4">
        <AssetSelector
          assets={assets}
          selected={selectedAsset}
          onSelect={(symbol) => {
            setSelectedAsset(symbol);
            void saveConfig({ selected_asset: symbol });
          }}
          disabled={phase === "scanning"}
          autoSelect={config?.settings.auto_select ?? true}
        />
      </div>

      <div className="mb-4">
        <SignalCard
          phase={phase}
          result={result}
          signal={signal}
          error={error}
          secondsToEntry={secondsToEntry}
          stageIndex={stageIndex}
          stages={STAGES}
          minConfidence={config?.settings.min_confidence ?? 85}
        />
      </div>

      {/* primary action */}
      <div className="panel mb-4 flex flex-col items-center gap-3 px-4 py-5">
        <button
          type="button"
          onClick={() => runScan("NOW")}
          disabled={phase === "scanning"}
          className="btn-primary w-full max-w-md !py-4 text-base tracking-wide sm:text-lg"
        >
          {phase === "scanning" ? "ANALYSING MARKET…" : "⚡ GENERATE SIGNAL"}
        </button>
        <div className="flex flex-wrap items-center justify-center gap-3 text-[11px] text-ink-500">
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={autoScan}
              onChange={(event) => setAutoScan(event.target.checked)}
              className="accent-emerald-400"
            />
            auto-scan every candle
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => setAutoRefresh(event.target.checked)}
              className="accent-emerald-400"
            />
            auto-refresh panels
          </label>
          <span>·</span>
          <span>
            mode <span className="font-mono text-ink-300">{system?.mode ?? "PAPER"}</span>
          </span>
          <span>·</span>
          <span>
            threshold <span className="font-mono text-ink-300">{config?.settings.min_confidence ?? 85}%</span>
          </span>
        </div>
        {system?.data.status !== "OK" && system?.data.message && (
          <Chip tone={system.data.status === "PARTIAL" ? "amber" : "red"}>{system.data.message}</Chip>
        )}
      </div>

      <div className="panel mb-4 flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 text-[11px]">
        <TimingItem label="current candle" value={system?.time.current_candle_ist} sub="forming now" />
        <TimingItem label="last closed candle" value={system?.time.last_closed_candle_ist} sub="used for prediction" />
        <TimingItem label="next candle" value={system?.time.next_candle_ist} sub="formation starts" />
        <TimingItem label="entry time" value={system?.time.entry_time_ist} sub="next M1 boundary" />
        <TimingItem label="expiry" value={system?.time.expiry_time_ist} sub={`${system?.time.expiry_minutes ?? 1} MIN after entry`} />
        <TimingItem label="countdown" value={countdown(secondsToCandle)} sub="backend clock" accent />
        <span className="text-ink-500/70">
          Only closed M1 candles are used for prediction; the in-progress candle is excluded server-side.
        </span>
      </div>

      <div className="mb-4 grid gap-4 lg:grid-cols-2">
        <ConfidencePanel components={result?.components ?? signal?.components ?? null} threshold={config?.settings.min_confidence ?? 85} />
        <div className="space-y-4">
          <ModelAgreementPanel ensemble={result?.ensemble ?? signal?.model_votes ?? null} modelStatus={system?.model ?? null} />
          <ChecksPanel checks={result?.checks ?? null} />
        </div>
      </div>

      <div className="mb-4 grid gap-4 lg:grid-cols-2">
        <MarketConditionsPanel features={result?.features ?? null} feed={result?.feed ?? null} />
        <div className="space-y-4">
          <ChartPanel
            market={market}
            asset={activeAsset}
            loading={chartLoading}
            error={chartError}
            onLoad={(asset) => {
              setChartAsset(asset);
              void loadChart(asset);
            }}
          />
        </div>
      </div>

      <div className="mb-4">
        <LivePaperPanel
          summary={paper}
          card={paperCard}
          evidence={paperEvidence}
          ledger={paperLedger}
          error={paperError}
        />

        <ModelQualityPanel
          model={system?.model ?? null}
          performance={performance}
          research={research}
          error={qualityError}
        />
      </div>

      <div className="mb-4">
        <DataProvenancePanel status={dataStatus} error={dataError} />
      </div>

      <div className="mb-4">
        <HistoryTable rows={history} />
      </div>

      <div className="mb-4">
        <DailyPanel
          daily={daily}
          loading={dailyLoading}
          onRefresh={() => runScan("DAILY")}
          disabled={phase === "scanning"}
        />
      </div>

      <div className="mb-4">
        <PerformancePanel data={performance} />
      </div>

      <div className="mb-4">
        <ScanLogPanel scans={scans} />
      </div>

      <div className="mb-4">
        <SettingsPanel
          config={config}
          assets={assets}
          onSave={saveConfig}
          onReloadModel={async () => {
            await api.reloadModel().catch(() => null);
            await loadSystem();
          }}
          onImported={async () => {
            await loadDataStatus();
          }}
          onEvaluate={async () => {
            await api.evaluate().catch(() => null);
            await loadLists();
          }}
          saving={false}
          message={saveMessage}
        />
      </div>

      <footer className="panel px-4 py-4 text-[11px] leading-relaxed text-ink-500">
        <p className="font-semibold text-ink-300">Risk &amp; honesty notice</p>
        <p className="mt-1">
          Quotex Signal AI produces study/paper signals. Confidence is a calculated model score — it is not a
          guarantee, a &quot;sure shot&quot;, or an accuracy claim. Signals can and do lose. No order is ever sent
          to a broker by this build. Model validation metrics (offline) and signal performance (live paper) are
          reported separately and must not be combined.
        </p>
        <p className="mt-2">
          Data provenance is always displayed: simulated or replay feeds are labelled as such on every card. WIN/LOSS
          is only recorded when the verified closing candle exists.
        </p>
      </footer>
    </div>
  );
}

/** Typed API client. All requests are same-origin (`/api/*`), proxied to FastAPI by next.config.mjs. */
import type {
  PaperCard,
  PaperEvidence,
  PaperLedger,
  PaperSummary,
  AssetInfo,
  ConfigPayload,
  DataImportResponse,
  DataStatusPayload,
  GenerateResponse,
  HistoryRow,
  MarketPayload,
  ModelStatus,
  PerformancePayload,
  ResearchLatest,
  ScanRow,
  Signal,
  SystemStatus,
} from "./types";

export class ApiError extends Error {
  status: number;
  payload: any;

  constructor(message: string, status: number, payload?: any) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

async function request<T>(path: string, init?: RequestInit, timeoutMs = 25000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    });
    const text = await response.text();
    const payload = text ? safeJson(text) : null;
    if (!response.ok) {
      const detail = payload?.detail ?? payload;
      const reason =
        (typeof detail === "object" && detail ? detail.reason || detail.status : null) ||
        (typeof detail === "string" ? detail : null) ||
        `Request failed (${response.status})`;
      throw new ApiError(reason, response.status, payload);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if ((error as Error).name === "AbortError") throw new ApiError("Request timed out — backend not responding", 0);
    throw new ApiError((error as Error).message || "Network error", 0);
  } finally {
    clearTimeout(timer);
  }
}

function safeJson(text: string): any {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const api = {
  system: () => request<SystemStatus>("/api/system", undefined, 30000),
  health: () => request<any>("/api/health", undefined, 8000),
  ist: () => request<any>("/api/time/ist", undefined, 8000),
  assets: (probe = false) => request<{ assets: AssetInfo[] }>(`/api/assets?probe=${probe}`, undefined, 20000),
  market: (asset: string, limit = 180) =>
    request<MarketPayload>(`/api/market/${encodeURIComponent(asset)}?limit=${limit}`, undefined, 20000),
  generate: (asset: string | null, mode: "NOW" | "DAILY" = "NOW") =>
    request<GenerateResponse>(
      "/api/signal/generate",
      { method: "POST", body: JSON.stringify({ asset: asset ?? "AUTO", mode }) },
      90000,
    ),
  latest: () => request<{ signal: Signal | null; last_scan: any; session: any }>("/api/signal/latest", undefined, 15000),
  history: (limit = 30) => request<{ signals: HistoryRow[] }>(`/api/signals/history?limit=${limit}`, undefined, 15000),
  performance: () => request<PerformancePayload>("/api/signals/performance", undefined, 20000),
  scans: (limit = 12) => request<{ scans: ScanRow[] }>(`/api/signals/scans?limit=${limit}`, undefined, 15000),
  daily: (asset?: string | null) =>
    request<GenerateResponse>(`/api/signals/daily${asset ? `?asset=${encodeURIComponent(asset)}` : ""}`, undefined, 120000),
  evaluate: () => request<any>("/api/signals/evaluate", { method: "POST" }, 60000),
  modelStatus: () => request<ModelStatus>("/api/model/status", undefined, 15000),
  config: () => request<ConfigPayload>("/api/config", undefined, 15000),
  dataStatus: () => request<DataStatusPayload>("/api/data/status", {}, 20000),
  dataProvenance: () => request<any>("/api/data/provenance", {}, 15000),
  importCsv: (body: Record<string, unknown>) =>
    request<DataImportResponse>("/api/data/import", { method: "POST", body: JSON.stringify(body) }, 60000),
  updateConfig: (patch: Record<string, unknown>) =>
    request<any>("/api/config", { method: "POST", body: JSON.stringify(patch) }, 20000),
  reloadModel: () => request<any>("/api/model/reload", { method: "POST" }, 30000),
  researchLatest: () => request<ResearchLatest>("/api/research/latest", {}, 20000),
  researchExperiments: (limit = 200) =>
    request<any>(`/api/research/experiments?limit=${limit}`, {}, 20000),
  researchExperiment: (id: string) =>
    request<any>(`/api/research/experiments/${encodeURIComponent(id)}`, {}, 20000),
  // continuous M1 ingestion + paper trading
  dataLiveStatus: () => request<any>("/api/data/live-status", {}, 20000),
  paperSummary: () => request<PaperSummary>("/api/paper/summary", {}, 20000),
  paperCard: () => request<PaperCard>("/api/paper/card", {}, 15000),
  paperLedger: (limit = 25) => request<PaperLedger>(`/api/paper/ledger?limit=${limit}`, {}, 20000),
  paperEvidence: () => request<PaperEvidence>("/api/research/paper-evidence", {}, 20000),
  paperReport: () => request<any>("/api/research/paper-report", { method: "POST" }, 30000),
};

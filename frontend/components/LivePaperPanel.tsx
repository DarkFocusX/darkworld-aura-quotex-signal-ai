"use client";

import { Chip, KeyValue, Notice, Panel, Stat } from "./ui";
import type { PaperCard, PaperEvidence, PaperLedger, PaperSummary } from "@/lib/types";

/** LIVE PAPER TRADING panel (§7/§8 of the continuous-data phase).
 *
 * Reports only what the backend recorded: the freshness verdict of the continuously
 * ingested feed, the paper ledger counters, the evidence bar, and the real-time card
 * (a qualified paper signal, or NO TRADE with the *actual* blocking reason).
 *
 * The panel never renders a signal the gates did not approve, and never labels
 * anything profitable while the published evidence bar is unmet.
 */
export default function LivePaperPanel({
  summary,
  card,
  evidence,
  ledger,
  error,
}: {
  summary: PaperSummary | null;
  card: PaperCard | null;
  evidence: PaperEvidence | null;
  ledger: PaperLedger | null;
  error?: string | null;
}) {
  if (error) {
    return (
      <Panel title="Live paper trading" subtitle="Continuous M1 ingestion → same engine → verified paper ledger">
        <Notice tone="red">
          Live paper state could not be loaded: {error}. No numbers are invented while the backend is
          unreachable.
        </Notice>
      </Panel>
    );
  }
  if (!summary) {
    return (
      <Panel title="Live paper trading" subtitle="Continuous M1 ingestion → same engine → verified paper ledger">
        <div className="text-xs text-slate-400">Loading live paper state…</div>
      </Panel>
    );
  }

  const fresh = summary.data_freshness;
  const tone = fresh === "FRESH" ? "green" : fresh === "STALE" ? "amber" : "red";
  const paper = summary.paper;
  const scans = summary.scans;
  const pct = (value: number | null | undefined, digits = 2) =>
    value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
  const barPassed = evidence?.evidence_bar === "PASSED";

  return (
    <Panel
      title="Live paper trading"
      subtitle="Continuous M1 ingestion → the same engine the button runs → a paper ledger settled from verified candles only"
      right={<Chip tone={tone}>PAPER MODE · DATA {fresh}</Chip>}
    >
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
        <Stat label="current IST" value={summary.current_ist?.slice(0, 8) ?? "—"} sub="server clock" mono />
        <Stat
          label="latest M1"
          value={summary.latest_m1 ? summary.latest_m1.asset : "—"}
          sub={summary.latest_m1 ? `${summary.latest_m1.provider ?? "—"} · close ${summary.latest_m1.close_time_utc?.slice(11, 19)}Z` : "no ingested candle"}
          mono
        />
        <Stat
          label="next M1"
          value={summary.next_m1 ? summary.next_m1.open_time_ist.slice(11, 19) : "—"}
          sub={summary.next_m1 ? `${Math.round(summary.next_m1.seconds_away)}s away` : ""}
          mono
        />
        <Stat label="paper signals today" value={paper.signals_today} sub="qualified only" mono />
        <Stat label="pending" value={paper.pending} sub="awaiting expiry candle" mono />
        <Stat label="wins" value={paper.wins} sub="verified close only" mono />
        <Stat label="losses" value={paper.losses} sub="verified close only" mono />
        <Stat label="unresolved" value={paper.unresolved} sub="candle could not be verified" mono />
        <Stat label="coverage" value={pct(scans.coverage)} sub="signals / recorded scans" mono />
        <Stat label="no-trade rate" value={pct(scans.no_trade_rate)} sub="rejections are the normal case" mono />
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-400">Real-time card</div>
          {card && card.kind === "PAPER SIGNAL" ? (
            <Notice tone="green">
              <div className="font-semibold">🚨 QUOTEX M1 PAPER SIGNAL</div>
              <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 font-mono text-xs">
                <span>Asset</span><span>{card.asset}</span>
                <span>💎 Timeframe</span><span>M1</span>
                <span>🕐 Entry</span><span>{card.time_ist} IST</span>
                <span>{card.direction === "PUT" ? "🔻" : "🔺"} Direction</span><span>{card.direction}</span>
                <span>🎯 Model Confidence</span><span>{card.confidence}%</span>
                <span>🤖 Model Agreement</span>
                <span>{card.model_agreement === null || card.model_agreement === undefined ? "—" : `${(card.model_agreement * 100).toFixed(1)}%`}</span>
                <span>⏳ Expiry</span><span>{card.expiry_minutes} MIN</span>
                <span>📡 Data</span><span>{card.data}</span>
                <span>Provider</span><span>{card.provider ?? "—"}</span>
                <span>Model</span><span>{card.model ?? "—"}</span>
                <span>Ledger</span><span>{card.status}</span>
              </div>
            </Notice>
          ) : (
            <Notice tone="amber">
              <div className="font-semibold">NO TRADE</div>
              <div className="mt-1 text-xs">
                Reason: <span className="font-mono">{card?.reason ?? "no qualified setup"}</span>
              </div>
              {card?.detail ? <div className="mt-1 text-[11px] text-slate-300">{card.detail}</div> : null}
              {card?.blocked_by?.length ? (
                <div className="mt-1 text-[11px] text-slate-300">blocked by: {card.blocked_by.join(" · ")}</div>
              ) : null}
              <div className="mt-1 text-[11px] text-slate-400">
                Nothing is forced: a signal appears only when every gate passes.
              </div>
            </Notice>
          )}
        </div>

        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-400">
            Feed status (FRESH / STALE / INVALID / UNAVAILABLE)
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-400">
                  <th className="text-left">asset</th>
                  <th className="text-left">provider</th>
                  <th className="text-left">status</th>
                  <th className="text-left">age</th>
                  <th className="text-left">rows</th>
                  <th className="text-left">gaps</th>
                  <th className="text-left">flags</th>
                </tr>
              </thead>
              <tbody>
                {summary.live.assets.map((row) => {
                  const rowTone =
                    row.freshness_status === "FRESH" ? "green" : row.freshness_status === "STALE" ? "amber" : "red";
                  const flags =
                    [row.is_simulated ? "SIMULATED" : null, row.is_replay ? "REPLAY" : null]
                      .filter(Boolean)
                      .join(" · ") || "REAL";
                  return (
                    <tr key={row.asset} className="border-t border-white/5">
                      <td className="font-mono">{row.asset}</td>
                      <td className="font-mono">{row.provider ?? "—"}</td>
                      <td>
                        <Chip tone={rowTone}>{row.freshness_status}</Chip>
                      </td>
                      <td className="font-mono">{row.age_seconds === null ? "—" : `${Math.round(row.age_seconds)}s`}</td>
                      <td className="font-mono">{row.rows}</td>
                      <td className="font-mono">{row.gaps ? row.gaps.missing_minutes : "—"}</td>
                      <td className="font-mono text-[10px]">{flags}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="mb-1 mt-3 text-[11px] uppercase tracking-wide text-slate-400">Evidence tracker (spec §12)</div>
          <KeyValue
            items={[
              ["decided paper trades", evidence?.total_decided_trades ?? 0],
              ["wins / losses", `${evidence?.wins ?? 0} / ${evidence?.losses ?? 0}`],
              ["win rate", pct(evidence?.win_rate ?? null, 1)],
              ["Wilson 95% lower bound", evidence?.wilson_95_lower_bound?.toFixed(4) ?? "—"],
              ["max losing streak", evidence?.max_losing_streak ?? 0],
              ["days / assets", `${evidence?.days ?? 0} / ${evidence?.assets.length ?? 0}`],
            ]}
          />
          <Notice tone={barPassed ? "amber" : "red"}>
            EVIDENCE BAR: <b>{evidence?.evidence_bar ?? "NOT PASSED"}</b> — rule: ≥{" "}
            {evidence?.evidence_rule.minimum_qualifying_signals ?? 100} decided signals and a Wilson 95% lower bound
            above {(evidence?.evidence_rule.wilson_95_lower_bound_above ?? 0.5).toFixed(2)}.{" "}
            {barPassed ? "Still not a profitability claim." : "Until then nothing here is labelled profitable."}
          </Notice>
        </div>
      </div>

      <div className="mt-3">
        <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-400">
          Paper ledger — every qualified signal with its feed provenance and verified outcome
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-400">
                <th className="text-left">Signal</th>
                <th className="text-left">Entry IST</th>
                <th className="text-left">Direction</th>
                <th className="text-left">Confidence</th>
                <th className="text-left">Provider</th>
                <th className="text-left">Model</th>
                <th className="text-left">Status</th>
                <th className="text-left">Result</th>
              </tr>
            </thead>
            <tbody>
              {(ledger?.rows ?? []).length === 0 ? (
                <tr>
                  <td colSpan={8} className="py-2 text-slate-400">
                    No qualified paper signal has been recorded yet — the ledger stays empty rather than showing a
                    forced CALL/PUT.
                  </td>
                </tr>
              ) : (
                (ledger?.rows ?? []).map((row) => (
                  <tr key={row.signal_id} className="border-t border-white/5">
                    <td className="font-mono text-[10px]">{row.signal_id.slice(-20)}</td>
                    <td className="font-mono">{row.entry_time_ist ?? "—"}</td>
                    <td className="font-mono">{row.direction ?? "—"}</td>
                    <td className="font-mono">{row.confidence === null ? "—" : `${row.confidence}%`}</td>
                    <td className="font-mono text-[10px]">{row.feed_provider ?? "—"}</td>
                    <td className="font-mono text-[10px]">{row.model_version ?? "—"}</td>
                    <td>
                      <Chip
                        tone={
                          row.status === "WIN" ? "green" : row.status === "LOSS" ? "red" : row.status === "PENDING" ? "blue" : "amber"
                        }
                      >
                        {row.status}
                      </Chip>
                    </td>
                    <td className="font-mono text-[10px]">{row.result ?? "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </Panel>
  );
}

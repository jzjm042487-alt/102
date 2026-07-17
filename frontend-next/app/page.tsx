"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  legacyGroupFromSample,
  loadSamples,
  logComparison,
  solveProblem,
  type SampleRecord,
  type SolveResponse,
} from "./lib/api";
import {
  asArray,
  cutsByStockLength,
  firstDefined,
  formatLength,
  formatNumber,
  formatRate,
  groupCollections,
  groupIdentity,
  groupMetrics,
  normalizeGroups,
  normalizeParts,
  normalizeRate,
  toNumber,
  verificationPassed,
  weldsByPipeIdentity,
  type CutMethod,
  type NestingGroup,
  type PipeWelds,
} from "./lib/nesting";

const colors = ["#258dff", "#57c4a8", "#8769e9", "#f0a959", "#e96f87", "#63a9cf", "#7ab967", "#c882d7"];

type CompareKind = "rate" | "int" | "length";

interface CompareRowDef {
  key: "utilization" | "weldingJoints" | "usedStockLength" | "cuttingPatternTypes" | "weldingPatternTypes";
  label: string;
  hint: string;
  kind: CompareKind;
  betterHigh: boolean;
}

const COMPARE_ROWS: CompareRowDef[] = [
  { key: "utilization", label: "综合利用率", hint: "有效成品长度 ÷ 实际领用原管", kind: "rate", betterHigh: true },
  { key: "weldingJoints", label: "新增焊口", hint: "数值越少，现场焊接工作量越低", kind: "int", betterHigh: false },
  { key: "usedStockLength", label: "领用原管", hint: "实际投入库存总长", kind: "length", betterHigh: false },
  { key: "cuttingPatternTypes", label: "切法种类", hint: "种类越少，车间换型越少", kind: "int", betterHigh: false },
  { key: "weldingPatternTypes", label: "拼法种类", hint: "种类越少，标准化程度越高", kind: "int", betterHigh: false },
];

function formatCompareValue(value: number, kind: CompareKind): string {
  if (kind === "rate") return formatRate(value);
  if (kind === "length") return `${formatLength(value)} mm`;
  return formatNumber(value, 0);
}

function formatCompareDelta(delta: number, kind: CompareKind): string {
  const sign = delta > 0 ? "+" : "";
  if (kind === "rate") return `${sign}${formatNumber(normalizeRate(delta), 4)}pp`;
  if (kind === "length") return `${sign}${formatLength(delta)} mm`;
  return `${sign}${formatNumber(delta, 0)}`;
}

function stockCountOf(group: NestingGroup | null): number {
  if (!group) return 0;
  return groupCollections(group).cutting.reduce(
    (sum, p) => sum + toNumber(firstDefined(p, ["quantity", "count", "stock_quantity"], 1), 1),
    0,
  );
}

function sideMetrics(groups: NestingGroup[]) {
  const list = groups.map(groupMetrics);
  const used = list.reduce((s, m) => s + m.usedStockLength, 0);
  const demand = list.reduce((s, m) => s + m.demandLength, 0);
  return {
    utilization: used > 0 ? demand / used : list.reduce((s, m) => s + m.utilization, 0) / (list.length || 1),
    weldingJoints: list.reduce((s, m) => s + m.weldingJoints, 0),
    usedStockLength: used,
    stockCount: groups.reduce((s, g) => s + stockCountOf(g), 0),
    cuttingPatternTypes: list.reduce((s, m) => s + m.cuttingPatternTypes, 0),
    weldingPatternTypes: list.reduce((s, m) => s + m.weldingPatternTypes, 0),
  };
}

// 每次求解后落盘一条 A/B 对比记录，供后端 compare_log.jsonl 分析差距。
function buildCompareRecord(
  sample: SampleRecord,
  solved: SolveResponse,
  timeLimit: number,
  elapsedSeconds: number,
) {
  const systemGroups = normalizeGroups(solved).filter((g) => !g.is_legacy_group);
  const legacyGroup = legacyGroupFromSample(sample);
  const system = sideMetrics(systemGroups);
  const legacy = sideMetrics(legacyGroup ? [legacyGroup] : []);
  return {
    sample_id: sample.id,
    com: sample.com,
    material: sample.material,
    spec: sample.spec,
    pipe_count: sample.pipe_count,
    time_limit_seconds: timeLimit,
    elapsed_seconds: Number(elapsedSeconds.toFixed(2)),
    status: solved.status ?? null,
    verified: verificationPassed(getVerificationSafe(solved)),
    system,
    legacy,
    delta: {
      cuttingPatternTypes: system.cuttingPatternTypes - legacy.cuttingPatternTypes,
      weldingPatternTypes: system.weldingPatternTypes - legacy.weldingPatternTypes,
      weldingJoints: system.weldingJoints - legacy.weldingJoints,
      utilization: system.utilization - legacy.utilization,
      stockCount: system.stockCount - legacy.stockCount,
    },
  };
}

function getVerificationSafe(solved: SolveResponse): unknown {
  return (solved as { verification?: unknown }).verification ?? solved;
}

function pieceColor(piece: number): string {
  const colorIndex = Math.abs(piece * 7 + Math.floor(piece / 10)) % colors.length;
  return colors[colorIndex];
}

function MiniRing({ value, color }: { value: number; color: string }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div className="mini-ring" style={{ background: `conic-gradient(${color} ${v * 3.6}deg, #e9edf3 0deg)` }}>
      <div className="mini-ring__inside">
        <strong>{v.toFixed(2)}</strong>
        <span>%</span>
      </div>
    </div>
  );
}

function WinnerBadge({ type }: { type: "system" | "legacy" }) {
  return <span className={`winner-badge ${type}`}>{type === "system" ? "A 更优" : "B 更优"}</span>;
}

function CuttingMethodCard({ method, scheme }: { method: CutMethod; scheme: "system" | "legacy" }) {
  const schemeLabel = scheme === "system" ? "A" : "B";
  const pattern = method.pattern;
  const stockLength = toNumber(firstDefined(pattern, ["stock_length", "StockLength", "material_length"], 12000), 12000) || 12000;
  const parts = normalizeParts(firstDefined(pattern, ["parts", "Parts", "Part"], []));
  const loss = method.loss;
  const lossCls = loss <= 70 ? "good" : loss > 500 ? "bad" : "";
  return (
    <div className={`cutting-card ${scheme}`}>
      <div className="cutting-card__meta">
        <div className="cutting-card__id">
          <i>{schemeLabel}</i>
          <strong>{method.id}</strong>
          <span>执行 {formatNumber(method.quantity, 0)} 次</span>
        </div>
        <div className={`cutting-card__loss ${lossCls}`}>
          <strong>{formatLength(loss)} mm</strong>
          <span>切损</span>
        </div>
      </div>
      <div className="pipe-bar" aria-label={`${schemeLabel} 方案 ${method.id} 切法`}>
        {parts.map((piece, index) => (
          <span
            key={`${piece}-${index}`}
            style={{ width: `${(piece / stockLength) * 100}%`, background: pieceColor(piece) }}
          >
            <b>{formatLength(piece)}</b>
          </span>
        ))}
        {loss > 0 && (
          <span className="loss" style={{ width: `${(loss / stockLength) * 100}%` }}>
            <b>{loss >= 150 ? formatLength(loss) : ""}</b>
          </span>
        )}
      </div>
      <div className="pipe-meta">
        <span>0</span>
        <span>原管 {formatLength(stockLength)} mm</span>
      </div>
    </div>
  );
}

function SpliceMethodList({ block, scheme }: { block: PipeWelds | undefined; scheme: "A" | "B" }) {
  if (!block || !block.methods.length) {
    return <div className="cutting-empty">{scheme} 在该管圈上无拼法</div>;
  }
  return (
    <table className="splice-mini">
      <thead>
        <tr>
          <th>有序料段（mm）</th>
          <th>焊口位置（mm）</th>
          <th>根数</th>
        </tr>
      </thead>
      <tbody>
        {block.methods.map((method, index) => (
          <tr key={`${scheme}-${index}`}>
            <td className="mono">{method.parts.map((p) => formatLength(p)).join(" + ")}</td>
            <td className="mono">{method.weldPositions.length ? method.weldPositions.map((p) => formatLength(p)).join(" / ") : "整段"}</td>
            <td>{formatNumber(method.quantity, 0)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

interface ComparableMetrics {
  utilization: number;
  weldingJoints: number;
  usedStockLength: number;
  cuttingPatternTypes: number;
  weldingPatternTypes: number;
}

interface ComputedRow extends CompareRowDef {
  system: number;
  legacy: number;
  delta: number;
  meaningful: boolean;
  verdict: "win" | "lose" | "tie";
}

export default function Home() {
  const [samples, setSamples] = useState<SampleRecord[]>([]);
  const [samplesError, setSamplesError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [timeLimit, setTimeLimit] = useState(300);
  const [engine, setEngine] = useState<"baseline" | "route3">("route3");

  const [solving, setSolving] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<SolveResponse | null>(null);
  const [legacyGroup, setLegacyGroup] = useState<NestingGroup | null>(null);
  const [solveError, setSolveError] = useState("");

  const [tab, setTab] = useState<"compare" | "cut" | "splice" | "audit">("compare");
  const [toast, setToast] = useState("");

  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    loadSamples()
      .then((list) => setSamples(list))
      .catch((error: unknown) => setSamplesError(error instanceof Error ? error.message : String(error)));
  }, []);

  const selectedSample = useMemo(
    () => samples.find((s) => s.id === selectedId) || null,
    [samples, selectedId],
  );

  const systemGroups = useMemo(
    () => (result ? normalizeGroups(result).filter((g) => !g.is_legacy_group) : []),
    [result],
  );

  const sampleStats = useMemo(() => {
    const map = new Map<string, { pipes: number; stocks: number; util: number }>();
    samples.forEach((sample) => {
      const util = toNumber(firstDefined(sample.legacy?.GeneralInfo, ["UtilRate"], 0), 0);
      map.set(sample.id, {
        pipes: toNumber(sample.pipe_count, 0),
        stocks: stockCountOf(legacyGroupFromSample(sample)),
        util,
      });
    });
    return map;
  }, [samples]);

  const sampleLabel = (sample: SampleRecord) => {
    const stat = sampleStats.get(sample.id);
    const util = stat?.util;
    const utilText = util ? ` · 旧算法 ${formatRate(util)}` : "";
    return `${sample.com} · ${sample.material} · ${sample.spec} · 管圈 ${stat?.pipes ?? sample.pipe_count} · 原料 ${stat?.stocks ?? 0} 根${utilText}`;
  };

  function showToast(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(""), 2200);
  }

  function stopTimer() {
    if (timerRef.current) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }

  async function runSolve() {
    if (!selectedSample || solving) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setSolving(true);
    setSolveError("");
    setElapsed(0);
    const startedAt = performance.now();
    stopTimer();
    timerRef.current = window.setInterval(() => {
      setElapsed((performance.now() - startedAt) / 1000);
    }, 100);

    // 旧软件结果先就位，求解完成即可对比。
    setLegacyGroup(legacyGroupFromSample(selectedSample));

    try {
      const solved = await solveProblem(selectedSample.problem, timeLimit, controller.signal, engine);
      const solvedElapsed = (performance.now() - startedAt) / 1000;
      setResult(solved);
      setTab("compare");
      showToast("排料计算已完成");
      void logComparison(
        buildCompareRecord(selectedSample, solved, timeLimit, solvedElapsed),
      );
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      const isNetwork = error instanceof TypeError;
      setSolveError(
        isNetwork
          ? "无法连接排料计算服务，请确认 FastAPI 已在 127.0.0.1:8000 启动。"
          : error instanceof Error
            ? error.message
            : String(error),
      );
    } finally {
      stopTimer();
      setSolving(false);
    }
  }

  const systemMetrics: ComparableMetrics = useMemo(() => {
    const list = systemGroups.map(groupMetrics);
    return {
      utilization: (() => {
        const used = list.reduce((s, m) => s + m.usedStockLength, 0);
        const demand = list.reduce((s, m) => s + m.demandLength, 0);
        return used > 0 ? demand / used : 0;
      })(),
      weldingJoints: list.reduce((s, m) => s + m.weldingJoints, 0),
      usedStockLength: list.reduce((s, m) => s + m.usedStockLength, 0),
      cuttingPatternTypes: list.reduce((s, m) => s + m.cuttingPatternTypes, 0),
      weldingPatternTypes: list.reduce((s, m) => s + m.weldingPatternTypes, 0),
    };
  }, [systemGroups]);

  const legacyMetrics = useMemo(() => groupMetrics(legacyGroup), [legacyGroup]);

  const rows: ComputedRow[] = useMemo(() => {
    return COMPARE_ROWS.map((row) => {
      const systemValue = toNumber((systemMetrics as unknown as Record<string, number>)[row.key], 0);
      const legacyValue = toNumber((legacyMetrics as unknown as Record<string, number>)[row.key], 0);
      const delta = systemValue - legacyValue;
      const meaningful = Math.abs(delta) > (row.kind === "rate" ? 1e-6 : 1e-9);
      let verdict: "win" | "lose" | "tie" = "tie";
      if (meaningful) {
        const systemBetter = row.betterHigh ? delta > 0 : delta < 0;
        verdict = systemBetter ? "win" : "lose";
      }
      return { ...row, system: systemValue, legacy: legacyValue, delta, meaningful, verdict };
    });
  }, [systemMetrics, legacyMetrics]);

  const hasComparison = Boolean(result && legacyGroup);
  const wins = rows.filter((r) => r.verdict === "win").length;
  const losses = rows.filter((r) => r.verdict === "lose").length;
  const verdictTone: "system" | "legacy" = wins >= losses ? "system" : "legacy";
  const verdictTitle = wins > losses ? "本系统方案综合表现更优" : wins < losses ? "旧软件方案综合表现更优" : "两套方案综合表现相当";

  const systemUtil = normalizeRate(systemMetrics.utilization);
  const legacyUtil = normalizeRate(legacyMetrics.utilization);
  const passed = result ? verificationPassed(result) : false;

  const identity = systemGroups.length ? groupIdentity(systemGroups[0]) : { material: "—", specifications: "—" };
  const materialLabel = identity.material;
  const specLabel = identity.specifications;

  const summaryDemand = systemGroups.map(groupMetrics).reduce((s, m) => s + m.demandLength, 0);

  const insights = rows
    .filter((r) => r.meaningful)
    .slice(0, 3)
    .map((r) => {
      const better = r.verdict === "win" ? "本系统" : "旧软件";
      const iconTone = r.verdict === "win" ? "green" : r.key === "usedStockLength" ? "blue" : "purple";
      return { key: r.key, label: r.label, better, delta: formatCompareDelta(r.delta, r.kind), iconTone, betterHigh: r.betterHigh };
    });

  // A/B 切法对照：按母材长度汇总（跨材质池合并同长度）。
  const sysCuttingAll = systemGroups.flatMap((g) => groupCollections(g).cutting);
  const legacyCuttingAll = legacyGroup ? groupCollections(legacyGroup).cutting : [];
  const sysCutsByLen = cutsByStockLength(sysCuttingAll);
  const legacyCutsByLen = cutsByStockLength(legacyCuttingAll);
  const cutLengths = Array.from(
    new Set([...sysCutsByLen.map((c) => c.stockLength), ...legacyCutsByLen.map((c) => c.stockLength)]),
  ).sort((a, b) => a - b);
  const sysCutMap = new Map(sysCutsByLen.map((c) => [c.stockLength, c]));
  const legacyCutMap = new Map(legacyCutsByLen.map((c) => [c.stockLength, c]));

  // A/B 拼法对照：按管圈标识汇总。
  const sysWeldingAll = systemGroups.flatMap((g) => groupCollections(g).welding);
  const legacyWeldingAll = legacyGroup ? groupCollections(legacyGroup).welding : [];
  const sysWeldsByPipe = weldsByPipeIdentity(sysWeldingAll);
  const legacyWeldsByPipe = weldsByPipeIdentity(legacyWeldingAll);
  const weldIdentities = Array.from(
    new Set([...sysWeldsByPipe.map((w) => w.identity), ...legacyWeldsByPipe.map((w) => w.identity)]),
  ).sort((a, b) => new Intl.Collator("zh-CN", { numeric: true }).compare(a, b));
  const sysWeldMap = new Map(sysWeldsByPipe.map((w) => [w.identity, w]));
  const legacyWeldMap = new Map(legacyWeldsByPipe.map((w) => [w.identity, w]));

  const cutCountLabel = `${systemMetrics.cuttingPatternTypes} / ${legacyMetrics.cuttingPatternTypes}`;
  const spliceCountLabel = `${systemMetrics.weldingPatternTypes} / ${legacyMetrics.weldingPatternTypes}`;

  const normalizationCount = systemGroups.reduce(
    (sum, g) => sum + groupCollections(g).normalizations.length,
    0,
  );

  return (
    <main className="app-shell">
      {toast && (
        <div className="toast" role="status">
          <span>✓</span>
          {toast}
        </div>
      )}

      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </div>
          <div>
            <strong>排料分析中心</strong>
            <span>PRODUCTION NESTING</span>
          </div>
        </div>
        <nav className="topnav" aria-label="主导航">
          <a className="active" href="#">
            排料任务
          </a>
          <a href="#details">方案对比</a>
          <a href="#details">历史记录</a>
        </nav>
        <div className="top-actions">
          <button className="icon-button" aria-label="帮助">
            ?
          </button>
          <div className="avatar">工</div>
        </div>
      </header>

      <div className="page-wrap">
        <div className="breadcrumb">
          <span>生产排料</span>
          <b>/</b>
          <span>任务结果</span>
        </div>

        {/* 用例选择 + 求解 */}
        <section className="run-panel">
          <div className="run-panel-head">
            <div>
              <span className="section-kicker">选择历史用例</span>
              <h2>从旧软件已解出的用例中挑选，重新用本系统求解并对比</h2>
            </div>
            <span className="run-panel-count">
              {samplesError ? `样例库不可用：${samplesError}` : `${samples.length} 条已解出用例`}
            </span>
          </div>
          <div className="run-controls">
            <label className="run-field record-field">
              <span>历史用例（部件图号 · 材质 · 规格）</span>
              <select
                value={selectedId}
                onChange={(e) => setSelectedId(e.target.value)}
                disabled={!samples.length}
              >
                <option value="">{samples.length ? `选择历史用例（共 ${samples.length} 条）` : "加载样例库…"}</option>
                {[...samples]
                  .sort((a, b) => {
                    const sa = sampleStats.get(a.id);
                    const sb = sampleStats.get(b.id);
                    const pipeDiff = (sa?.pipes ?? 0) - (sb?.pipes ?? 0);
                    if (pipeDiff !== 0) return pipeDiff;
                    const stockDiff = (sa?.stocks ?? 0) - (sb?.stocks ?? 0);
                    if (stockDiff !== 0) return stockDiff;
                    return sampleLabel(a).localeCompare(sampleLabel(b), "zh-CN");
                  })
                  .map((sample) => (
                    <option key={sample.id} value={sample.id}>
                      {sampleLabel(sample)}
                    </option>
                  ))}
              </select>
            </label>
            <label className="run-field time-field">
              <span>求解上限（秒）</span>
              <input
                type="number"
                min={1}
                max={3600}
                step={10}
                value={timeLimit}
                onChange={(e) => setTimeLimit(Math.min(3600, Math.max(1, Number(e.target.value) || 120)))}
              />
            </label>
            <label className="run-field time-field">
              <span>求解引擎</span>
              <select value={engine} onChange={(e) => setEngine(e.target.value as "baseline" | "route3")}>
                <option value="route3">全局优化（集合覆盖 v2）</option>
                <option value="baseline">老求解器（分级放松 MILP）</option>
              </select>
            </label>
            <button className="primary-button run-button" onClick={runSolve} disabled={!selectedSample || solving}>
              {solving ? `求解中 ${elapsed.toFixed(1)}s` : "开始排料并对比"}
            </button>
          </div>
          {solveError && <p className="run-error">{solveError}</p>}
        </section>

        {!result && !solving && (
          <section className="empty-hero">
            <div className="empty-hero-icon" aria-hidden="true">
              ⤴
            </div>
            <h3>选择一条历史用例后开始排料</h3>
            <p>本系统将重新求解该用例，并与旧软件的历史结果逐项对比：利用率、焊口、领用原管、切法与拼法。</p>
          </section>
        )}

        {solving && (
          <section className="empty-hero solving">
            <div className="run-spinner" aria-hidden="true" />
            <h3>正在求解 {elapsed.toFixed(1)} 秒</h3>
            <p>正在生成合法拼法与切法、优化利用率与焊口数量，并进行独立校验。</p>
          </section>
        )}

        {result && (
          <>
            <section className="task-header">
              <div>
                <div className="eyebrow">
                  <span className="status-dot" />
                  {passed ? "计算完成 · 已通过独立校验" : "计算完成 · 校验存在告警"}
                </div>
                <h1>两套排料方案对比</h1>
                <span className="task-id">
                  用例 {selectedSample?.com ?? ""} · {materialLabel} / {specLabel}
                </span>
              </div>
              <div className="task-actions">
                <button className="secondary-button" onClick={() => document.getElementById("details")?.scrollIntoView({ behavior: "smooth" })}>
                  查看生产明细
                </button>
                <button className="primary-button" onClick={() => downloadComparison(result, systemMetrics, legacyMetrics, materialLabel, specLabel)}>
                  <span className="button-icon" aria-hidden="true">
                    ↓
                  </span>
                  导出对比 JSON
                </button>
              </div>
            </section>

            {hasComparison && (
              <>
                <section className={`verdict-card`}>
                  <div className="verdict-copy">
                    <span className="section-kicker">本次对比结论</span>
                    <h2>{verdictTitle}</h2>
                    <p>
                      在 {rows.length} 项核心指标中，本系统领先 {wins} 项，旧软件领先 {losses} 项。A = 本系统当前求解结果，B = 旧软件历史结果。
                    </p>
                    <div className="scoreline">
                      <div>
                        <span>方案 A</span>
                        <strong>{wins}</strong>
                      </div>
                      <i>
                        <span style={{ width: `${rows.length ? (wins / rows.length) * 100 : 0}%` }} />
                      </i>
                      <div className="right">
                        <strong>{losses}</strong>
                        <span>方案 B</span>
                      </div>
                    </div>
                  </div>
                  <div className="verdict-insights">
                    {insights.length ? (
                      insights.map((ins) => (
                        <div key={ins.key}>
                          <span className={`insight-icon ${ins.iconTone}`}>{ins.betterHigh ? "▲" : "▼"}</span>
                          <p>
                            <strong>
                              {ins.label}：{ins.better}更优
                            </strong>
                            <small>{ins.delta}</small>
                          </p>
                        </div>
                      ))
                    ) : (
                      <div>
                        <span className="insight-icon blue">=</span>
                        <p>
                          <strong>两套方案指标一致</strong>
                          <small>无显著差异</small>
                        </p>
                      </div>
                    )}
                  </div>
                </section>

                <section className="comparison-board" aria-label="方案核心指标对比">
                  <div className="comparison-head">
                    <div className="metric-label-head">核心指标</div>
                    <article className="scheme-head system">
                      <div>
                        <span className="scheme-letter">A</span>
                        <div>
                          <strong>本系统方案</strong>
                          <small>当前计算 · {elapsed.toFixed(2)} 秒</small>
                        </div>
                      </div>
                      <MiniRing value={systemUtil} color="#258dff" />
                    </article>
                    <div className="versus">VS</div>
                    <article className="scheme-head legacy">
                      <div>
                        <span className="scheme-letter">B</span>
                        <div>
                          <strong>旧软件方案</strong>
                          <small>已载入对照数据</small>
                        </div>
                      </div>
                      <MiniRing value={legacyUtil} color="#12a87b" />
                    </article>
                  </div>

                  <div className="metric-rows">
                    {rows.map((row) => (
                      <div className="metric-row" key={row.key}>
                        <div className="metric-name">
                          <strong>{row.label}</strong>
                          <span>{row.hint}</span>
                        </div>
                        <div className={`metric-value system ${row.verdict === "win" ? "winner" : ""}`}>
                          <strong>{formatCompareValue(row.system, row.kind)}</strong>
                          {row.verdict === "win" && <WinnerBadge type="system" />}
                        </div>
                        <div className="metric-delta">
                          <span className={row.verdict}>
                            {row.meaningful ? formatCompareDelta(row.delta, row.kind) : "持平"}
                          </span>
                        </div>
                        <div className={`metric-value legacy ${row.verdict === "lose" ? "winner" : ""}`}>
                          <strong>{formatCompareValue(row.legacy, row.kind)}</strong>
                          {row.verdict === "lose" && <WinnerBadge type="legacy" />}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              </>
            )}

            <section className="material-strip">
              <div>
                <span>材质</span>
                <strong>{materialLabel}</strong>
              </div>
              <div>
                <span>规格</span>
                <strong>{specLabel}</strong>
              </div>
              <div>
                <span>需求总长</span>
                <strong>{formatLength(summaryDemand)} mm</strong>
              </div>
              <div>
                <span>材质池</span>
                <strong>{systemGroups.length} 个</strong>
              </div>
              <div className="pool-state">
                <span>校验状态</span>
                <strong>
                  <i />
                  {passed ? "通过" : "有告警"}
                </strong>
              </div>
            </section>

            <section className="details-card" id="details">
              <div className="details-title">
                <div>
                  <span className="section-kicker">生产执行明细</span>
                  <h2>
                    {materialLabel} / {specLabel}
                  </h2>
                </div>
                <div className="legend">
                  <span>
                    <i className="legend-a" />
                    方案 A
                  </span>
                  <span>
                    <i className="legend-b" />
                    方案 B
                  </span>
                </div>
              </div>

              <div className="tabs" role="tablist" aria-label="结果分类">
                {(
                  [
                    ["compare", "差异总览", String(rows.length)],
                    ["cut", "原材料切法", cutCountLabel],
                    ["splice", "管段拼法", spliceCountLabel],
                    ["audit", "校验与归一化", String(normalizationCount)],
                  ] as const
                ).map(([id, label, count]) => (
                  <button
                    key={id}
                    className={tab === id ? "active" : ""}
                    onClick={() => setTab(id)}
                    role="tab"
                    aria-selected={tab === id}
                  >
                    {label}
                    <span>{count}</span>
                  </button>
                ))}
              </div>

              <div className="tab-panel">
                {tab === "compare" && (
                  <div className="difference-list">
                    {rows.map((row, index) => (
                      <article key={row.key}>
                        <div className={`rank ${row.verdict === "lose" ? "legacy" : "system"}`}>{index + 1}</div>
                        <div>
                          <strong>{row.label}</strong>
                          <span>{row.hint}</span>
                        </div>
                        <div className="compare-numbers">
                          <b>{formatCompareValue(row.system, row.kind)}</b>
                          <i>→</i>
                          <b>{formatCompareValue(row.legacy, row.kind)}</b>
                        </div>
                        {row.verdict === "tie" ? (
                          <span className="winner-badge legacy">持平</span>
                        ) : (
                          <WinnerBadge type={row.verdict === "win" ? "system" : "legacy"} />
                        )}
                      </article>
                    ))}
                  </div>
                )}

                {tab === "cut" && (
                  <div className="cut-panel">
                    <div className="panel-note">
                      <span>i</span>
                      <p>
                        <strong>按母材长度对照切法</strong>
                        每个长度分组内，左侧列出 A（本系统）在该长度上的全部切法明细，右侧列出 B（旧软件）的全部切法；两侧切法种类数可以不同。
                      </p>
                    </div>

                    {cutLengths.length === 0 && <div className="cutting-empty">暂无切法记录</div>}

                    {cutLengths.map((length) => {
                      const sysBlock = sysCutMap.get(length);
                      const legBlock = legacyCutMap.get(length);
                      return (
                        <section className="cut-length-block" key={`len-${length}`}>
                          <header className="cut-length-head">
                            <strong>母材 {formatLength(length)} mm</strong>
                            <span className="cut-length-count">
                              A {sysBlock ? sysBlock.methods.length : 0} 种 · {sysBlock ? sysBlock.totalStocks : 0} 根
                              {"  /  "}
                              B {legBlock ? legBlock.methods.length : 0} 种 · {legBlock ? legBlock.totalStocks : 0} 根
                            </span>
                          </header>
                          <div className="cut-length-cols">
                            <div className="cut-length-col system">
                              <div className="cut-col-title">
                                <span className="scheme-letter">A</span> 本系统 · {sysBlock ? sysBlock.methods.length : 0} 种
                              </div>
                              {sysBlock && sysBlock.methods.length ? (
                                sysBlock.methods.map((method) => (
                                  <CuttingMethodCard key={method.id} method={method} scheme="system" />
                                ))
                              ) : (
                                <div className="cutting-empty">A 在该长度上无切法</div>
                              )}
                            </div>
                            <div className="cut-length-col legacy">
                              <div className="cut-col-title">
                                <span className="scheme-letter">B</span> 旧软件 · {legBlock ? legBlock.methods.length : 0} 种
                              </div>
                              {legBlock && legBlock.methods.length ? (
                                legBlock.methods.map((method) => (
                                  <CuttingMethodCard key={method.id} method={method} scheme="legacy" />
                                ))
                              ) : (
                                <div className="cutting-empty">B 在该长度上无切法</div>
                              )}
                            </div>
                          </div>
                        </section>
                      );
                    })}
                  </div>
                )}

                {tab === "splice" && (
                  <div className="splice-panel">
                    <div className="panel-note">
                      <span>i</span>
                      <p>
                        <strong>按管圈标识对照拼法</strong>
                        每个管圈分组内，左侧为 A（本系统）的拼接方式，右侧为 B（旧软件）的拼接方式。
                      </p>
                    </div>

                    {weldIdentities.length === 0 && <div className="cutting-empty">暂无拼法记录</div>}

                    {weldIdentities.map((identity) => {
                      const sysBlock = sysWeldMap.get(identity);
                      const legBlock = legacyWeldMap.get(identity);
                      return (
                        <section className="cut-length-block" key={`pipe-${identity}`}>
                          <header className="cut-length-head">
                            <strong>管圈 {identity}</strong>
                            <span className="cut-length-count">
                              A {sysBlock ? sysBlock.totalPipes : 0} 根 / B {legBlock ? legBlock.totalPipes : 0} 根
                            </span>
                          </header>
                          <div className="cut-length-cols">
                            <div className="cut-length-col system">
                              <div className="cut-col-title">
                                <span className="scheme-letter">A</span> 本系统
                              </div>
                              <SpliceMethodList block={sysBlock} scheme="A" />
                            </div>
                            <div className="cut-length-col legacy">
                              <div className="cut-col-title">
                                <span className="scheme-letter">B</span> 旧软件
                              </div>
                              <SpliceMethodList block={legBlock} scheme="B" />
                            </div>
                          </div>
                        </section>
                      );
                    })}
                  </div>
                )}

                {tab === "audit" && (
                  <AuditPanel result={result} systemGroups={systemGroups} normalizationCount={normalizationCount} passed={passed} />
                )}
              </div>
            </section>

            <footer>
              <span>用例 {selectedSample?.id ?? ""}</span>
              <span>数据来自本系统实时求解 + 旧软件历史结果</span>
            </footer>
          </>
        )}
      </div>
    </main>
  );
}

function AuditPanel({
  result,
  systemGroups,
  normalizationCount,
  passed,
}: {
  result: SolveResponse;
  systemGroups: NestingGroup[];
  normalizationCount: number;
  passed: boolean;
}) {
  const verification = firstDefined<Record<string, unknown>>(result, ["verification", "VerificationInfo"], {}) || {};
  const errorList = asArray<unknown>(firstDefined(verification, ["errors", "issues"], []));
  const forbiddenNorm = systemGroups.reduce((sum, g) => {
    const norms = groupCollections(g).normalizations;
    return sum + norms.filter((n) => /禁焊/.test(n.rule)).length;
  }, 0);
  const scalarNorm = normalizationCount - forbiddenNorm;
  return (
    <div className="audit-grid">
      <article>
        <span className="audit-check">✓</span>
        <div>
          <strong>长度向上取整</strong>
          <p>{scalarNorm} 项小数长度已按生产规则调整，原始输入值保留可追溯。</p>
        </div>
        <b>{scalarNorm} 项</b>
      </article>
      <article>
        <span className="audit-check">✓</span>
        <div>
          <strong>禁焊区边界外扩</strong>
          <p>起点向下、终点向上取整，确保实际生产不侵入禁焊区。</p>
        </div>
        <b>{forbiddenNorm} 项</b>
      </article>
      <article>
        <span className="audit-check">{passed ? "✓" : "!"}</span>
        <div>
          <strong>结果一致性校验</strong>
          <p>
            {passed
              ? "需求量、料段长度、切损及拼接关系均已通过独立校验。"
              : `独立校验发现 ${errorList.length} 项问题，请在投产前处理。`}
          </p>
        </div>
        <b>{passed ? "通过" : `${errorList.length} 项`}</b>
      </article>
    </div>
  );
}

function downloadComparison(
  result: SolveResponse,
  system: ComparableMetrics,
  legacy: ReturnType<typeof groupMetrics>,
  material: string,
  specification: string,
) {
  const payload = {
    material,
    specification,
    generatedAt: new Date().toISOString(),
    system: {
      utilization: system.utilization,
      welds: system.weldingJoints,
      stockLength: system.usedStockLength,
      cuttingTypes: system.cuttingPatternTypes,
      spliceTypes: system.weldingPatternTypes,
    },
    legacy: {
      utilization: legacy.utilization,
      welds: legacy.weldingJoints,
      stockLength: legacy.usedStockLength,
      cuttingTypes: legacy.cuttingPatternTypes,
      spliceTypes: legacy.weldingPatternTypes,
    },
    raw: result,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `排料方案对比-${material}-${specification}.json`.replace(/[^\w.\-一-龥]/g, "_");
  anchor.click();
  URL.revokeObjectURL(url);
}

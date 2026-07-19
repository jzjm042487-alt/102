// 后端求解服务与样例库的客户端。本地开发时前端跑在 vinext dev（默认 5173），
// 求解服务是同仓的 FastAPI（默认 127.0.0.1:8000，CORS 放开）。可用环境变量覆盖。

import type { AnyRecord, NestingGroup } from "./nesting";
import { normalizeLegacyGroup } from "./nesting";

const API_BASE =
  (typeof process !== "undefined" && process.env?.NEXT_PUBLIC_NESTING_API) ||
  "http://127.0.0.1:8000";

const SOLVE_URL = `${API_BASE.replace(/\/$/, "")}/api/v1/solve`;
const COMPARE_LOG_URL = `${API_BASE.replace(/\/$/, "")}/api/v1/compare-log`;
const SAMPLES_URL = "/samples.json";

export interface SampleRecord {
  id: string;
  com: string;
  figures?: string[];
  material: string;
  spec: string;
  pipe_count: number;
  pipe_demand_total?: number;
  problem: AnyRecord;
  legacy: AnyRecord;
}

export interface SamplesLibrary {
  version?: string;
  count?: number;
  samples: SampleRecord[];
}

export async function loadSamples(): Promise<SampleRecord[]> {
  const response = await fetch(SAMPLES_URL, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`样例库加载失败 HTTP ${response.status}`);
  const library = (await response.json()) as SamplesLibrary;
  return Array.isArray(library.samples) ? library.samples : [];
}

export interface SolveResponse extends AnyRecord {
  status?: string;
  groups?: NestingGroup[];
  verification?: AnyRecord;
  summary?: AnyRecord;
}

export async function solveProblem(
  problem: AnyRecord,
  timeLimitSeconds?: number | null,
  signal?: AbortSignal,
  engine?: "baseline" | "route3" | "v4" | "global",
): Promise<SolveResponse> {
  const params = new URLSearchParams();
  if (timeLimitSeconds && Number.isFinite(timeLimitSeconds)) {
    params.set(
      "time_limit_seconds",
      String(Math.min(3600, Math.max(1, Math.round(timeLimitSeconds)))),
    );
  }
  if (engine) params.set("engine", engine);
  const query = params.toString();
  const url = query ? `${SOLVE_URL}?${query}` : SOLVE_URL;
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(problem),
    signal,
  });
  const raw = await response.text();
  let result: SolveResponse | null = null;
  if (raw) {
    try {
      result = JSON.parse(raw) as SolveResponse;
    } catch {
      throw new Error(`计算服务返回了非 JSON 内容（HTTP ${response.status}）`);
    }
  }
  if (!response.ok) {
    const detail =
      (result && (result.detail ?? result.message ?? result.error)) || `计算服务返回 HTTP ${response.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (!result || typeof result !== "object") throw new Error("计算服务未返回结果内容");
  return result;
}

export function legacyGroupFromSample(sample: SampleRecord): NestingGroup | null {
  return normalizeLegacyGroup(sample.legacy);
}

// 落盘一次「本系统 vs 旧软件」对比结果，供事后分析差距。失败不影响主流程。
export async function logComparison(record: AnyRecord): Promise<void> {
  try {
    await fetch(COMPARE_LOG_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(record),
    });
  } catch {
    // 日志失败静默：不打断用户对比流程。
  }
}

// 数据模型工具：从原生前端 app.js 移植而来，负责把 FastAPI 求解结果与旧软件历史
// 结果归一化成统一的 group / metrics / patterns 结构，供 React 页面渲染。

export type AnyRecord = Record<string, unknown>;

export type Pattern = AnyRecord;

export interface GroupMetrics {
  utilization: number;
  weldingJoints: number;
  weldingPatternTypes: number;
  cuttingPatternTypes: number;
  usedStockLength: number;
  demandLength: number;
  targetReached: boolean;
  status: string;
}

export interface NestingGroup extends AnyRecord {
  material?: string;
  specifications?: string;
  status?: string;
  metrics?: AnyRecord;
  cutting_patterns?: Pattern[];
  welding_patterns?: Pattern[];
  is_legacy_group?: boolean;
  legacy_result?: boolean;
}

export function asArray<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

export function firstDefined<T = unknown>(
  object: unknown,
  keys: string[],
  fallback?: T,
): T {
  if (!object || typeof object !== "object") return fallback as T;
  const record = object as AnyRecord;
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key] as T;
  }
  return fallback as T;
}

export function toNumber(value: unknown, fallback = 0): number {
  const parsed =
    typeof value === "number" ? value : Number(String(value ?? "").trim());
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function toBoolean(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "1", "yes", "passed", "success"].includes(normalized)) return true;
    if (["false", "0", "no", "failed", "failure"].includes(normalized)) return false;
  }
  return fallback;
}

export function formatNumber(value: unknown, maximumFractionDigits = 1): string {
  const number = toNumber(value, NaN);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits,
    minimumFractionDigits: 0,
  }).format(number);
}

export function formatLength(value: unknown): string {
  return formatNumber(value, 1);
}

export function normalizeRate(value: unknown): number {
  const rate = toNumber(value, 0);
  return rate > 0 && rate <= 1.000001 ? rate * 100 : rate;
}

export function formatRate(value: unknown): string {
  return `${formatNumber(normalizeRate(value), 4)}%`;
}

export function clampRate(value: unknown): number {
  return Math.min(100, Math.max(0, normalizeRate(value)));
}

export function normalizeParts(value: unknown): number[] {
  if (Array.isArray(value)) {
    return value.map((item) => toNumber(item, NaN)).filter(Number.isFinite);
  }
  if (typeof value === "string") {
    return value
      .trim()
      .split(/[\s,+|]+/)
      .map((item) => toNumber(item, NaN))
      .filter(Number.isFinite);
  }
  return [];
}

export function extractIssues(
  source: unknown,
  kind: "all" | "errors" | "warnings" = "all",
): string[] {
  if (!source || typeof source !== "object") return [];
  const record = source as AnyRecord;
  const errorKeys = ["errors", "issues", "violations", "Errors", "Problems"];
  const warningKeys = ["warnings", "Warnings"];
  const keys =
    kind === "errors"
      ? errorKeys
      : kind === "warnings"
        ? [...warningKeys, "issues"]
        : [...errorKeys, ...warningKeys];
  const items: unknown[] = [];
  keys.forEach((key) => {
    const value = record[key];
    const candidates = Array.isArray(value)
      ? value
      : value !== undefined && value !== null && value !== ""
        ? [value]
        : [];
    candidates.forEach((item) => {
      if (key === "issues" && item && typeof item === "object") {
        const severity = String((item as AnyRecord).severity || "error").toLowerCase();
        if (kind === "errors" && severity === "warning") return;
        if (kind === "warnings" && severity !== "warning") return;
      }
      items.push(item);
    });
  });
  return items.map((item) => {
    if (typeof item === "string") return item;
    if (typeof item === "number" || typeof item === "boolean") return String(item);
    return firstDefined<string>(
      item,
      ["message", "msg", "detail", "description", "code"],
      JSON.stringify(item),
    );
  });
}

export function getVerification(result: unknown): AnyRecord {
  return firstDefined<AnyRecord>(result, ["verification", "VerificationInfo"], {}) || {};
}

export function verificationPassed(result: unknown): boolean {
  const verification = getVerification(result);
  const explicit = firstDefined(verification, ["passed", "Passed", "is_valid", "valid"]);
  if (explicit !== undefined) return toBoolean(explicit, false);
  return extractIssues(verification, "errors").length === 0;
}

function patternSignature(pattern: Pattern, type: "cutting" | "welding"): string {
  const parts = normalizeParts(firstDefined(pattern, ["parts", "Parts", "Part"], []));
  if (type === "cutting") {
    const stockLength = toNumber(
      firstDefined(pattern, ["stock_length", "StockLength", "material_length"]),
      0,
    );
    const canonicalParts = [...parts].sort((left, right) => left - right);
    return `${stockLength}|${canonicalParts.join(",")}`;
  }
  return parts.join(",");
}

export function patternGroupKey(pattern: Pattern, type: "cutting" | "welding"): string {
  const typeKeys =
    type === "cutting"
      ? ["cutting_pattern_id", "cut_pattern_id", "CuttingPatternID", "CutPatternID"]
      : ["welding_pattern_id", "weld_pattern_id", "WeldingPatternID", "WeldPatternID"];
  const explicit = firstDefined<string | number>(pattern, [
    "pattern_id",
    "patternId",
    "PatternID",
    ...typeKeys,
  ]);
  if (explicit !== undefined && String(explicit).trim()) return `id:${String(explicit).trim()}`;
  return `signature:${patternSignature(pattern, type)}`;
}

export interface GroupedPattern {
  pattern: Pattern;
  originalIndex: number;
  id: string;
  groupIndex: number;
  groupSize: number;
  isGroupStart: boolean;
}

export function groupedPatterns(
  patterns: Pattern[],
  type: "cutting" | "welding",
): GroupedPattern[] {
  const groups = new Map<string, { pattern: Pattern; originalIndex: number }[]>();
  patterns.forEach((pattern, index) => {
    const key = patternGroupKey(pattern, type);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push({ pattern, originalIndex: index });
  });
  const collator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
  return Array.from(groups.entries())
    .sort(([left], [right]) => collator.compare(left, right))
    .flatMap(([key, entries], typeIndex) => {
      const prefix = type === "cutting" ? "CP" : "WP";
      const id = key.startsWith("id:")
        ? key.slice(3)
        : `${prefix}-${String(typeIndex + 1).padStart(3, "0")}`;
      return entries.map((entry, groupIndex) => ({
        ...entry,
        id,
        groupIndex,
        groupSize: entries.length,
        isGroupStart: groupIndex === 0,
      }));
    });
}

export function patternTypeCount(patterns: Pattern[], type: "cutting" | "welding"): number {
  return new Set(patterns.map((pattern) => patternGroupKey(pattern, type))).size;
}

export function groupMetrics(group: NestingGroup | null | undefined): GroupMetrics {
  const metrics = firstDefined<AnyRecord>(group, ["metrics", "summary", "GeneralInfo"], {}) || {};
  const cuttingPatterns = asArray<Pattern>(
    firstDefined(group, ["cutting_patterns", "cuttingPatterns", "CuttingPatterns"], []),
  );
  const weldingPatterns = asArray<Pattern>(
    firstDefined(group, ["welding_patterns", "weldingPatterns", "WeldingPatterns"], []),
  );

  const derivedUsedStock = cuttingPatterns.reduce((sum, item) => {
    const stockLength = toNumber(firstDefined(item, ["stock_length", "StockLength", "material_length"]));
    const quantity = toNumber(firstDefined(item, ["quantity", "count", "stock_quantity"], 1));
    return sum + stockLength * quantity;
  }, 0);
  const derivedDemand = weldingPatterns.reduce((sum, item) => {
    const parts = normalizeParts(firstDefined(item, ["parts", "Parts", "Part"], []));
    const pipeLength = toNumber(
      firstDefined(item, ["pipe_length", "PipeLength"]),
      parts.reduce((a, b) => a + b, 0),
    );
    const quantity = toNumber(firstDefined(item, ["quantity", "count", "PipeQuantity"], 1));
    return sum + pipeLength * quantity;
  }, 0);
  const usedStockLength = toNumber(
    firstDefined(metrics, ["used_stock_length", "stock_used_length", "StockLength_Of_SingleMaterialSpecification"]),
    derivedUsedStock,
  );
  const demandLength = toNumber(
    firstDefined(metrics, ["demand_length", "pipe_demand_length", "PipeLength_Of_SingleMaterialSpecification"]),
    derivedDemand,
  );
  const utilization = toNumber(
    firstDefined(metrics, ["utilization_rate", "util_rate", "UtilizationRate"]),
    usedStockLength > 0 ? demandLength / usedStockLength : 0,
  );
  const weldingJoints = toNumber(
    firstDefined(metrics, ["welding_joint_quantity", "welding_joints", "WeldingJointQuantity"]),
    weldingPatterns.reduce((sum, item) => {
      const quantity = toNumber(firstDefined(item, ["quantity", "count"], 1));
      const joints = toNumber(
        firstDefined(item, ["joint_count", "welding_joint_count"]),
        Math.max(0, normalizeParts((item as AnyRecord).parts).length - 1),
      );
      return sum + quantity * joints;
    }, 0),
  );
  const derivedWeldingPatternTypes = patternTypeCount(weldingPatterns, "welding");
  const derivedCuttingPatternTypes = patternTypeCount(cuttingPatterns, "cutting");
  const weldingPatternTypes = weldingPatterns.length
    ? derivedWeldingPatternTypes
    : toNumber(
        firstDefined(metrics, ["welding_pattern_type_quantity", "welding_pattern_types", "WeldingPatternTypeQuantity"]),
        0,
      );
  const cuttingPatternTypes = cuttingPatterns.length
    ? derivedCuttingPatternTypes
    : toNumber(
        firstDefined(metrics, ["cutting_pattern_type_quantity", "cutting_pattern_types", "CuttingPatternTypeQuantity"]),
        0,
      );
  const targetReachedRaw = firstDefined(metrics, ["target_reached", "TargetReached"]);
  const targetReached =
    targetReachedRaw !== undefined
      ? toBoolean(targetReachedRaw, false)
      : normalizeRate(utilization) >= 99.25;
  return {
    utilization,
    weldingJoints,
    weldingPatternTypes,
    cuttingPatternTypes,
    usedStockLength,
    demandLength,
    targetReached,
    status: firstDefined(
      group,
      ["status"],
      firstDefined(metrics, ["solve_status"], targetReached ? "TARGET_REACHED" : "FEASIBLE"),
    ),
  };
}

export function groupIdentity(group: NestingGroup): { material: string; specifications: string } {
  const material = firstDefined(group, ["material", "Material", "material_name"], "未标明材质");
  const specifications = firstDefined(
    group,
    ["specifications", "specification", "Specifications", "spec"],
    "未标明规格",
  );
  return { material: String(material), specifications: String(specifications) };
}

export interface GroupCollections {
  cutting: Pattern[];
  welding: Pattern[];
  unused: Pattern[];
  remnants: Pattern[];
  warnings: string[];
  errors: string[];
  normalizations: NormalizationRecord[];
}

export function groupCollections(group: NestingGroup): GroupCollections {
  const cutting = asArray<Pattern>(
    firstDefined(group, ["cutting_patterns", "cuttingPatterns", "CuttingPatterns"], []),
  );
  const welding = asArray<Pattern>(
    firstDefined(group, ["welding_patterns", "weldingPatterns", "WeldingPatterns"], []),
  );
  const unused = asArray<Pattern>(
    firstDefined(group, ["unused_materials", "unusedMaterials", "UnusedMaterials"], []),
  );
  const remnants = asArray<Pattern>(
    firstDefined(group, ["generated_remnants", "generatedRemnants", "remnants", "ReusableRemnants"], []),
  );
  const warnings = extractIssues(group, "warnings");
  const errors = extractIssues(group, "errors");
  const normalizations = normalizeInputNormalizations(group);
  return { cutting, welding, unused, remnants, warnings, errors, normalizations };
}

export interface NormalizationRecord {
  path: string;
  original: unknown;
  normalized: unknown;
  rule: string;
}

export function normalizeInputNormalizations(group: unknown): NormalizationRecord[] {
  const raw = firstDefined(
    group,
    ["input_normalizations", "inputNormalizations", "normalizations", "InputNormalizations"],
    [],
  );
  let entries: { record: unknown; fallbackPath: string }[] = [];
  if (Array.isArray(raw)) {
    entries = raw.map((record, index) => ({ record, fallbackPath: `input_normalizations[${index}]` }));
  } else if (raw && typeof raw === "object") {
    const nested = firstDefined(raw, ["items", "records", "changes", "normalizations"]);
    if (Array.isArray(nested)) {
      entries = nested.map((record, index) => ({ record, fallbackPath: `input_normalizations[${index}]` }));
    } else {
      entries = Object.entries(raw as AnyRecord).map(([path, record]) => ({ record, fallbackPath: path }));
    }
  }
  return entries.map(({ record, fallbackPath }) => {
    if (!record || typeof record !== "object" || Array.isArray(record)) {
      return { path: fallbackPath, original: "—", normalized: record, rule: "按生产规则归一化" };
    }
    const path = String(firstDefined(record, ["path", "field_path", "input_path", "field", "name"], fallbackPath));
    return {
      path,
      original: firstDefined(record, ["original", "original_value", "before", "from", "raw_value", "source_value"], "—"),
      normalized: firstDefined(record, ["normalized", "normalized_value", "after", "to", "result", "target_value"], "—"),
      rule: normalizationRuleLabel(
        firstDefined(record, ["rule", "rule_name", "method", "strategy", "normalization_rule"], ""),
        path,
      ),
    };
  });
}

function normalizationRuleLabel(rule: unknown, path = ""): string {
  const raw = String(rule || "").trim();
  const normalized = raw.toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
  const normalizedPath = String(path).toLowerCase();
  if (/unweldable_area.*\[0\]$/.test(normalizedPath)) return "禁焊区起点向下取整";
  if (/unweldable_area.*\[1\]$/.test(normalizedPath)) return "禁焊区终点向上取整";
  if (/forbidden.*start|unweldable.*start|interval.*start|floor.*start|start.*floor/.test(normalized))
    return "禁焊区起点向下取整";
  if (/forbidden.*end|unweldable.*end|interval.*end|ceil.*end|end.*ceil/.test(normalized))
    return "禁焊区终点向上取整";
  if (/ceil|round_up|upward|scalar/.test(normalized)) return "标量长度向上取整";
  return raw || "按生产规则归一化";
}

export function normalizeLegacyGroup(result: unknown): NestingGroup | null {
  const problem = firstDefined<AnyRecord>(result, ["OriginalProblem", "original_problem"], {}) || {};
  const general = firstDefined<AnyRecord>(result, ["GeneralInfo", "general_info"], {}) || {};
  const record = (result as AnyRecord) || {};
  const legacyResult =
    record.Result && typeof record.Result === "object" ? (record.Result as AnyRecord) : {};
  const weldingRoot = firstDefined<AnyRecord>(legacyResult, ["WeldingPattern", "welding_pattern"], {}) || {};
  const cuttingRoot = firstDefined<AnyRecord>(legacyResult, ["CuttingPattern", "cutting_pattern"], {}) || {};
  const weldingPipes = asArray<AnyRecord>(firstDefined(weldingRoot, ["WeldingPipe", "welding_pipe"], []));
  const cuttingPipes = asArray<AnyRecord>(firstDefined(cuttingRoot, ["CuttingPipe", "cutting_pipe"], []));
  if (!weldingPipes.length && !cuttingPipes.length) return null;

  let weldingIndex = 0;
  const weldingPatterns: Pattern[] = [];
  weldingPipes.forEach((pipe) => {
    asArray<AnyRecord>(firstDefined(pipe, ["Pattern", "patterns"], [])).forEach((pattern) => {
      const parts = normalizeParts(firstDefined(pattern, ["Part", "parts"], []));
      let position = 0;
      const weldPositions = parts.slice(0, -1).map((part) => {
        position += part;
        return position;
      });
      weldingIndex += 1;
      const figureNumber = firstDefined(pipe, ["FigureNumber", "figure_number"], "");
      const jlxh = firstDefined(pipe, ["jlxh"], "");
      const cubeNo = firstDefined(pipe, ["InPipeNumber", "cube_no"], "");
      weldingPatterns.push({
        pipe_id: [figureNumber, jlxh, cubeNo].filter(Boolean).join("|") || `legacy-pipe-${weldingIndex}`,
        figure_number: figureNumber,
        jlxh,
        cube_no: cubeNo,
        pipe_length: firstDefined(pipe, ["Length", "pipe_length"], parts.reduce((sum, part) => sum + part, 0)),
        parts,
        weld_positions: weldPositions,
        quantity: firstDefined(pattern, ["Number", "quantity"], 1),
        joint_count: Math.max(0, parts.length - 1),
        legacy_result: true,
      });
    });
  });

  const cuttingPatterns: Pattern[] = cuttingPipes.map((pattern) => {
    const parts = normalizeParts(firstDefined(pattern, ["Part", "parts"], []));
    return {
      stock_length: firstDefined(pattern, ["Length", "stock_length"]),
      parts,
      quantity: firstDefined(pattern, ["Number", "quantity"], 1),
      kerf_loss_per_stock: firstDefined(pattern, ["KerfLoss", "kerf_loss_per_stock"], 0),
      remainder_per_stock: firstDefined(pattern, ["TrimLoss", "remainder_per_stock"], 0),
      used_length_per_stock: parts.reduce((sum, part) => sum + part, 0),
      legacy_result: true,
    };
  });

  const utilization = firstDefined(general, ["UtilRate", "UtilizationRate", "utilization_rate"], 0);
  const targetRate = firstDefined(problem, ["Target_Util_Rate", "target_util_rate"], 0.9925);
  const targetReached = normalizeRate(utilization) >= normalizeRate(targetRate);
  return {
    material: firstDefined(problem, ["material", "Material"], firstDefined(general, ["Material"], "未标明材质")),
    specifications: firstDefined(
      problem,
      ["specifications", "Specifications"],
      firstDefined(general, ["Specification"], "未标明规格"),
    ),
    status: targetReached ? "TARGET_REACHED" : "FEASIBLE",
    metrics: {
      utilization_rate: utilization,
      demand_length: firstDefined(general, ["PipeLength_Of_SingleMaterialSpecification", "demand_length"], 0),
      used_stock_length: firstDefined(general, ["StockLength_Of_SingleMaterialSpecification", "used_stock_length"], 0),
      welding_joint_quantity: firstDefined(general, ["WeldingJointQuantity", "welding_joint_quantity"], 0),
      welding_pattern_type_quantity: patternTypeCount(weldingPatterns, "welding"),
      cutting_pattern_type_quantity: patternTypeCount(cuttingPatterns, "cutting"),
      target_reached: targetReached,
    },
    welding_patterns: weldingPatterns,
    cutting_patterns: cuttingPatterns,
    unused_materials: asArray(firstDefined(legacyResult, ["UnusedMaterials", "unused_materials"], [])),
    generated_remnants: [],
    warnings: [],
    legacy_result: true,
  };
}

export function normalizeGroups(result: unknown): NestingGroup[] {
  const direct = firstDefined(result, ["groups", "material_groups", "results", "material_results"], []);
  if (Array.isArray(direct) && direct.length) return direct as NestingGroup[];
  if (direct && typeof direct === "object" && !Array.isArray(direct)) {
    const values = Object.values(direct as AnyRecord);
    if (values.length) return values as NestingGroup[];
  }
  const legacyGroup = normalizeLegacyGroup(result);
  return legacyGroup ? [legacyGroup] : [];
}

// 切损：原管长 - 有效料段 - 切缝。
export function patternLoss(pattern: Pattern): number {
  const stockLength = toNumber(firstDefined(pattern, ["stock_length", "StockLength", "material_length"]), 0);
  const parts = normalizeParts(firstDefined(pattern, ["parts", "Parts", "Part"], []));
  const kerf = toNumber(firstDefined(pattern, ["kerf_loss_per_stock", "kerf_loss"], 0), 0);
  return Math.max(0, stockLength - parts.reduce((sum, part) => sum + part, 0) - kerf);
}

// 每种切法取首条代表，按切损升序，便于 A/B 逐行对照。
export function sortedCuts(cuts: Pattern[]): Pattern[] {
  const grouped = groupedPatterns(cuts, "cutting");
  const byId = new Map<string, Pattern>();
  grouped.forEach((entry) => {
    if (!byId.has(entry.id)) byId.set(entry.id, { ...entry.pattern, pattern_id: entry.id });
  });
  return Array.from(byId.values()).sort((left, right) => patternLoss(left) - patternLoss(right));
}

export function stockLengthOf(pattern: Pattern): number {
  return toNumber(firstDefined(pattern, ["stock_length", "StockLength", "material_length"]), 0);
}

export interface CutMethod {
  pattern: Pattern;
  id: string;
  quantity: number;
  loss: number;
}

export interface StockLengthCuts {
  stockLength: number;
  methods: CutMethod[];
  totalStocks: number;
}

// 按母材长度汇总切法：同一长度下，去重出每种切法明细并累加执行根数。
export function cutsByStockLength(cuts: Pattern[]): StockLengthCuts[] {
  const byLength = new Map<number, Map<string, CutMethod>>();
  cuts.forEach((pattern) => {
    const length = stockLengthOf(pattern);
    const key = patternSignature(pattern, "cutting");
    if (!byLength.has(length)) byLength.set(length, new Map());
    const bucket = byLength.get(length)!;
    const quantity = toNumber(firstDefined(pattern, ["quantity", "count", "stock_quantity"], 1), 1);
    const existing = bucket.get(key);
    if (existing) {
      existing.quantity += quantity;
    } else {
      bucket.set(key, {
        pattern,
        id: "",
        quantity,
        loss: patternLoss(pattern),
      });
    }
  });
  return Array.from(byLength.entries())
    .sort(([a], [b]) => a - b)
    .map(([stockLength, bucket]) => {
      const methods = Array.from(bucket.values()).sort((l, r) => l.loss - r.loss);
      methods.forEach((method, index) => {
        method.id = `${stockLength}-${String(index + 1).padStart(2, "0")}`;
      });
      return {
        stockLength,
        methods,
        totalStocks: methods.reduce((sum, method) => sum + method.quantity, 0),
      };
    });
}

export function pipeIdentityOf(pattern: Pattern): string {
  const figure = firstDefined(pattern, ["figure_number", "figureNumber", "FigureNumber", "Parent_node"], "");
  const jlxh = firstDefined(pattern, ["jlxh", "JLXH"], "");
  const cube = firstDefined(pattern, ["cube_no", "InPipeNumber", "cubeNo"], "");
  const identity = [figure, jlxh, cube].map((value) => String(value ?? "").trim()).filter(Boolean).join(" · ");
  return identity || "未标明管圈";
}

export interface PipeWeldMethod {
  pattern: Pattern;
  parts: number[];
  weldPositions: number[];
  quantity: number;
  joints: number;
}

export interface PipeWelds {
  identity: string;
  figure: string;
  methods: PipeWeldMethod[];
  totalPipes: number;
}

// 按管圈标识汇总拼法：同一管圈下，去重出每种拼接方式并累加根数。
export function weldsByPipeIdentity(welds: Pattern[]): PipeWelds[] {
  const byPipe = new Map<string, { figure: string; methods: Map<string, PipeWeldMethod> }>();
  welds.forEach((pattern) => {
    const identity = pipeIdentityOf(pattern);
    const parts = normalizeParts(firstDefined(pattern, ["parts", "Parts", "Part"], []));
    const key = parts.join(",");
    if (!byPipe.has(identity)) {
      byPipe.set(identity, {
        figure: String(firstDefined(pattern, ["figure_number", "figureNumber", "FigureNumber", "Parent_node"], identity)),
        methods: new Map(),
      });
    }
    const bucket = byPipe.get(identity)!;
    const quantity = toNumber(firstDefined(pattern, ["quantity", "count", "PipeQuantity"], 1), 1);
    const existing = bucket.methods.get(key);
    if (existing) {
      existing.quantity += quantity;
    } else {
      let running = 0;
      const weldPositions = normalizeParts(firstDefined(pattern, ["weld_positions", "welding_positions"], []));
      const positions = weldPositions.length
        ? weldPositions
        : parts.slice(0, -1).map((part) => {
            running += part;
            return running;
          });
      bucket.methods.set(key, {
        pattern,
        parts,
        weldPositions: positions,
        quantity,
        joints: Math.max(0, parts.length - 1),
      });
    }
  });
  return Array.from(byPipe.entries())
    .sort(([a], [b]) => new Intl.Collator("zh-CN", { numeric: true }).compare(a, b))
    .map(([identity, { figure, methods }]) => {
      const list = Array.from(methods.values()).sort((l, r) => l.joints - r.joints);
      return {
        identity,
        figure,
        methods: list,
        totalPipes: list.reduce((sum, method) => sum + method.quantity, 0),
      };
    });
}

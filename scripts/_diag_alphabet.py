"""诊断：对比 oracle 字母表 vs 派生字母表，看差在哪根段、为什么派生规则没覆盖到。"""
from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain import parse_problem  # noqa: E402
from _exp_alphabet import legacy_alphabet  # noqa: E402
from _exp_derived_alphabet import derive_alphabet  # noqa: E402


def main():
    path = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else "fc829dcc"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    samples = data if isinstance(data, list) else data.get("RECORDS") or data.get("samples")
    for s in samples:
        sid = (s.get("id") or s.get("ID") or "")
        if not sid.startswith(target):
            continue
        prob = json.loads(s["MOMPROBLEMJSON"]) if "MOMPROBLEMJSON" in s else s.get("problem")
        problem = parse_problem(prob)
        group = problem.groups[0]
        stock_lens = sorted({st.length for st in group.stocks})
        pipe_lens = sorted({p.length for p in group.pipes})
        print(f"id={sid[:12]}")
        print(f"  定尺: {stock_lens}")
        print(f"  管长: {pipe_lens}")
        oracle = sorted(legacy_alphabet(s))
        derived = sorted(derive_alphabet(group, cap=400))
        print(f"  oracle 字母表({len(oracle)}): {oracle}")
        print(f"  派生 字母表({len(derived)}): {derived[:40]}{'...' if len(derived)>40 else ''}")
        missing = [a for a in oracle if a not in set(derived)]
        print(f"  ★ oracle 有、派生缺的段({len(missing)}): {missing}")
        # 对每个缺失段，尝试解释它来自什么
        for m in missing:
            expl = []
            for pl in pipe_lens:
                for sl in stock_lens:
                    if pl - sl == m:
                        expl.append(f"{pl}-{sl}")
                    if sl - pl == m:
                        expl.append(f"{sl}-{pl}")
            for pl in pipe_lens:
                if pl == m:
                    expl.append(f"pipe={pl}")
            print(f"     段 {m}: 可能来源 {expl[:5] or '未知(非管长±单定尺)'}")
        return
    print("未找到样本")


if __name__ == "__main__":
    main()

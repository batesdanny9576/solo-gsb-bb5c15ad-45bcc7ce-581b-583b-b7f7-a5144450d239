"""标注抽样复核：按文档指纹与标注 id 的稳定抽样纯函数。

抽样口径：
  - 抽样在「生成抽样计划」时对当前文档快照执行一次，结果落库，刷新页面、
    重启服务均不变；
  - 每条候选标注的抽签分数为 sha256("{文档内容指纹}|{编码 id}|{标注 id}")，
    只依赖文档指纹与标注 id，因此同一快照下重复生成得到完全相同的样本；
    文档内容或标注集合发生变化后重新生成，分数随之变化，样本自然更新；
  - 每种编码独立抽样：按分数升序取前 N 个（N 超过该编码标注总数时取全部），
    同分时按标注 id 兜底，保证结果完全确定；最终样本按文档位置统一排序。
"""

from __future__ import annotations

import hashlib

CONTEXT_CHARS = 30


def annotations_signature(codings: list[dict]) -> str:
    """当前文档标注集合快照指纹：sha256 of 排序后的 (id, code_id, start, end)。

    新增 / 删除标注、编辑区间或改挂编码（merge / split 会改写标注 id）
    都会改变签名，用于提示「标注已变化，请重新生成抽样」。
    """
    rows = sorted((c["id"], c["code_id"], c["start"], c["end"]) for c in codings)
    raw = "\n".join(f"{cid}:{code_id}:{start}:{end}" for cid, code_id, start, end in rows)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pick_score(fingerprint: str, code_id: int, coding_id: int) -> str:
    raw = f"{fingerprint}|{code_id}|{coding_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def select_sample_codings(
    fingerprint: str,
    codings: list[dict],
    samples: list[tuple[int, int]],
) -> list[dict]:
    """按 (code_id, 抽样数量) 列表稳定抽取标注，返回按文档位置排序的标注 dict。

    抽样数量大于该编码标注总数时取全部；数量 <= 0 或该编码无标注时跳过。
    """
    chosen: list[dict] = []
    for code_id, sample_size in samples:
        if sample_size <= 0:
            continue
        pool = [c for c in codings if c["code_id"] == code_id]
        if not pool:
            continue
        ranked = sorted(
            pool,
            key=lambda c: (
                _pick_score(fingerprint, code_id, c["id"]),
                c["id"],
            ),
        )
        chosen.extend(ranked[: min(sample_size, len(pool))])
    chosen.sort(key=lambda c: (c["start"], c["end"], c["id"]))
    return chosen


def build_context(content: str, start: int, end: int, radius: int = CONTEXT_CHARS) -> dict:
    """返回标注原文与前后文（默认各 30 字），区间越界时安全裁剪。"""
    lo = max(0, start)
    hi = min(len(content), max(lo, end))
    return {
        "text": content[lo:hi],
        "before": content[max(0, lo - radius) : lo],
        "after": content[hi : hi + radius],
    }

"""标注抽样复核：稳定抽样 / 指纹 / 上下文纯函数（不触碰数据库）。

抽样稳定性：给定同一篇文档（内容指纹）与同一批标注（标注集合指纹），
对每种编码的标注列表按 ``文档指纹 | 编码 id | 标注 id`` 计算 sha256
散列后排序取前 N 条。只要参与抽样的文档与标注不变，反复生成（含刷新
页面后重新请求）得到的标注 id 集合与顺序都一致；文档内容或标注发生
变化后指纹改变，抽样随之重新生成。
"""

from __future__ import annotations

import hashlib

from .importing import content_fingerprint

REVIEW_CONTEXT_CHARS = 24


def codings_fingerprint(codings: list[dict]) -> str:
    """标注集合指纹：对全部标注的 (id, code_id, start, end) 规范化后求 sha256。

    任何一条标注的新增、删除、改编码或改区间都会改变摘要；按 id 排序，
    避免列表顺序影响结果。
    """
    normalized = sorted(
        f"{c['id']}:{c['code_id']}:{c['start']}:{c['end']}" for c in codings
    )
    joined = "\n".join(normalized)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def stable_sample(
    document_fingerprint: str,
    code_id: int,
    coding_ids: list[int],
    sample_size: int,
) -> list[int]:
    """按稳定散列顺序从某编码的标注 id 中抽取至多 sample_size 条。

    散列键绑定文档指纹、编码 id 与标注 id：同输入必得同输出；新增 /
    删除其它标注不会改变未受影响标注的相对散列顺序。sample_size 小于
    等于 0 时返回空列表；大于可用数量时返回全部（按散列顺序）。
    """
    if sample_size <= 0 or not coding_ids:
        return []

    def rank(coding_id: int) -> str:
        key = f"{document_fingerprint}|{code_id}|{coding_id}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    ordered = sorted(coding_ids, key=lambda cid: (rank(cid), cid))
    return ordered[: min(sample_size, len(ordered))]


def build_context(content: str, start: int, end: int, radius: int = REVIEW_CONTEXT_CHARS) -> dict:
    """返回标注原文与前后文（供复核队列直接展示）。"""
    length = len(content)
    s = max(0, start)
    e = min(length, end)
    return {
        "text": content[s:e],
        "before": content[max(0, s - radius) : s],
        "after": content[e : min(length, e + radius)],
    }

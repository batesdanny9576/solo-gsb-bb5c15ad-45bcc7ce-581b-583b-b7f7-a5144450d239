"""文档版本迁移：依据文本差异把旧标注区间映射到新正文位置（纯函数，不触碰数据库）。

标注区间 [start, end) 的映射按下列优先级分类：
  kept       标注原文在新正文中唯一出现：唯一定位，自动生成迁移候选（保留）；
  ambiguous  标注原文在新正文中出现多次：多重匹配，列出全部候选及各自新上下文，
             并按字符级差异对齐给出默认候选，交用户确认；
  modified   原文已不能整体找到，但足量字符在差异对齐中存活：给出覆盖首尾存活
             字符的建议新区间（含其间的新插入内容），交用户确认；
  deleted    区间在新正文中没有留下可辨认内容（差异边界之间为空 / 仅剩孤立的
             对齐字符）：原文已删除，可在新正文手选新区间或放弃；
  mismatch   区间位置被实质的新文本占据：无法可靠映射，列为失配，确认时丢弃。

其中 kept 为「保留」；ambiguous / modified / deleted 为「待确认」；
mismatch 为「失配」。调用方在单事务内重跑本预览并据用户决策更新正文与标注，
任一校验失败整体回滚。
"""

from __future__ import annotations

import bisect
import difflib

CONTEXT_CHARS = 16

# 存活字符判定：至少 2 个字符对齐，且占旧区间比例不低于此值，才认为原标注
# 在新版本中仍可辨认（低于此值的孤立标点对齐归入删除 / 失配）
SURVIVE_RATIO = 0.25
SURVIVE_MIN_CHARS = 2

# replace 片段中新文本短于旧文本的此比例时视为「实际删除」（对齐把远处内容
# 连进了删除位置）；长度相当则视为原位置被新内容替换（失配）
REPLACE_GAP_RATIO = 0.5

STATUS_LABELS = {
    "kept": "保留",
    "ambiguous": "待确认·多重匹配",
    "modified": "待确认·内容改动",
    "deleted": "待确认·文本删除",
    "mismatch": "失配",
}

PENDING_STATUSES = {"ambiguous", "modified", "deleted"}


def find_occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    """返回 needle 在 text 中的全部半开命中区间（含彼此重叠的命中）。"""
    if not needle:
        return []
    positions: list[tuple[int, int]] = []
    pos = text.find(needle)
    while pos != -1:
        positions.append((pos, pos + len(needle)))
        pos = text.find(needle, pos + 1)
    return positions


def build_alignment(old: str, new: str) -> tuple[list[int], list[int], list[tuple]]:
    """字符级差异对齐。

    返回 (old_eq, new_eq, opcodes)：前两个长度相同的下标数组描述全部「相同」
    字符的对应关系（均按旧下标升序）；opcodes 为 SequenceMatcher 的差异片段。
    """
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    opcodes = matcher.get_opcodes()
    old_eq: list[int] = []
    new_eq: list[int] = []
    for tag, i1, i2, j1, _j2 in opcodes:
        if tag == "equal":
            for k in range(i2 - i1):
                old_eq.append(i1 + k)
                new_eq.append(j1 + k)
    return old_eq, new_eq, opcodes


def map_offset(old_eq: list[int], new_eq: list[int], offset: int, new_length: int) -> int:
    """把旧文本偏移量按最近的相同字符折算到新文本坐标（用于候选默认排序）。"""
    if not old_eq:
        return 0
    k = min(range(len(old_eq)), key=lambda i: abs(old_eq[i] - offset))
    return max(0, min(new_length, new_eq[k] + (offset - old_eq[k])))


def context_around(text: str, start: int, end: int) -> dict:
    """截取 [start, end) 在 text 中的片段与前后文（零宽区间亦合法，用于删除锚点）。"""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    return {
        "start": start,
        "end": end,
        "before": text[max(0, start - CONTEXT_CHARS) : start],
        "text": text[start:end],
        "after": text[end : end + CONTEXT_CHARS],
    }


def classify_coding(
    coding: dict,
    old: str,
    new: str,
    old_eq: list[int],
    new_eq: list[int],
    opcodes: list[tuple],
) -> dict:
    """对单条旧标注计算迁移分类与候选 / 建议 / 上下文。"""
    s, e = coding["start"], coding["end"]
    old_text = old[s:e]
    occurrences = find_occurrences(new, old_text)
    length = e - s

    item = {
        "coding_id": coding["id"],
        "code_id": coding["code_id"],
        "code_name": coding["code_name"],
        "code_color": coding["code_color"],
        "old_start": s,
        "old_end": e,
        "old_text": old_text,
        "old_context": context_around(old, s, e),
        "status": None,
        "status_label": None,
        "candidates": [],
        "suggestion": None,
        "anchor": None,
        "new_region": None,
        "stats": {"length": length, "equal": 0, "deleted": length},
        "detail": "",
    }

    # 1) 旧标注原文在新正文中整体出现：唯一 → 保留；多处 → 多重匹配待确认
    if len(occurrences) == 1:
        ns, ne = occurrences[0]
        item["status"] = "kept"
        item["status_label"] = STATUS_LABELS["kept"]
        item["suggestion"] = context_around(new, ns, ne)
        item["stats"] = {"length": length, "equal": length, "deleted": 0}
        item["detail"] = "新正文中唯一匹配，已自动定位"
        return item

    # 区间内在新版本中对齐存活的字符（旧下标 -> 新下标）
    lo = bisect.bisect_left(old_eq, s)
    hi = bisect.bisect_left(old_eq, e)
    inside_new = new_eq[lo:hi]
    equal_count = len(inside_new)
    item["stats"] = {
        "length": length,
        "equal": equal_count,
        "deleted": length - equal_count,
    }

    if len(occurrences) > 1:
        expected = map_offset(old_eq, new_eq, s, len(new))
        default_idx = min(
            range(len(occurrences)),
            key=lambda i: abs(occurrences[i][0] - expected),
        )
        item["status"] = "ambiguous"
        item["status_label"] = STATUS_LABELS["ambiguous"]
        item["candidates"] = [
            {**context_around(new, ns, ne), "default": i == default_idx}
            for i, (ns, ne) in enumerate(occurrences)
        ]
        item["detail"] = (
            f"标注文本在新正文中出现 {len(occurrences)} 次，请选择对应位置"
        )
        return item

    # 2) 原文已不能整体找到：足量字符存活 → 内容改动待确认（建议区间）
    survive_ratio = equal_count / length if length else 0.0
    if equal_count >= SURVIVE_MIN_CHARS and survive_ratio >= SURVIVE_RATIO:
        ns = min(inside_new)
        ne = max(inside_new) + 1
        item["status"] = "modified"
        item["status_label"] = STATUS_LABELS["modified"]
        item["suggestion"] = context_around(new, ns, ne)
        item["detail"] = "原文字句有改动，已按文本差异对齐给出建议区间，请确认"
        return item

    # 3) 存活不足：按差异片段聚合删除与替换
    #    - replace 片段中新文本长度不短于旧片段的 REPLACE_GAP_RATIO：原位置
    #      被等量新内容占据 → 失配；
    #    - 纯 delete 片段与「新文本明显更短」的 replace：原文删除 → 删除待确认。
    delete_anchors: list[int] = []
    replace_regions: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if max(i1, s) >= min(i2, e):
            continue
        if tag == "delete":
            delete_anchors.append(j1)
        elif tag == "replace":
            # 以片段整体新旧长度比判定删除 / 替换；区域仅用于新上下文展示，
            # 直接采用片段的新侧范围
            if j2 - j1 >= (i2 - i1) * REPLACE_GAP_RATIO:
                replace_regions.append((j1, j2))
            else:
                delete_anchors.append(j1)

    if replace_regions:
        r0 = min(r[0] for r in replace_regions)
        r1 = max(r[1] for r in replace_regions)
        item["status"] = "mismatch"
        item["status_label"] = STATUS_LABELS["mismatch"]
        item["new_region"] = context_around(new, r0, r1)
        item["detail"] = "标注文本在新版本中被其它内容替换，无法可靠映射，确认后将丢弃"
    else:
        anchor_pos = delete_anchors[0] if delete_anchors else 0
        item["status"] = "deleted"
        item["status_label"] = STATUS_LABELS["deleted"]
        item["anchor"] = context_around(new, anchor_pos, anchor_pos)
        item["detail"] = "标注文本在新版本中已删除，可放弃或在新正文中重新指定区间"
    return item


def _identical_item(coding: dict, content: str) -> dict:
    """新旧正文一致：全部标注原样保留，避免重复字句被误判为多重匹配。"""
    s, e = coding["start"], coding["end"]
    return {
        "coding_id": coding["id"],
        "code_id": coding["code_id"],
        "code_name": coding["code_name"],
        "code_color": coding["code_color"],
        "old_start": s,
        "old_end": e,
        "old_text": content[s:e],
        "old_context": context_around(content, s, e),
        "status": "kept",
        "status_label": STATUS_LABELS["kept"],
        "candidates": [],
        "suggestion": context_around(content, s, e),
        "anchor": None,
        "new_region": None,
        "stats": {"length": e - s, "equal": e - s, "deleted": 0},
        "detail": "新旧正文一致，标注位置不变",
    }


def build_migration_preview(
    document_id: int,
    old_content: str,
    new_content: str,
    codings: list[dict],
    fingerprint: str,
) -> dict:
    """构建整篇文档的迁移预览：逐条分类并汇总保留 / 待确认 / 失配计数。"""
    if new_content == old_content:
        items = [_identical_item(c, old_content) for c in codings]
    else:
        old_eq, new_eq, opcodes = build_alignment(old_content, new_content)
        items = [
            classify_coding(c, old_content, new_content, old_eq, new_eq, opcodes)
            for c in codings
        ]
    summary = {
        "total": len(items),
        "kept": sum(1 for it in items if it["status"] == "kept"),
        "pending": sum(1 for it in items if it["status"] in PENDING_STATUSES),
        "mismatch": sum(1 for it in items if it["status"] == "mismatch"),
    }
    return {
        "document_id": document_id,
        "old_length": len(old_content),
        "new_length": len(new_content),
        "old_fingerprint": fingerprint,
        "summary": summary,
        "items": items,
    }

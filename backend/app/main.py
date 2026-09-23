"""质性分析工作台后端：FastAPI + SQLite。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .conflicts import (
    find_conflicts,
    merge_same_code,
    split_containers,
    subtract_ranges,
)
from .cooccurrence import build_code_intervals, intersect_intervals
from .db import get_db, init_db, list_codings, next_color
from .distribution import compute_distribution
from .gaps import compute_gaps
from .importing import (
    PackageFormatError,
    build_import_preview,
    content_fingerprint,
    parse_package,
)
from .migration import PENDING_STATUSES, build_migration_preview
from .review import build_context, annotations_signature, select_sample_codings
from .speakers import compute_speaker_stats
from .transitions import compute_transitions

Resolution = Literal["keep", "merge", "split"]


# ---------- Pydantic 模型 ----------

class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class CodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str | None = None


class CodeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = None


class PreviewRequest(BaseModel):
    document_id: int
    code_id: int
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    exclude_id: int | None = None


class CodingCreate(BaseModel):
    document_id: int
    code_id: int
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    resolution: Resolution = "keep"


class CodingUpdate(BaseModel):
    code_id: int | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    resolution: Resolution = "keep"


class BatchCodingItem(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class BatchCodingCreate(BaseModel):
    document_id: int
    code_id: int
    items: list[BatchCodingItem] = Field(min_length=1, max_length=1000)


class AnnotationPackage(BaseModel):
    """标注包导入请求：正文为「导出 JSON」原文（dict 形式）。"""

    package: dict


class MigrationPreviewRequest(BaseModel):
    """文档版本迁移预检：提交新正文（不落库）。"""

    new_content: str = Field(min_length=1)


class MigrationDecision(BaseModel):
    """单条待确认标注的用户决策：discard 放弃 / remap 改到新区间。"""

    coding_id: int
    action: Literal["discard", "remap"]
    start: int | None = Field(default=None, ge=0)
    # 区间合法性（end > start、不超出新正文长度）由迁移端点统一校验
    end: int | None = Field(default=None, ge=0)


class MigrationCommitRequest(BaseModel):
    """文档版本迁移确认：原文指纹 + 新正文 + 待确认项决策。"""

    old_fingerprint: str = Field(min_length=1)
    new_content: str = Field(min_length=1)
    decisions: list[MigrationDecision] = Field(default_factory=list)


class ReviewCodeSample(BaseModel):
    """抽样计划中单个编码的抽样数量。"""

    code_id: int
    sample_size: int = Field(ge=1, le=100000)


class ReviewPlanCreate(BaseModel):
    """生成（或重新生成）当前文档的标注抽样复核计划。"""

    samples: list[ReviewCodeSample] = Field(min_length=1, max_length=500)


class ReviewVerdictUpdate(BaseModel):
    """单条复核结果：verdict 缺省表示只更新备注。"""

    verdict: Literal["pending", "approved", "revision"] | None = None
    note: str | None = Field(default=None, max_length=2000)


# ---------- 应用 ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="访谈转录质性分析工作台", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_document_or_404(conn, document_id: int) -> dict:
    row = conn.execute(
        "SELECT id, title, content, created_at FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return dict(row)


def get_code_or_404(conn, code_id: int) -> dict:
    row = conn.execute(
        "SELECT id, name, color, created_at FROM codes WHERE id = ?", (code_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="编码不存在")
    return dict(row)


def validate_range(doc: dict, start: int, end: int) -> None:
    if end <= start:
        raise HTTPException(status_code=422, detail="区间不合法：end 必须大于 start")
    if end > len(doc["content"]):
        raise HTTPException(status_code=422, detail="区间超出文本长度")


# ---------- 健康检查 ----------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# ---------- 文档 ----------

@app.get("/api/documents")
def list_documents() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.title, d.created_at, length(d.content) AS length,
                   (SELECT COUNT(*) FROM codings cg WHERE cg.document_id = d.id) AS coding_count
            FROM documents d ORDER BY d.id
            """
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/documents", status_code=201)
def create_document(body: DocumentCreate) -> dict:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO documents (title, content) VALUES (?, ?)",
            (body.title, body.content),
        )
        return get_document_or_404(conn, cur.lastrowid)


@app.get("/api/documents/{document_id}")
def get_document(document_id: int) -> dict:
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        codes = [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, color, created_at FROM codes ORDER BY id"
            ).fetchall()
        ]
        return {
            "document": doc,
            "codes": codes,
            "codings": list_codings(conn, document_id),
        }


@app.delete("/api/documents/{document_id}", status_code=204)
def delete_document(document_id: int) -> Response:
    with get_db() as conn:
        get_document_or_404(conn, document_id)
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    return Response(status_code=204)


# ---------- 关键词检索（批量编码用） ----------

SEARCH_CONTEXT_CHARS = 20


@app.get("/api/documents/{document_id}/search")
def search_document(
    document_id: int,
    query: str = Query(min_length=1, max_length=200),
) -> dict:
    """在文档全文中检索字面关键词（非正则），返回全部命中的稳定半开区间。

    彼此重叠的命中也一并返回（如 "aa" 在 "aaa" 中命中 [0,2) 与 [1,3)），
    每项附带前后文片段，便于前端逐项确认后批量编码。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
    content = doc["content"]
    matches: list[dict] = []
    pos = content.find(query)
    while pos != -1:
        end = pos + len(query)
        matches.append(
            {
                "start": pos,
                "end": end,
                "text": content[pos:end],
                "before": content[max(0, pos - SEARCH_CONTEXT_CHARS) : pos],
                "after": content[end : end + SEARCH_CONTEXT_CHARS],
            }
        )
        # 从下一字符继续找，保证重叠命中不被跳过
        pos = content.find(query, pos + 1)
    return {"document_id": document_id, "query": query, "matches": matches}


# ---------- 编码本（codebook） ----------

@app.get("/api/codes")
def list_codes() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.color, c.created_at,
                   (SELECT COUNT(*) FROM codings cg WHERE cg.code_id = c.id) AS usage_count
            FROM codes c ORDER BY c.id
            """
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/codes", status_code=201)
def create_code(body: CodeCreate) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="编码名不能为空")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM codes WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="同名编码已存在")
        color = body.color or next_color(conn)
        cur = conn.execute(
            "INSERT INTO codes (name, color) VALUES (?, ?)", (name, color)
        )
        return get_code_or_404(conn, cur.lastrowid)


@app.put("/api/codes/{code_id}")
def update_code(code_id: int, body: CodeUpdate) -> dict:
    with get_db() as conn:
        code = get_code_or_404(conn, code_id)
        name = (body.name or code["name"]).strip()
        color = body.color or code["color"]
        dup = conn.execute(
            "SELECT id FROM codes WHERE name = ? AND id != ?", (name, code_id)
        ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail="同名编码已存在")
        conn.execute(
            "UPDATE codes SET name = ?, color = ? WHERE id = ?",
            (name, color, code_id),
        )
        return get_code_or_404(conn, code_id)


@app.delete("/api/codes/{code_id}", status_code=204)
def delete_code(code_id: int) -> Response:
    with get_db() as conn:
        get_code_or_404(conn, code_id)
        conn.execute("DELETE FROM codes WHERE id = ?", (code_id,))
    return Response(status_code=204)


# ---------- 冲突预览 ----------

@app.post("/api/codings/preview")
def preview_coding(body: PreviewRequest) -> dict:
    with get_db() as conn:
        doc = get_document_or_404(conn, body.document_id)
        get_code_or_404(conn, body.code_id)
        validate_range(doc, body.start, body.end)
        existing = list_codings(conn, body.document_id)
        conflicts = find_conflicts(
            existing, body.code_id, body.start, body.end, body.exclude_id
        )
        return {
            "has_conflicts": len(conflicts) > 0,
            "conflicts": conflicts,
            "available_resolutions": sorted(
                {a for c in conflicts for a in c["actions"]} | {"keep"}
            ),
        }


# ---------- 编码的创建 / 修改 / 删除 ----------

INSERT_CODING = (
    'INSERT INTO codings (document_id, code_id, start, "end") VALUES (?, ?, ?, ?)'
)


def apply_coding(
    conn,
    document_id: int,
    code_id: int,
    start: int,
    end: int,
    resolution: Resolution,
    exclude_id: int | None = None,
) -> list[int]:
    """按决策写入编码，返回产生的 coding id 列表。"""
    existing = [
        c for c in list_codings(conn, document_id) if c["id"] != exclude_id
    ]
    conflicts = find_conflicts(existing, code_id, start, end)
    same = [c["coding"] for c in conflicts if c["type"] == "same_code"]
    containers = [c["coding"] for c in conflicts if c["type"] == "nested_container"]
    contained = [c["coding"] for c in conflicts if c["type"] == "nested_contained"]

    if resolution == "merge" and same:
        m_start, m_end, absorbed = merge_same_code(existing, code_id, start, end)
        for cid in absorbed:
            conn.execute("DELETE FROM codings WHERE id = ?", (cid,))
        cur = conn.execute(INSERT_CODING, (document_id, code_id, m_start, m_end))
        return [cur.lastrowid]

    if resolution == "split" and (containers or contained):
        for op in split_containers(containers, start, end):
            old = next(c for c in containers if c["id"] == op["id"])
            conn.execute("DELETE FROM codings WHERE id = ?", (op["id"],))
            for s, e in op["parts"]:
                conn.execute(
                    INSERT_CODING, (document_id, old["code_id"], s, e)
                )
        holes = [(c["start"], c["end"]) for c in contained]
        ids = []
        for s, e in subtract_ranges(start, end, holes):
            cur = conn.execute(INSERT_CODING, (document_id, code_id, s, e))
            ids.append(cur.lastrowid)
        return ids

    cur = conn.execute(INSERT_CODING, (document_id, code_id, start, end))
    return [cur.lastrowid]


@app.post("/api/codings", status_code=201)
def create_coding(body: CodingCreate) -> dict:
    with get_db() as conn:
        doc = get_document_or_404(conn, body.document_id)
        get_code_or_404(conn, body.code_id)
        validate_range(doc, body.start, body.end)
        ids = apply_coding(
            conn, body.document_id, body.code_id, body.start, body.end,
            body.resolution,
        )
        return {"ids": ids, "codings": list_codings(conn, body.document_id)}


@app.post("/api/codings/batch", status_code=201)
def create_codings_batch(body: BatchCodingCreate) -> dict:
    """批量创建编码标注（单个事务）。

    先在事务内校验全部区间：任一非法（end <= start 或超出文本长度）即抛 422，
    整批回滚不落库。校验通过后逐项写入：
      - 与既有标注完全相同（同编码、同区间）的项记为跳过（含本批先写入的项）；
      - 其余按 keep 边界原样写入，不做合并 / 拆分调整。
    返回新增与跳过明细及最新标注列表。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, body.document_id)
        get_code_or_404(conn, body.code_id)
        length = len(doc["content"])
        invalid = [
            {"index": i, "start": it.start, "end": it.end}
            for i, it in enumerate(body.items)
            if not (it.start < it.end <= length)
        ]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"存在 {len(invalid)} 个非法区间，整批未写入",
                    "invalid": invalid,
                },
            )
        id_by_key = {
            (c["code_id"], c["start"], c["end"]): c["id"]
            for c in list_codings(conn, body.document_id)
        }
        created: list[dict] = []
        skipped: list[dict] = []
        for it in body.items:
            key = (body.code_id, it.start, it.end)
            if key in id_by_key:
                skipped.append(
                    {"start": it.start, "end": it.end, "existing_id": id_by_key[key]}
                )
                continue
            cur = conn.execute(
                INSERT_CODING, (body.document_id, body.code_id, it.start, it.end)
            )
            id_by_key[key] = cur.lastrowid
            created.append({"id": cur.lastrowid, "start": it.start, "end": it.end})
        return {
            "created": created,
            "skipped": skipped,
            "codings": list_codings(conn, body.document_id),
        }


# ---------- 标注包导入（导出 JSON → 当前文档） ----------


def prepare_import(conn, document_id: int, package: dict) -> tuple[dict, dict, list[dict]]:
    """在当前连接快照上构建导入预览，返回 (doc, preview, code_rows)。

    结构无法解析时抛 422（标注包格式错误）。
    """
    doc = get_document_or_404(conn, document_id)
    try:
        src_doc, items = parse_package(package)
    except PackageFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    code_rows = [
        dict(r)
        for r in conn.execute("SELECT id, name, color FROM codes ORDER BY id").fetchall()
    ]
    existing = list_codings(conn, document_id)
    preview = build_import_preview(
        document_id, doc["content"], src_doc, items, code_rows, existing
    )
    return doc, preview, code_rows


@app.post("/api/documents/{document_id}/import/preview")
def import_preview(document_id: int, body: AnnotationPackage) -> dict:
    """标注包导入预检（不落库）。

    按编码名称映射当前编码本，校验文档内容指纹、区间边界与原文切片、重复项，
    返回新增 / 跳过 / 未知编码 / 非法条目分类及新增项的冲突明细。
    """
    with get_db() as conn:
        _, preview, _ = prepare_import(conn, document_id, body.package)
    return preview


@app.post("/api/documents/{document_id}/import", status_code=201)
def import_confirm(document_id: int, body: AnnotationPackage) -> dict:
    """确认导入：单事务写入全部合法（新增）标注。

    先在事务内重跑全部校验：指纹不一致、存在非法区间 / 切片不匹配或未知编码
    时抛 422，整批回滚、不落库。通过后仅写入预览分类为「新增」的标注，
    完全相同的既有标注 / 包内重复项按跳过处理；新增项按 keep（保留边界）
    写入，不做合并 / 拆分。
    """
    with get_db() as conn:
        _, preview, _ = prepare_import(conn, document_id, body.package)
        if not preview["can_import"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "校验未通过，标注包未导入",
                    "blockers": preview["blockers"],
                    "summary": preview["summary"],
                },
            )

        # 以事务内最新快照再去重一次（created 已在预览时去重，此处防御并发写入）
        id_by_key = {
            (c["code_id"], c["start"], c["end"]): c["id"]
            for c in list_codings(conn, document_id)
        }
        created: list[dict] = []
        extra_skipped: list[dict] = []
        for it in preview["created"]:
            key = (it["code_id"], it["start"], it["end"])
            if key in id_by_key:
                extra_skipped.append(
                    {
                        **{k: it[k] for k in ("index", "code_name", "start", "end")},
                        "reason": "existing",
                        "existing_id": id_by_key[key],
                    }
                )
                continue
            cur = conn.execute(
                INSERT_CODING,
                (document_id, it["code_id"], it["start"], it["end"]),
            )
            id_by_key[key] = cur.lastrowid
            created.append(
                {
                    "id": cur.lastrowid,
                    "code_id": it["code_id"],
                    "code_name": it["code_name"],
                    "color": it["color"],
                    "start": it["start"],
                    "end": it["end"],
                    "text": it["text"],
                    "conflicts": it["conflicts"],
                }
            )
        # 校验通过但全部为重复项：事务正常提交，返回空新增列表
        return {
            "created": created,
            "skipped": [*preview["skipped"], *extra_skipped],
            "codings": list_codings(conn, document_id),
        }


# ---------- 文档版本迁移（新正文 → 旧标注区间映射） ----------


def prepare_migration(conn, document_id: int, new_content: str) -> tuple[dict, dict]:
    """在当前连接快照上构建迁移预览，返回 (doc, preview)。"""
    doc = get_document_or_404(conn, document_id)
    fingerprint = content_fingerprint(doc["content"])
    codings = list_codings(conn, document_id)
    preview = build_migration_preview(
        document_id, doc["content"], new_content, codings, fingerprint
    )
    return doc, preview


@app.post("/api/documents/{document_id}/migration/preview")
def migration_preview(document_id: int, body: MigrationPreviewRequest) -> dict:
    """文档版本迁移预检（不落库）。

    对新正文与当前正文做字符级差异对齐，逐条把旧标注分类为保留（唯一匹配
    自动定位）/ 待确认（多重匹配、内容改动、文本删除，附新旧上下文）/ 失配
    （被替换，确认时丢弃），并返回原文内容指纹供确认时校验版本。
    """
    with get_db() as conn:
        _, preview = prepare_migration(conn, document_id, body.new_content)
    return preview


@app.post("/api/documents/{document_id}/migration/commit")
def migration_commit(document_id: int, body: MigrationCommitRequest) -> dict:
    """确认迁移：校验原文版本，在单事务中更新正文与全部标注。

    先重跑差异对齐并在事务内完成全部校验：
      - 原文内容指纹不一致（正文已被其它操作改动）→ 422，整批回滚；
      - 待确认项缺少决策 / 存在未知标注 id / remap 缺少区间或区间非法
        （end <= start 或超出新正文长度）→ 422，整批回滚。
    任一校验失败均不落库；通过后：保留项与 remap 项更新区间（保留原 id），
    用户放弃项与失配项删除，正文更新为新版本。
    """
    with get_db() as conn:
        doc, preview = prepare_migration(conn, document_id, body.new_content)

        blockers: list[dict] = []
        current_fp = content_fingerprint(doc["content"])
        if body.old_fingerprint != current_fp:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "原文版本校验失败：文档在预检后已被修改，请重新生成迁移预览",
                    "expected": current_fp,
                },
            )

        items_by_id = {it["coding_id"]: it for it in preview["items"]}
        pending_ids = {
            it["coding_id"] for it in preview["items"] if it["status"] in PENDING_STATUSES
        }
        decision_by_id: dict[int, MigrationDecision] = {}
        for d in body.decisions:
            if d.coding_id not in items_by_id:
                blockers.append(
                    {"coding_id": d.coding_id, "detail": "标注不存在或不属于当前文档"}
                )
                continue
            if d.coding_id not in pending_ids:
                blockers.append(
                    {"coding_id": d.coding_id, "detail": "该标注不是待确认项，无需决策"}
                )
                continue
            if d.coding_id in decision_by_id:
                blockers.append(
                    {"coding_id": d.coding_id, "detail": "同一标注存在重复决策"}
                )
                continue
            if d.action == "remap":
                if d.start is None or d.end is None:
                    blockers.append(
                        {"coding_id": d.coding_id, "detail": "重新指定区间缺少 start / end"}
                    )
                    continue
                if not (0 <= d.start < d.end <= len(body.new_content)):
                    blockers.append(
                        {
                            "coding_id": d.coding_id,
                            "detail": "区间不合法：需满足 0 <= start < end <= 新正文长度",
                        }
                    )
                    continue
            decision_by_id[d.coding_id] = d
        missing = sorted(pending_ids - set(decision_by_id))
        for cid in missing:
            blockers.append({"coding_id": cid, "detail": "待确认项缺少决策"})

        if blockers:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"存在 {len(blockers)} 个未通过的校验，迁移未执行",
                    "blockers": blockers,
                },
            )

        kept = remapped = discarded = dropped = 0
        for it in preview["items"]:
            cid = it["coding_id"]
            if it["status"] == "kept":
                ns, ne = it["suggestion"]["start"], it["suggestion"]["end"]
                conn.execute(
                    'UPDATE codings SET start = ?, "end" = ?,'
                    " updated_at = datetime('now') WHERE id = ?",
                    (ns, ne, cid),
                )
                kept += 1
            elif it["status"] in PENDING_STATUSES:
                decision = decision_by_id[cid]
                if decision.action == "discard":
                    conn.execute("DELETE FROM codings WHERE id = ?", (cid,))
                    discarded += 1
                else:
                    conn.execute(
                        'UPDATE codings SET start = ?, "end" = ?,'
                        " updated_at = datetime('now') WHERE id = ?",
                        (decision.start, decision.end, cid),
                    )
                    remapped += 1
            else:
                # mismatch（失配）：无可靠映射，确认即丢弃
                conn.execute("DELETE FROM codings WHERE id = ?", (cid,))
                dropped += 1

        conn.execute(
            "UPDATE documents SET content = ? WHERE id = ?",
            (body.new_content, document_id),
        )
        return {
            "document": {**doc, "content": body.new_content},
            "summary": {
                "total": len(preview["items"]),
                "kept": kept,
                "remapped": remapped,
                "discarded": discarded,
                "dropped": dropped,
            },
            "codings": list_codings(conn, document_id),
        }


@app.put("/api/codings/{coding_id}")
def update_coding(coding_id: int, body: CodingUpdate) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, document_id, code_id, start, \"end\" FROM codings WHERE id = ?",
            (coding_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="编码标注不存在")
        old = dict(row)
        doc = get_document_or_404(conn, old["document_id"])
        code_id = body.code_id if body.code_id is not None else old["code_id"]
        start = body.start if body.start is not None else old["start"]
        end = body.end if body.end is not None else old["end"]
        get_code_or_404(conn, code_id)
        validate_range(doc, start, end)
        if body.resolution == "keep":
            # 无结构变更：原地更新，保留原 id
            conn.execute(
                'UPDATE codings SET code_id = ?, start = ?, "end" = ?,'
                " updated_at = datetime('now') WHERE id = ?",
                (code_id, start, end, coding_id),
            )
            ids = [coding_id]
        else:
            # merge / split 会改动其它编码：删除旧标注后按决策重写（同事务，失败回滚）
            conn.execute("DELETE FROM codings WHERE id = ?", (coding_id,))
            ids = apply_coding(
                conn, old["document_id"], code_id, start, end,
                body.resolution, exclude_id=coding_id,
            )
        return {"ids": ids, "codings": list_codings(conn, old["document_id"])}


@app.delete("/api/codings/{coding_id}", status_code=204)
def delete_coding(coding_id: int) -> Response:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM codings WHERE id = ?", (coding_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="编码标注不存在")
        conn.execute("DELETE FROM codings WHERE id = ?", (coding_id,))
    return Response(status_code=204)


# ---------- 导出 ----------

@app.get("/api/documents/{document_id}/export")
def export_document(document_id: int) -> JSONResponse:
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        codes = [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, color FROM codes ORDER BY id"
            ).fetchall()
        ]
        codings = list_codings(conn, document_id)
    content = doc["content"]
    payload = {
        "document": {
            "id": doc["id"],
            "title": doc["title"],
            "content": content,
            "length": len(content),
            "fingerprint": content_fingerprint(content),
        },
        "codes": codes,
        "codings": [
            {
                "id": c["id"],
                "code_id": c["code_id"],
                "code": c["code_name"],
                "color": c["code_color"],
                "start": c["start"],
                "end": c["end"],
                "text": content[c["start"] : c["end"]],
            }
            for c in codings
        ],
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    filename = f"codings-doc-{document_id}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- 编码共现统计 ----------

@app.get("/api/documents/{document_id}/cooccurrence")
def cooccurrence(
    document_id: int,
    codes: list[int] | None = Query(default=None),
) -> dict:
    """按当前文档返回编码两两共现矩阵数据。

    查询参数 `codes` 为可选的编码筛选（?codes=1&codes=2）；缺省统计编码本
    全部编码。每个编码先各自合并重叠/相邻区间，再对编码对求交集，保证
    共享字符不被重复计数。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        all_codes = {
            r["id"]: {"id": r["id"], "name": r["name"], "color": r["color"]}
            for r in conn.execute("SELECT id, name, color FROM codes ORDER BY id")
        }
        if codes:
            unknown = [cid for cid in dict.fromkeys(codes) if cid not in all_codes]
            if unknown:
                raise HTTPException(
                    status_code=404, detail=f"编码不存在: {unknown}"
                )
            member_ids = sorted(set(codes))
        else:
            member_ids = sorted(all_codes)
        codings = list_codings(conn, document_id)

    intervals = build_code_intervals(codings)
    content = doc["content"]
    pairs: list[dict] = []
    for i, ci in enumerate(member_ids):
        for cj in member_ids[i:]:
            ivs_i = intervals.get(ci, [])
            ivs_j = intervals.get(cj, [])
            shared = ivs_i if ci == cj else intersect_intervals(ivs_i, ivs_j)
            pairs.append(
                {
                    "code_a_id": ci,
                    "code_b_id": cj,
                    "diagonal": ci == cj,
                    "char_count": sum(e - s for s, e in shared),
                    "segment_count": len(shared),
                    "segments": [
                        {"start": s, "end": e, "text": content[s:e]}
                        for s, e in shared
                    ],
                }
            )
    return {
        "document_id": document_id,
        "codes": [all_codes[cid] for cid in member_ids],
        "pairs": pairs,
    }


# ---------- 说话人分析 ----------

@app.get("/api/documents/{document_id}/speakers")
def speaker_analysis(
    document_id: int,
    codes: str | None = Query(default=None),
) -> dict:
    """按当前文档返回说话人分析数据。

    解析形如「说话人：内容」的发言段落（行首标签 + 全角/半角冒号），统计
    每位说话人的文本长度、各编码覆盖字符数与覆盖率；文首无法识别说话人
    的文本归入「未标注说话人」。同一编码的重叠标注先合并再裁剪到发言段落，
    共享字符不重复计数。

    查询参数 `codes` 为逗号分隔的编码 id（?codes=1,2）：缺省统计编码本
    全部编码；空串（?codes=）表示不统计任何编码，仅返回文本长度。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        all_codes = {
            r["id"]: {"id": r["id"], "name": r["name"], "color": r["color"]}
            for r in conn.execute("SELECT id, name, color FROM codes ORDER BY id")
        }
        if codes is None:
            member_ids = sorted(all_codes)
        else:
            try:
                member_ids = sorted(
                    {int(x) for x in codes.split(",") if x.strip()}
                )
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="codes 参数格式错误：应为逗号分隔的编码 id",
                )
            unknown = [cid for cid in member_ids if cid not in all_codes]
            if unknown:
                raise HTTPException(
                    status_code=404, detail=f"编码不存在: {unknown}"
                )
        codings = list_codings(conn, document_id)

    content = doc["content"]
    return {
        "document_id": document_id,
        "document_length": len(content),
        "codes": [all_codes[cid] for cid in member_ids],
        "speakers": compute_speaker_stats(content, codings, member_ids),
    }


# ---------- 覆盖缺口 ----------

@app.get("/api/documents/{document_id}/gaps")
def coverage_gaps(
    document_id: int,
    codes: str | None = Query(default=None),
) -> dict:
    """按当前文档返回未被所选编码覆盖的连续文本区间（覆盖缺口）。

    先把所选编码的半开标注区间合并为并集（重叠/相邻的区间并成一段），
    再对 [0, 全文长度) 求补集；文首、文尾与零标注（全文即一个缺口）场景
    均自然保留。每个缺口返回起止位置、长度与原文摘录。

    查询参数 `codes` 为逗号分隔的编码 id（?codes=1,2）：缺省统计编码本
    全部编码；空串（?codes=）表示不统计任何编码，全文即一个缺口。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        all_codes = {
            r["id"]: {"id": r["id"], "name": r["name"], "color": r["color"]}
            for r in conn.execute("SELECT id, name, color FROM codes ORDER BY id")
        }
        if codes is None:
            member_ids = sorted(all_codes)
        else:
            try:
                member_ids = sorted(
                    {int(x) for x in codes.split(",") if x.strip()}
                )
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="codes 参数格式错误：应为逗号分隔的编码 id",
                )
            unknown = [cid for cid in member_ids if cid not in all_codes]
            if unknown:
                raise HTTPException(
                    status_code=404, detail=f"编码不存在: {unknown}"
                )
        codings = list_codings(conn, document_id)

    content = doc["content"]
    gaps = compute_gaps(content, codings, member_ids)
    covered = len(content) - sum(g["length"] for g in gaps)
    return {
        "document_id": document_id,
        "document_length": len(content),
        "codes": [all_codes[cid] for cid in member_ids],
        "covered_char_count": covered,
        "coverage_rate": round(covered / len(content), 4) if content else 0.0,
        "gaps": gaps,
    }


# ---------- 编码流转 ----------

@app.get("/api/documents/{document_id}/transitions")
def code_transitions(
    document_id: int,
    codes: str | None = Query(default=None),
) -> dict:
    """按当前文档返回相邻编码状态的有向转移统计。

    把所选编码的全部标注起止边界切成原子区间，以覆盖编码集合表示区间
    状态；跳过无编码区间并合并物理相邻的同状态区间，再按文档顺序统计
    相邻状态的有向转移次数，返回每次转移的来源、目标区间及中间距离
    （目标起点 − 来源终点，0 表示首尾相接）。

    查询参数 `codes` 为逗号分隔的编码 id（?codes=1,2）：缺省统计编码本
    全部编码；空串（?codes=）表示不统计任何编码（无状态区间、无转移）。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        all_codes = {
            r["id"]: {"id": r["id"], "name": r["name"], "color": r["color"]}
            for r in conn.execute("SELECT id, name, color FROM codes ORDER BY id")
        }
        if codes is None:
            member_ids = sorted(all_codes)
        else:
            try:
                member_ids = sorted(
                    {int(x) for x in codes.split(",") if x.strip()}
                )
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="codes 参数格式错误：应为逗号分隔的编码 id",
                )
            unknown = [cid for cid in member_ids if cid not in all_codes]
            if unknown:
                raise HTTPException(
                    status_code=404, detail=f"编码不存在: {unknown}"
                )
        codings = list_codings(conn, document_id)

    content = doc["content"]
    return {
        "document_id": document_id,
        "document_length": len(content),
        "codes": [all_codes[cid] for cid in member_ids],
        **compute_transitions(content, codings, member_ids),
    }


# ---------- 跨文档编码分布对比 ----------

ALL_CODINGS_SQL = """
    SELECT cg.id, cg.document_id, cg.code_id, cg.start, cg."end",
           cg.created_at, cg.updated_at, c.name AS code_name, c.color AS code_color
    FROM codings cg JOIN codes c ON c.id = cg.code_id
    ORDER BY cg.start, cg."end", cg.id
"""


@app.get("/api/code-distribution")
def code_distribution(codes: str | None = Query(default=None)) -> dict:
    """跨文档编码分布对比（单一快照计算，不修改任何标注）。

    对全部文档按所选编码汇总标注字符数、标注段数与占文档长度比例，文档按
    标题稳定排序（标题相同按 id）；同一编码在每篇文档内先合并重叠/相邻区间
    再统计，共享字符不重复计数。每行（编码）附带相对行内最高占比的差异
    百分比与首个合并区间（供前端定位）。同时返回：
      - missing_codes：在全部文档中都没有标注的所选编码；
      - empty_documents：没有任何所选编码标注的文档。

    查询参数 codes 为逗号分隔的编码 id（?codes=1,2）：缺省统计编码本全部
    编码；空串（?codes=）表示不统计任何编码。
    """
    with get_db() as conn:
        all_codes = {
            r["id"]: {"id": r["id"], "name": r["name"], "color": r["color"]}
            for r in conn.execute("SELECT id, name, color FROM codes ORDER BY id")
        }
        if codes is None:
            member_ids = sorted(all_codes)
        else:
            try:
                member_ids = sorted(
                    {int(x) for x in codes.split(",") if x.strip()}
                )
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="codes 参数格式错误：应为逗号分隔的编码 id",
                )
            unknown = [cid for cid in member_ids if cid not in all_codes]
            if unknown:
                raise HTTPException(
                    status_code=404, detail=f"编码不存在: {unknown}"
                )
        documents = [
            dict(r)
            for r in conn.execute(
                "SELECT id, title, content FROM documents ORDER BY title, id"
            ).fetchall()
        ]
        codings_by_doc: dict[int, list[dict]] = {}
        for row in conn.execute(ALL_CODINGS_SQL).fetchall():
            codings_by_doc.setdefault(row["document_id"], []).append(dict(row))

    rows = compute_distribution(documents, codings_by_doc, member_ids)
    missing_codes = [
        all_codes[cid]
        for cid, row in zip(member_ids, rows)
        if all(cell["char_count"] == 0 for cell in row["cells"])
    ]
    coded_doc_ids = {
        cell["document_id"]
        for row in rows
        for cell in row["cells"]
        if cell["char_count"] > 0
    }
    empty_documents = [
        {"id": d["id"], "title": d["title"]}
        for d in documents
        if d["id"] not in coded_doc_ids
    ]
    return {
        "codes": [all_codes[cid] for cid in member_ids],
        "documents": [
            {"id": d["id"], "title": d["title"], "length": len(d["content"])}
            for d in documents
        ],
        "rows": rows,
        "missing_codes": missing_codes,
        "empty_documents": empty_documents,
    }


# ---------- 标注抽样复核 ----------


def _review_plan_row(conn, document_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM review_plans WHERE document_id = ?", (document_id,)
    ).fetchone()
    return dict(row) if row else None


def _build_review_payload(conn, document_id: int, doc: dict) -> dict:
    """组装当前文档抽样复核队列（计划、配置、逐项明细与计数）。"""
    plan = _review_plan_row(conn, document_id)
    if plan is None:
        return {"document_id": document_id, "plan": None}

    fingerprint_now = content_fingerprint(doc["content"])
    signature_now = annotations_signature(list_codings(conn, document_id))
    # 文档正文或标注集合自生成后发生变化：旧队列仅供查看，需重新生成
    stale = (
        plan["fingerprint"] != fingerprint_now
        or plan["coding_signature"] != signature_now
    )

    config_rows = conn.execute(
        """
        SELECT pc.code_id, pc.sample_size, c.name AS code_name, c.color AS code_color,
               (SELECT COUNT(*) FROM codings cg
                WHERE cg.document_id = ? AND cg.code_id = pc.code_id) AS population
        FROM review_plan_codes pc
        LEFT JOIN codes c ON c.id = pc.code_id
        WHERE pc.plan_id = ?
        ORDER BY pc.code_id
        """,
        (document_id, plan["id"]),
    ).fetchall()

    content = doc["content"]
    item_rows = conn.execute(
        """
        SELECT ri.id, ri.coding_id, ri.code_id, ri.verdict, ri.note,
               ri.reviewed_at, ri.snap_start, ri.snap_end,
               cg.start AS cur_start, cg."end" AS cur_end,
               cg.code_id AS cur_code_id,
               c.name AS code_name, c.color AS code_color
        FROM review_items ri
        LEFT JOIN codings cg ON cg.id = ri.coding_id
        LEFT JOIN codes c ON c.id = COALESCE(cg.code_id, ri.code_id)
        WHERE ri.plan_id = ?
        ORDER BY ri.snap_start, ri.snap_end, ri.id
        """,
        (plan["id"],),
    ).fetchall()

    items: list[dict] = []
    counts = {"pending": 0, "approved": 0, "revision": 0}
    for r in item_rows:
        r = dict(r)
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        start, end = r["snap_start"], r["snap_end"]
        items.append(
            {
                "id": r["id"],
                "coding_id": r["coding_id"],
                "code_id": r["code_id"],
                "code_name": r["code_name"],
                "code_color": r["code_color"],
                "verdict": r["verdict"],
                "note": r["note"],
                "reviewed_at": r["reviewed_at"],
                "start": start,
                "end": end,
                "context": build_context(content, start, end),
                # 标注仍存在且区间未变时可直接定位正文，否则标记差异原因
                "missing": r["coding_id"] is None,
                "range_changed": (
                    r["coding_id"] is not None
                    and (r["cur_start"], r["cur_end"]) != (start, end)
                ),
                "code_changed": (
                    r["coding_id"] is not None and r["cur_code_id"] != r["code_id"]
                ),
            }
        )

    return {
        "document_id": document_id,
        "stale": stale,
        "plan": {
            "id": plan["id"],
            "fingerprint": plan["fingerprint"],
            "coding_signature": plan["coding_signature"],
            "created_at": plan["created_at"],
        },
        "config": [
            {
                "code_id": r["code_id"],
                "code_name": r["code_name"],
                "code_color": r["code_color"],
                "sample_size": r["sample_size"],
                "population": r["population"],
            }
            for r in config_rows
        ],
        "counts": counts,
        "items": items,
    }


@app.get("/api/documents/{document_id}/review")
def get_review(document_id: int) -> dict:
    """获取当前文档的标注抽样复核计划与复核队列。

    尚未生成计划时返回 {"plan": null}；文档内容指纹或标注集合快照与生成时
    不一致时 stale=true，提示复核者重新生成抽样（旧结果仍可查看）。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        return _build_review_payload(conn, document_id, doc)


@app.put("/api/documents/{document_id}/review")
def create_review_plan(document_id: int, body: ReviewPlanCreate) -> dict:
    """生成（或重新生成）当前文档的标注抽样复核计划。

    按请求中每种编码的抽样数量，以「文档内容指纹 + 编码 id + 标注 id」
    稳定抽签；同一快照下重复生成结果完全相同，文档 / 标注变化后重新生成
    样本随之更新。重新生成时覆盖旧计划；新样本中仍存在的同标注若此前已
    复核，则保留其结论与备注（按 plan × coding 继承）。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        seen: set[int] = set()
        samples: list[tuple[int, int]] = []
        for item in body.samples:
            if item.code_id in seen:
                raise HTTPException(
                    status_code=422,
                    detail=f"编码 {item.code_id} 在抽样配置中重复出现",
                )
            seen.add(item.code_id)
            get_code_or_404(conn, item.code_id)
            samples.append((item.code_id, item.sample_size))

        codings = list_codings(conn, document_id)
        if not codings:
            raise HTTPException(status_code=422, detail="当前文档还没有任何标注，无法抽样")

        # 继承旧计划中对同一标注的复核结论（重新生成后仍被抽中时保留）
        old_by_coding: dict[int, dict] = {}
        old = _review_plan_row(conn, document_id)
        if old is not None:
            for r in conn.execute(
                "SELECT coding_id, verdict, note FROM review_items"
                " WHERE plan_id = ? AND coding_id IS NOT NULL",
                (old["id"],),
            ).fetchall():
                old_by_coding[r["coding_id"]] = dict(r)

        fingerprint = content_fingerprint(doc["content"])
        picked = select_sample_codings(fingerprint, codings, samples)
        if not picked:
            raise HTTPException(
                status_code=422,
                detail="所选编码在当前文档中没有任何标注，抽样为空",
            )

        conn.execute(
            "INSERT INTO review_plans (document_id, fingerprint, coding_signature)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(document_id) DO UPDATE SET"
            " fingerprint = excluded.fingerprint,"
            " coding_signature = excluded.coding_signature,"
            " created_at = datetime('now')",
            (document_id, fingerprint, annotations_signature(codings)),
        )
        plan_id = conn.execute(
            "SELECT id FROM review_plans WHERE document_id = ?", (document_id,)
        ).fetchone()["id"]
        # 覆盖式重建：先清掉旧配置与旧条目（级联），再写入新计划内容
        conn.execute("DELETE FROM review_plan_codes WHERE plan_id = ?", (plan_id,))
        conn.execute("DELETE FROM review_items WHERE plan_id = ?", (plan_id,))
        conn.executemany(
            "INSERT INTO review_plan_codes (plan_id, code_id, sample_size)"
            " VALUES (?, ?, ?)",
            [(plan_id, code_id, size) for code_id, size in samples],
        )
        for c in picked:
            prev = old_by_coding.get(c["id"])
            conn.execute(
                "INSERT INTO review_items"
                " (plan_id, coding_id, code_id, verdict, note, reviewed_at,"
                "  snap_start, snap_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    c["id"],
                    c["code_id"],
                    prev["verdict"] if prev else "pending",
                    prev["note"] if prev else "",
                    prev["reviewed_at"] if prev else None,
                    c["start"],
                    c["end"],
                ),
            )
        return _build_review_payload(conn, document_id, doc)


@app.patch("/api/documents/{document_id}/review/items/{item_id}")
def update_review_item(
    document_id: int, item_id: int, body: ReviewVerdictUpdate
) -> dict:
    """更新单条复核结论（通过 / 需修订 / 重置为待处理）与备注。

    队列已过期（文档或标注在生成后发生变化）时不阻止记录复核意见，但响应
    中 stale=true，由前端提示「结果基于旧快照，建议重新生成抽样」。
    """
    with get_db() as conn:
        doc = get_document_or_404(conn, document_id)
        plan = _review_plan_row(conn, document_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="尚未生成抽样复核计划")
        row = conn.execute(
            "SELECT id FROM review_items WHERE id = ? AND plan_id = ?",
            (item_id, plan["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="复核条目不存在")

        if body.verdict is not None:
            reviewed = "datetime('now')" if body.verdict != "pending" else "NULL"
            conn.execute(
                f'UPDATE review_items SET verdict = ?, reviewed_at = {reviewed}'
                " WHERE id = ?",
                (body.verdict, item_id),
            )
        if body.note is not None:
            conn.execute(
                "UPDATE review_items SET note = ? WHERE id = ?",
                (body.note, item_id),
            )
        return _build_review_payload(conn, document_id, doc)

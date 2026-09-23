import { useEffect, useMemo, useState } from 'react'

/** 列表摘录：压缩空白并截断 */
function excerpt(text, max = 80) {
  if (text == null) return ''
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max)}…` : flat
}

const STATUS_META = {
  pending: { label: '待处理', className: 'rv-pending' },
  approved: { label: '通过', className: 'rv-approved' },
  revision: { label: '需修订', className: 'rv-revision' },
}

/** 单条复核条目的原文上下文与操作区（备注为本地草稿，保存后同步后端） */
function ReviewItemCard({ item, disabled, busyId, onPatch }) {
  const [note, setNote] = useState(item.note || '')
  const [saving, setSaving] = useState(false)

  // 批次刷新（他人 / 其它操作已写入备注）后同步后端值；正在编辑时不打断
  useEffect(() => {
    setNote(item.note || '')
  }, [item.note])

  async function patch(status) {
    setSaving(true)
    try {
      await onPatch(item.id, status, note)
    } catch {
      /* 错误已由上层提示；保留本地备注草稿便于重试 */
    } finally {
      setSaving(false)
    }
  }

  async function saveNoteOnly() {
    if (disabled || note === (item.note || '')) return
    await patch(item.status)
  }

  const meta = STATUS_META[item.status] ?? STATUS_META.pending
  const busy = busyId === item.id || saving

  return (
    <li className={`rv-item ${meta.className}${item.changed ? ' rv-changed' : ''}`}>
      <div className="rv-item-head">
        <span className="dot" style={{ background: item.color ?? '#9aa1ad' }} />
        <strong>{item.code_name ?? `编码 #${item.code_id}`}</strong>
        <code>
          [{item.start}, {item.end})
        </code>
        <span className={`rv-status-tag ${meta.className}`}>{meta.label}</span>
        {item.changed && <span className="rv-flag">区间/编码已变化</span>}
        {item.missing && <span className="rv-flag rv-flag-bad">原标注已删除</span>}
      </div>

      {item.text == null ? (
        <div className="rv-text rv-missing">
          原标注区间已超出当前正文长度，无法展示原文（抽样时区间 [{item.start},{' '}
          {item.end})）。
        </div>
      ) : (
        <div className="rv-text" title={item.text}>
          <span className="rv-ctx">{excerpt(item.context_before)}</span>
          <mark>{item.text}</mark>
          <span className="rv-ctx">{excerpt(item.context_after)}</span>
        </div>
      )}

      <textarea
        className="rv-note"
        rows={2}
        placeholder="复核备注：通过理由或需修订之处（可选）"
        value={note}
        disabled={disabled}
        onChange={(e) => setNote(e.target.value)}
        onBlur={saveNoteOnly}
      />
      <div className="rv-actions">
        <button
          type="button"
          className={item.status === 'approved' ? 'rv-btn-active rv-ok' : ''}
          disabled={disabled || busy}
          onClick={() => patch('approved')}
        >
          ✓ 通过
        </button>
        <button
          type="button"
          className={item.status === 'revision' ? 'rv-btn-active rv-bad' : ''}
          disabled={disabled || busy}
          onClick={() => patch('revision')}
        >
          ✎ 需修订
        </button>
        <button
          type="button"
          disabled={disabled || busy || item.status === 'pending'}
          onClick={() => patch('pending')}
          title="重置为待处理（清空处理时间）"
        >
          重置
        </button>
      </div>
    </li>
  )
}

/**
 * 标注抽样复核视图。
 *
 * 无批次时展示抽样设置（勾选编码 + 每种编码抽样数量）；生成后展示复核队列：
 * 顶部统计待处理 / 通过 / 需修订数量，逐条查看标注原文与前后文、标记结论
 * 并填写备注、点击定位切回正文闪烁高亮。文档或标注变化后批次标记为过期，
 * 只可读、需重新生成抽样（重新生成会清空此前结论）。
 */
export default function ReviewView({
  batch,
  loading,
  error,
  codes,
  codingCounts,
  onCreate,
  onRegenerate,
  onDelete,
  onPatch,
  onLocate,
}) {
  const [sizes, setSizes] = useState({}) // codeId -> 抽样数量（设置表单）
  const [submitting, setSubmitting] = useState(false)
  const [statusFilter, setStatusFilter] = useState('all')
  const [busyId, setBusyId] = useState(null)

  const usableCodes = useMemo(
    () => codes.filter((c) => (codingCounts[c.id] ?? 0) > 0),
    [codes, codingCounts]
  )

  // 进入设置态（无批次 / 批次被删除）时把每种编码默认抽样数量填为 1
  useEffect(() => {
    if (batch) return
    setSizes((cur) => {
      const next = { ...cur }
      for (const c of usableCodes) {
        if (next[c.id] == null) next[c.id] = 1
      }
      return next
    })
  }, [batch, usableCodes])

  async function handleGenerate() {
    const payload = usableCodes
      .filter((c) => Number(sizes[c.id]) > 0)
      .map((c) => ({
        code_id: c.id,
        sample_size: Math.max(0, Number(sizes[c.id]) || 0),
      }))
    if (payload.length === 0) return
    setSubmitting(true)
    try {
      await onCreate(payload)
      setStatusFilter('all')
    } catch {
      /* 错误已由上层提示，保留表单设置便于修改后重试 */
    } finally {
      setSubmitting(false)
    }
  }

  async function handlePatch(itemId, status, note) {
    setBusyId(itemId)
    try {
      await onPatch(itemId, status, note)
    } finally {
      setBusyId(null)
    }
  }

  // ---------- 无批次：抽样设置 ----------
  if (!batch) {
    return (
      <div className="rv-panel">
        {loading && <div className="cooc-status">复核批次加载中…</div>}
        {!loading && error && <div className="cooc-status error">{error}</div>}
        {!loading && !error && (
          <>
            <p className="speaker-summary">
              选择参与复核的编码并设置每种编码的抽样数量，系统将按文档指纹与
              标注 id 稳定抽取待复核标注；文档或标注不变时，刷新页面抽样结果保持一致。
            </p>
            {usableCodes.length === 0 ? (
              <div className="cooc-status">当前文档还没有任何标注，请先在正文中编码。</div>
            ) : (
              <>
                <ul className="rv-setup-list">
                  {usableCodes.map((c) => {
                    const total = codingCounts[c.id] ?? 0
                    const n = Math.max(0, Number(sizes[c.id]) || 0)
                    return (
                      <li key={c.id}>
                        <label>
                          <span className="dot" style={{ background: c.color }} />
                          <span className="code-name">{c.name}</span>
                          <span className="count">{total} 条标注</span>
                        </label>
                        <label className="rv-size">
                          抽样
                          <input
                            type="number"
                            min="0"
                            max={total}
                            value={sizes[c.id] ?? 1}
                            onChange={(e) =>
                              setSizes((cur) => ({
                                ...cur,
                                [c.id]: Math.max(0, Number(e.target.value) || 0),
                              }))
                            }
                          />
                          条{n >= total ? '（全部）' : ''}
                        </label>
                      </li>
                    )
                  })}
                </ul>
                <div className="rv-setup-actions">
                  <button
                    type="button"
                    className="primary-btn"
                    disabled={
                      submitting ||
                      !usableCodes.some((c) => Number(sizes[c.id]) > 0)
                    }
                    onClick={handleGenerate}
                  >
                    {submitting ? '生成中…' : '生成抽样'}
                  </button>
                  <span className="hint">
                    抽样数量超过该编码标注数时抽取全部；生成后可随时重新抽样（会清空已有复核结论）。
                  </span>
                </div>
              </>
            )}
          </>
        )}
      </div>
    )
  }

  // ---------- 复核队列 ----------
  const { counts, items } = batch
  const visibleItems = items.filter((it) =>
    statusFilter === 'all' ? true : it.status === statusFilter
  )

  return (
    <div className="rv-panel">
      <div className="rv-summary-bar">
        <div className="rv-counts">
          <span className="rv-count rv-pending">待处理 {counts.pending}</span>
          <span className="rv-count rv-approved">通过 {counts.approved}</span>
          <span className="rv-count rv-revision">需修订 {counts.revision}</span>
          <span className="rv-total">共 {counts.total} 条</span>
        </div>
        <div className="rv-head-actions">
          <button type="button" onClick={onRegenerate} title="按当前文档与标注重新抽样，清空已有结论">
            重新生成抽样
          </button>
          <button
            type="button"
            onClick={() => {
              if (window.confirm('丢弃当前抽样批次及全部复核结论？')) onDelete()
            }}
          >
            删除批次
          </button>
        </div>
      </div>

      {batch.stale && (
        <div className="rv-stale-banner">
          <strong>抽样已过期：</strong>
          {batch.stale_reasons.includes('document_changed') && '文档正文已变化；'}
          {batch.stale_reasons.includes('codings_changed') && '标注已新增 / 修改 / 删除；'}
          队列仅供查看，复核操作已暂停。请
          <button type="button" className="link-btn" onClick={onRegenerate}>
            重新生成抽样
          </button>
          后继续。
        </div>
      )}

      <div className="rv-filter-bar">
        {[
          ['all', `全部 ${counts.total}`],
          ['pending', `待处理 ${counts.pending}`],
          ['approved', `通过 ${counts.approved}`],
          ['revision', `需修订 ${counts.revision}`],
        ].map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={statusFilter === key ? 'rv-filter-active' : ''}
            onClick={() => setStatusFilter(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {loading && <div className="cooc-status">复核队列加载中…</div>}
      {!loading && error && <div className="cooc-status error">{error}</div>}

      {!loading && !error && visibleItems.length === 0 && (
        <div className="cooc-status">
          {items.length === 0
            ? '本批次没有抽到任何标注，可重新生成抽样。'
            : '当前筛选状态下没有条目。'}
        </div>
      )}

      {!loading && !error && visibleItems.length > 0 && (
        <ul className="rv-list">
          {visibleItems.map((it) => (
            <li key={it.id}>
              <ReviewItemCard
                item={it}
                disabled={batch.stale}
                busyId={busyId}
                onPatch={handlePatch}
              />
              <button
                type="button"
                className="rv-locate-side"
                disabled={it.missing || it.text == null}
                title="切回正文并闪烁定位该标注"
                onClick={() => onLocate(it)}
              >
                定位正文
              </button>
            </li>
          ))}
        </ul>
      )}

      <p className="hint">
        口径：抽样按 sha256(文档指纹 | 编码 id | 标注 id) 稳定排序取前 N 条，
        文档或标注不变时刷新页面结果不变；批次生成于 {batch.created_at}
        ，文档内容或标注集合变化后批次自动标记过期，需重新生成。
      </p>
    </div>
  )
}

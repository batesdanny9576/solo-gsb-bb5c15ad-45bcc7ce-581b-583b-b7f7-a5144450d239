import { useEffect, useMemo, useState } from 'react'

const VERDICT_LABEL = { pending: '待处理', approved: '通过', revision: '需修订' }
const VERDICT_CLASS = {
  pending: 'rv-pending',
  approved: 'rv-approved',
  revision: 'rv-revision',
}

function flatten(text, max = 140) {
  const flat = (text || '').replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max)}…` : flat
}

/**
 * 标注抽样复核视图。
 *
 * - 无计划：为每种编码设置抽样数量（0 = 不抽该编码），生成抽样计划；
 * - 有计划：展示待处理 / 通过 / 需修订计数与逐条队列，可标记通过 / 需修订
 *   并填写备注，点击「定位正文」切回正文闪烁定位；
 * - 抽样按文档指纹与标注 id 稳定生成，刷新后结果不变；文档正文或标注集合
 *   变化后后端标记 stale，提示重新生成（重新生成会保留仍被抽中标注的旧结论）。
 */
export default function ReviewView({
  docId,
  codebook,
  review,
  loading,
  error,
  onCreatePlan,
  onUpdateItem,
  onLocate,
}) {
  const [sizes, setSizes] = useState({})
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState(null)
  const [filter, setFilter] = useState('all')
  const [busyItem, setBusyItem] = useState(null)

  // 队列数据由 App 随文档与标注快照变化自动拉取；切换文档时重置状态筛选
  useEffect(() => {
    setFilter('all')
  }, [docId])

  // 切到当前文档尚无计划时，用各编码的标注数预填默认抽样数量
  useEffect(() => {
    if (!review || review.plan) return
    const next = {}
    for (const c of codebook) {
      const pop = c.usage_count ?? 0
      next[c.id] = pop > 0 ? Math.min(3, pop) : 0
    }
    setSizes(next)
  }, [review, codebook])

  const plan = review?.plan ?? null

  const populationById = useMemo(() => {
    const m = new Map()
    if (plan) {
      for (const cfg of review.config) m.set(cfg.code_id, cfg.population)
    } else {
      for (const c of codebook) m.set(c.id, c.usage_count ?? 0)
    }
    return m
  }, [review, plan, codebook])

  async function handleGenerate() {
    const samples = Object.entries(sizes)
      .map(([id, n]) => ({ code_id: Number(id), sample_size: Number(n) }))
      .filter((s) => Number.isInteger(s.sample_size) && s.sample_size > 0)
    if (samples.length === 0) {
      setFormError('请至少为一种编码设置大于 0 的抽样数量')
      return
    }
    setFormError(null)
    setSubmitting(true)
    try {
      await onCreatePlan(samples)
    } catch (e) {
      setFormError(e.message || String(e))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleRegenerate() {
    if (!plan) return
    if (!window.confirm('重新生成抽样将覆盖当前队列；仍被抽中的标注会保留已有复核结论。是否继续？')) {
      return
    }
    setSubmitting(true)
    try {
      const samples = review.config.map((c) => ({
        code_id: c.code_id,
        sample_size: c.sample_size,
      }))
      await onCreatePlan(samples)
    } catch (e) {
      setFormError(e.message || String(e))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleVerdict(item, verdict) {
    // 直接点击结论按钮时备注可能尚未失焦保存：读取 DOM 中最新备注一并提交，
    // 避免用闭包里的旧 note 覆盖刚输入的内容
    const ta = document.querySelector(
      `.rv-item[data-item-id="${item.id}"] .rv-note`
    )
    const note = ta ? ta.value : item.note
    setBusyItem(item.id)
    try {
      await onUpdateItem(item.id, { verdict, note })
    } finally {
      setBusyItem(null)
    }
  }

  function handleNoteBlur(item, note) {
    if (note === item.note) return
    onUpdateItem(item.id, { note })
  }

  const items = review?.items ?? []
  const counts = review?.counts ?? { pending: 0, approved: 0, revision: 0 }
  const visibleItems =
    filter === 'all' ? items : items.filter((it) => it.verdict === filter)

  // ---------- 无计划：抽样配置表单 ----------
  if (loading && !review) {
    return (
      <div className="rv-panel">
        <div className="cooc-status">复核队列加载中…</div>
      </div>
    )
  }

  if (!loading && !plan) {
    return (
      <div className="rv-panel">
        <h3 className="rv-title">生成标注抽样复核</h3>
        <p className="hint">
          为每种编码设置抽样数量，系统按当前文档内容指纹与标注 id 稳定抽取；同一快照下
          重复生成结果相同，刷新页面也不会变化。文档或标注变化后可重新生成。
        </p>
        <ul className="rv-config-list">
          {codebook.length === 0 && <li className="empty">编码本为空，请先在侧栏新建编码。</li>}
          {codebook.map((c) => {
            const pop = populationById.get(c.id) ?? 0
            return (
              <li key={c.id} className={pop === 0 ? 'rv-code-empty' : ''}>
                <label>
                  <span className="dot" style={{ background: c.color }} />
                  <span className="rv-code-name">{c.name}</span>
                  <span className="count">当前文档 {pop} 条</span>
                </label>
                <input
                  type="number"
                  min="0"
                  max={Math.max(pop, 0)}
                  value={sizes[c.id] ?? 0}
                  disabled={pop === 0}
                  onChange={(e) =>
                    setSizes((cur) => ({
                      ...cur,
                      [c.id]: Math.max(0, Number(e.target.value) || 0),
                    }))
                  }
                />
                <span className="hint">条</span>
              </li>
            )
          })}
        </ul>
        {formError && <div className="error-banner rv-error">{formError}</div>}
        {error && <div className="cooc-status error">{error}</div>}
        <div>
          <button
            className="primary-btn"
            disabled={submitting || codebook.length === 0}
            onClick={handleGenerate}
          >
            {submitting ? '生成中…' : '生成抽样'}
          </button>
        </div>
      </div>
    )
  }

  // ---------- 有计划：复核队列 ----------
  return (
    <div className="rv-panel">
      <div className="rv-head">
        <h3 className="rv-title">标注抽样复核</h3>
        <div className="rv-head-actions">
          <button onClick={handleRegenerate} disabled={submitting}>
            {submitting ? '处理中…' : '重新生成抽样'}
          </button>
        </div>
      </div>

      <p className="hint rv-plan-meta">
        抽样生成于 {plan.created_at} · 共 {items.length} 条待复核标注 ·
        口径：sha256(文档指纹 | 编码 id | 标注 id) 稳定抽取
      </p>

      {review?.stale && (
        <div className="rv-stale">
          ⚠ 文档正文或标注在抽样生成后已发生变化，当前队列基于旧快照。可继续记录复核意见，
          或点击「重新生成抽样」按最新文档抽取（仍被抽中的标注会保留已有结论）。
        </div>
      )}

      <div className="rv-counts">
        {[
          ['all', `全部 ${items.length}`],
          ['pending', `待处理 ${counts.pending}`],
          ['approved', `通过 ${counts.approved}`],
          ['revision', `需修订 ${counts.revision}`],
        ].map(([key, label]) => (
          <button
            key={key}
            className={`rv-count ${VERDICT_CLASS[key] ?? ''} ${filter === key ? 'active' : ''}`}
            onClick={() => setFilter(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {loading && <div className="cooc-status">复核队列加载中…</div>}
      {!loading && error && <div className="cooc-status error">{error}</div>}

      {!loading && !error && visibleItems.length === 0 && (
        <div className="cooc-status">
          {items.length === 0 ? '抽样队列为空。' : '当前筛选下没有条目。'}
        </div>
      )}

      <ul className="rv-item-list">
        {visibleItems.map((item) => (
          <li
            key={item.id}
            data-item-id={item.id}
            className={`rv-item ${VERDICT_CLASS[item.verdict]}`}
          >
            <div className="rv-item-head">
              <span className="dot" style={{ background: item.code_color || '#9ca3af' }} />
              <strong className="rv-code-name">{item.code_name ?? '（编码已删除）'}</strong>
              <code>
                [{item.start}, {item.end})
              </code>
              <span className={`rv-badge ${VERDICT_CLASS[item.verdict]}`}>
                {VERDICT_LABEL[item.verdict]}
              </span>
              {item.missing && <span className="rv-warn">标注已删除</span>}
              {!item.missing && item.range_changed && (
                <span className="rv-warn">区间已变更</span>
              )}
              {!item.missing && item.code_changed && (
                <span className="rv-warn">已改挂其它编码</span>
              )}
              {!item.missing && (
                <button
                  type="button"
                  className="link-btn rv-locate"
                  onClick={() => onLocate(item)}
                >
                  定位正文
                </button>
              )}
            </div>
            <div className="rv-context">
              <span className="rv-ctx-side">{flatten(item.context.before, 40)}</span>
              <mark>{flatten(item.context.text)}</mark>
              <span className="rv-ctx-side">{flatten(item.context.after, 40)}</span>
            </div>
            <div className="rv-actions">
              <button
                className={item.verdict === 'approved' ? 'rv-btn-on rv-ok' : ''}
                disabled={busyItem === item.id || item.missing}
                onClick={() => handleVerdict(item, 'approved')}
              >
                ✓ 通过
              </button>
              <button
                className={item.verdict === 'revision' ? 'rv-btn-on rv-bad' : ''}
                disabled={busyItem === item.id || item.missing}
                onClick={() => handleVerdict(item, 'revision')}
              >
                ✎ 需修订
              </button>
              {item.verdict !== 'pending' && (
                <button
                  disabled={busyItem === item.id || item.missing}
                  onClick={() => handleVerdict(item, 'pending')}
                >
                  重置为待处理
                </button>
              )}
            </div>
            <textarea
              className="rv-note"
              rows={2}
              placeholder="复核备注（修订建议、疑问等，失焦自动保存）"
              defaultValue={item.note}
              disabled={item.missing}
              key={`${item.id}-${item.verdict}-${item.reviewed_at ?? ''}`}
              onBlur={(e) => handleNoteBlur(item, e.target.value)}
            />
          </li>
        ))}
      </ul>

      <p className="hint">
        复核结论与备注实时保存并持久化；抽样结果只取决于文档指纹与标注 id，刷新页面、
        重启服务均保持不变。文档或标注变化后顶部会出现过期提示，重新生成即按最新快照抽样。
      </p>
    </div>
  )
}

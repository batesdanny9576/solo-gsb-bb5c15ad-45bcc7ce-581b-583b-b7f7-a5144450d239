import { useMemo, useRef, useState } from 'react'
import { api } from '../api'
import TextView from './TextView'

/**
 * 文档版本迁移对话框：
 *   input   阶段编辑新正文 → 调迁移预检接口（不落库）；
 *   preview 阶段分三栏列出保留 / 待确认 / 失配项，展示新旧上下文：
 *             - 保留（唯一匹配自动定位，可核对）；
 *             - 待确认：多重匹配（从候选位置中选择）、内容改动（接受建议区间 /
 *               放弃 / 在下方新正文里拖选手选）、文本删除（放弃 / 手选）；
 *             - 失配（被替换，确认迁移时自动丢弃）。
 *           所有待确认项处理完才能确认；
 *   done    阶段展示迁移结果。
 * 确认时后端校验原文版本并在单事务中更新正文与全部标注，任一校验失败不落库；
 * 取消 / 关闭不产生任何写入。
 */
export default function MigrationDialog({ docId, oldContent, onClose, onMigrated }) {
  const [newContent, setNewContent] = useState(oldContent)
  const [phase, setPhase] = useState('input') // input | busy | preview | committing | done
  const [error, setError] = useState(null)
  const [preview, setPreview] = useState(null)
  const [decisions, setDecisions] = useState({}) // {codingId: {kind, start, end}}
  const [pickingId, setPickingId] = useState(null) // 正在新正文中拖选手选的待确认项
  const [flashRange, setFlashRange] = useState(null)
  const [result, setResult] = useState(null)
  const paneRef = useRef(null)

  const newLength = newContent.length

  // 新正文面板上的叠加标注：已确定区间的保留 / 待确认项（供整体预览）
  const previewCodings = useMemo(() => {
    if (!preview) return []
    const out = []
    for (const it of preview.items) {
      if (it.status === 'kept' && it.suggestion) {
        out.push({
          id: `kept-${it.coding_id}`,
          start: it.suggestion.start,
          end: it.suggestion.end,
          code_name: it.code_name,
          code_color: it.code_color,
        })
      } else if (it.status === 'ambiguous' || it.status === 'modified' || it.status === 'deleted') {
        const d = decisions[it.coding_id]
        if (d && (d.kind === 'candidate' || d.kind === 'suggestion' || d.kind === 'manual')) {
          out.push({
            id: `dec-${it.coding_id}`,
            start: d.start,
            end: d.end,
            code_name: `${it.code_name}（待确认）`,
            code_color: it.code_color,
          })
        }
      }
    }
    return out
  }, [preview, decisions])

  const pendingItems = useMemo(
    () => (preview ? preview.items.filter((it) => PENDING.has(it.status)) : []),
    [preview]
  )
  const unresolved = useMemo(
    () => pendingItems.filter((it) => !decisions[it.coding_id]),
    [pendingItems, decisions]
  )

  async function handlePreview() {
    if (!newContent) {
      setError('新正文不能为空')
      return
    }
    setPhase('busy')
    setError(null)
    try {
      const data = await api.previewMigration(docId, newContent)
      setPreview(data)
      setPickingId(null)
      // 内容改动项默认填入建议区间，用户仍可改选 / 放弃
      const initial = {}
      for (const it of data.items) {
        if (it.status === 'modified' && it.suggestion) {
          initial[it.coding_id] = {
            kind: 'suggestion',
            start: it.suggestion.start,
            end: it.suggestion.end,
          }
        }
      }
      setDecisions(initial)
      setPhase('preview')
    } catch (e) {
      setError(e.message || String(e))
      setPhase('input')
    }
  }

  async function handleCommit() {
    if (!preview || unresolved.length > 0) return
    const payload = {
      old_fingerprint: preview.old_fingerprint,
      new_content: newContent,
      decisions: pendingItems.map((it) => {
        const d = decisions[it.coding_id]
        if (d.kind === 'discard') {
          return { coding_id: it.coding_id, action: 'discard' }
        }
        return {
          coding_id: it.coding_id,
          action: 'remap',
          start: d.start,
          end: d.end,
        }
      }),
    }
    setPhase('committing')
    setError(null)
    try {
      const data = await api.commitMigration(docId, payload)
      setResult(data)
      setPhase('done')
      await onMigrated()
    } catch (e) {
      setError(e.message || String(e))
      setPhase('preview')
    }
  }

  function scrollToRange(range) {
    const spans = paneRef.current?.querySelectorAll('[data-s]') ?? []
    for (const span of spans) {
      const s = Number(span.dataset.s)
      const e = Number(span.dataset.e)
      if (s < range.end && e > range.start) {
        span.scrollIntoView({ behavior: 'smooth', block: 'center' })
        break
      }
    }
  }

  function flash(range) {
    setFlashRange(range)
    scrollToRange(range)
    setTimeout(() => setFlashRange(null), 1800)
  }

  function chooseCandidate(it, cand) {
    setPickingId(null)
    setDecisions((cur) => ({
      ...cur,
      [it.coding_id]: { kind: 'candidate', start: cand.start, end: cand.end },
    }))
    flash({ start: cand.start, end: cand.end })
  }

  function acceptSuggestion(it) {
    setPickingId(null)
    setDecisions((cur) => ({
      ...cur,
      [it.coding_id]: {
        kind: 'suggestion',
        start: it.suggestion.start,
        end: it.suggestion.end,
      },
    }))
    flash({ start: it.suggestion.start, end: it.suggestion.end })
  }

  function discard(it) {
    setPickingId(null)
    setDecisions((cur) => ({ ...cur, [it.coding_id]: { kind: 'discard' } }))
  }

  function startManualPick(it) {
    setPickingId(it.coding_id)
    setFlashRange(null)
  }

  function handleNewTextSelect(range) {
    if (pickingId == null) return
    const id = pickingId
    setDecisions((cur) => ({
      ...cur,
      [id]: { kind: 'manual', start: range.start, end: range.end },
    }))
    setPickingId(null)
    flash(range)
  }

  const decisionOf = (it) => decisions[it.coding_id]

  return (
    <div
      className="modal-backdrop"
      onClick={() => {
        if (phase !== 'committing') onClose()
      }}
    >
      <div className="modal migration-modal" onClick={(e) => e.stopPropagation()}>
        <h3>文档版本迁移</h3>

        {phase === 'done' && result ? (
          <DonePanel result={result} onClose={onClose} />
        ) : (
          <>
            {phase === 'input' && (
              <div className="migration-input">
                <p className="hint">
                  粘贴或编辑<strong>当前文档的新版本正文</strong>。预检依据文本差异把旧标注区间映射到
                  新位置：唯一匹配自动保留；多重匹配、内容改动或文本删除列为待确认；被替换的标注列为
                  失配。预检与取消均不写入，确认时才在单事务中更新正文与全部标注。
                </p>
                <textarea
                  className="migration-textarea"
                  rows={14}
                  value={newContent}
                  onChange={(e) => setNewContent(e.target.value)}
                />
                <p className="hint">
                  旧正文 {oldContent.length} 字 · 新正文 {newLength} 字
                </p>
                {error && <div className="error-banner import-error">{error}</div>}
              </div>
            )}

            {phase === 'busy' && <div className="empty">正在比对文本差异…</div>}
            {phase === 'committing' && <div className="empty">正在执行迁移…</div>}

            {(phase === 'preview' || phase === 'committing') && preview && (
              <PreviewBody
                preview={preview}
                items={preview.items}
                decisionOf={decisionOf}
                unresolved={unresolved}
                pickingId={pickingId}
                paneRef={paneRef}
                flashRange={flashRange}
                previewCodings={previewCodings}
                newContent={newContent}
                onLocate={flash}
                onChooseCandidate={chooseCandidate}
                onAcceptSuggestion={acceptSuggestion}
                onDiscard={discard}
                onStartManual={startManualPick}
                onSelectNewText={handleNewTextSelect}
              />
            )}

            {phase === 'preview' && error && (
              <div className="error-banner import-error">{error}</div>
            )}

            <div className="modal-actions">
              {phase === 'preview' && (
                <button className="link-btn" onClick={() => setPhase('input')}>
                  返回编辑新正文
                </button>
              )}
              {phase !== 'committing' && (
                <button className="link-btn" onClick={onClose}>
                  {phase === 'input' ? '取消' : '关闭'}
                </button>
              )}
              {phase === 'input' && (
                <button className="primary-btn" onClick={handlePreview}>
                  生成迁移预览
                </button>
              )}
              {phase === 'preview' && (
                <button
                  className="primary-btn"
                  onClick={handleCommit}
                  disabled={unresolved.length > 0}
                  title={unresolved.length > 0 ? '仍有待确认项未处理' : undefined}
                >
                  {unresolved.length > 0
                    ? `确认迁移（${unresolved.length} 项待确认）`
                    : '确认迁移'}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

const PENDING = new Set(['ambiguous', 'modified', 'deleted'])

const STATUS_TAG = {
  kept: { label: '保留', cls: 'tag-created' },
  ambiguous: { label: '多重匹配', cls: 'tag-unknown' },
  modified: { label: '内容改动', cls: 'tag-unknown' },
  deleted: { label: '文本删除', cls: 'tag-unknown' },
  mismatch: { label: '失配', cls: 'tag-invalid' },
}

function Ctx({ ctx, onLocate, actionLabel }) {
  if (!ctx) return null
  return (
    <div className="mig-context">
      <code>
        [{ctx.start}, {ctx.end})
      </code>
      <span className="mig-ctx-text">
        …{ctx.before}
        <mark>{ctx.text || '∅'}</mark>
        {ctx.after}…
      </span>
      {onLocate && (
        <button className="link-btn" onClick={() => onLocate({ start: ctx.start, end: Math.max(ctx.end, ctx.start + 1) })}>
          {actionLabel || '定位'}
        </button>
      )}
    </div>
  )
}

function DecisionBadge({ decision }) {
  if (!decision) return <span className="mig-undecided">未处理</span>
  if (decision.kind === 'discard') return <span className="mig-decided-bad">将放弃</span>
  const label =
    decision.kind === 'candidate'
      ? '已选候选'
      : decision.kind === 'suggestion'
        ? '采用建议'
        : '手选区间'
  return (
    <span className="mig-decided-ok">
      {label} [{decision.start}, {decision.end})
    </span>
  )
}

function ItemRow({
  it,
  decision,
  picking,
  onLocate,
  onChooseCandidate,
  onAcceptSuggestion,
  onDiscard,
  onStartManual,
}) {
  const tag = STATUS_TAG[it.status]
  return (
    <li className={`mig-item mig-${it.status}${picking ? ' picking' : ''}`}>
      <div className="import-item-head">
        <span className={`tag ${tag.cls}`}>{tag.label}</span>
        <span className="dot" style={{ background: it.code_color || '#c7ccd6' }} />
        <strong>{it.code_name}</strong>
        <code>
          旧 [{it.old_start}, {it.old_end})
        </code>
        {PENDING.has(it.status) && <DecisionBadge decision={decision} />}
      </div>

      <div className="mig-old">
        <span className="mig-side-label">旧</span>
        <Ctx ctx={it.old_context} />
      </div>

      {it.status === 'kept' && it.suggestion && (
        <div className="mig-new">
          <span className="mig-side-label">新</span>
          <Ctx
            ctx={it.suggestion}
            onLocate={onLocate}
            actionLabel="在新正文中定位"
          />
        </div>
      )}

      {it.status === 'ambiguous' && (
        <div className="mig-candidates">
          <div className="mig-side-label">候选</div>
          <ul>
            {it.candidates.map((c, i) => {
              const active =
                decision && decision.kind === 'candidate' && decision.start === c.start
              return (
                <li key={i} className={active ? 'active' : ''}>
                  <label>
                    <input
                      type="radio"
                      name={`cand-${it.coding_id}`}
                      checked={!!active}
                      onChange={() => onChooseCandidate(it, c)}
                    />
                    <span className="mig-ctx-text">
                      …{c.before}
                      <mark>{c.text}</mark>
                      {c.after}…
                    </span>
                    <code>
                      [{c.start}, {c.end})
                    </code>
                  </label>
                  <button className="link-btn" onClick={() => onLocate({ start: c.start, end: c.end })}>
                    定位
                  </button>
                  {c.default && <span className="mig-default-hint">差异对齐默认</span>}
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {it.status === 'modified' && it.suggestion && (
        <div className="mig-new">
          <span className="mig-side-label">建议</span>
          <Ctx ctx={it.suggestion} onLocate={onLocate} actionLabel="定位建议" />
        </div>
      )}

      {it.status === 'deleted' && it.anchor && (
        <div className="mig-new">
          <span className="mig-side-label">锚点</span>
          <Ctx
            ctx={it.anchor}
            onLocate={onLocate}
            actionLabel="定位删除位置"
          />
        </div>
      )}

      {it.status === 'mismatch' && it.new_region && (
        <div className="mig-new">
          <span className="mig-side-label">新</span>
          <Ctx ctx={it.new_region} onLocate={onLocate} actionLabel="在新正文中查看" />
        </div>
      )}

      {PENDING.has(it.status) && (
        <div className="mig-actions">
          {it.status === 'ambiguous' && (
            <span className="hint">从上方候选中选择一个位置，或在新正文中手选：</span>
          )}
          {it.status === 'modified' && (
            <button
              className={decision?.kind === 'suggestion' ? 'primary-btn' : ''}
              onClick={() => onAcceptSuggestion(it)}
            >
              采用建议区间
            </button>
          )}
          <button className={picking ? 'primary-btn' : ''} onClick={() => onStartManual(it)}>
            {picking ? '请在下方新正文中拖选…' : '在新正文中手选区间'}
          </button>
          <button
            className={decision?.kind === 'discard' ? 'mig-discard-active' : ''}
            onClick={() => onDiscard(it)}
          >
            放弃该标注
          </button>
        </div>
      )}

      {it.status === 'mismatch' && (
        <div className="import-item-detail bad">{it.detail}</div>
      )}
      {PENDING.has(it.status) && !decision && (
        <div className="import-item-detail bad">待处理：请选择候选 / 手选区间或放弃</div>
      )}
    </li>
  )
}

function Section({ title, items, kind, ...rowProps }) {
  if (items.length === 0) return null
  const open = kind !== 'kept'
  return (
    <details open={open} className="import-section">
      <summary>
        {title}（{items.length}）
      </summary>
      <ul className="import-item-list mig-item-list">
        {items.map((it) => (
          <ItemRow key={it.coding_id} it={it} kind={kind} {...rowProps} />
        ))}
      </ul>
    </details>
  )
}

function PreviewBody({
  preview,
  items,
  decisionOf,
  unresolved,
  pickingId,
  paneRef,
  flashRange,
  previewCodings,
  newContent,
  onLocate,
  onChooseCandidate,
  onAcceptSuggestion,
  onDiscard,
  onStartManual,
  onSelectNewText,
}) {
  const kept = items.filter((it) => it.status === 'kept')
  const ambiguous = items.filter((it) => it.status === 'ambiguous')
  const modified = items.filter((it) => it.status === 'modified')
  const deleted = items.filter((it) => it.status === 'deleted')
  const mismatch = items.filter((it) => it.status === 'mismatch')
  const pending = [...ambiguous, ...modified, ...deleted]
  const s = preview.summary

  const rowProps = {
    onLocate,
    onChooseCandidate,
    onAcceptSuggestion,
    onDiscard,
    onStartManual,
  }
  const rowPropsFor = (it) => ({
    ...rowProps,
    decision: decisionOf(it),
    picking: pickingId === it.coding_id,
  })

  return (
    <div className="migration-preview">
      <div className="import-stat-row">
        <span className="import-stat created">保留 {s.kept}</span>
        <span className={`import-stat ${s.pending ? 'unknown' : ''}`}>
          待确认 {s.pending}
        </span>
        <span className={`import-stat ${s.mismatch ? 'invalid' : ''}`}>
          失配 {s.mismatch}
        </span>
        <span className="hint">共 {s.total} 条 · 新正文 {preview.new_length} 字</span>
      </div>

      {unresolved.length > 0 ? (
        <p className="import-block-hint">
          还有 {unresolved.length} 条待确认项未处理（多重匹配 / 内容改动 / 文本删除），
          全部处理后才能确认迁移。
        </p>
      ) : (
        <p className="hint import-ok-hint">
          待确认项已全部处理。确认后将在单事务中更新正文与全部标注；
          {s.mismatch > 0 ? `失配的 ${s.mismatch} 条标注将被丢弃；` : ''}
          取消则原文档保持不变。
        </p>
      )}

      <div className="migration-sections">
        <Section title="保留（唯一匹配，自动定位）" items={kept} kind="kept" {...rowProps} />
        <PendingSection
          pending={pending}
          ambiguous={ambiguous}
          modified={modified}
          deleted={deleted}
          rowPropsFor={rowPropsFor}
        />
        <Section title="失配（被替换，确认后丢弃）" items={mismatch} kind="mismatch" {...rowProps} />
      </div>

      <div className={`migration-new-pane${pickingId != null ? ' picking' : ''}`}>
        <div className="mig-pane-head">
          <strong>新正文预览</strong>
          <span className="hint">
            {pickingId != null
              ? '在下方文本中拖选该标注的新区间'
              : '点击各条目「定位」在此查看候选位置；高亮为已确定的迁移区间'}
          </span>
        </div>
        <div ref={paneRef} className="migration-text-view-wrap">
          <TextView
            content={newContent}
            codings={previewCodings}
            selection={null}
            flashRange={flashRange}
            onSelect={onSelectNewText}
          />
        </div>
      </div>
    </div>
  )
}

function PendingSection({ pending, ambiguous, modified, deleted, rowPropsFor }) {
  if (pending.length === 0) return null
  return (
    <details open className="import-section mig-pending-section">
      <summary>
        待确认（{pending.length}：多重匹配 {ambiguous.length} · 内容改动 {modified.length} ·
        文本删除 {deleted.length}）
      </summary>
      <ul className="import-item-list mig-item-list">
        {pending.map((it) => (
          <ItemRow key={it.coding_id} it={it} kind="pending" {...rowPropsFor(it)} />
        ))}
      </ul>
    </details>
  )
}

function DonePanel({ result, onClose }) {
  const s = result.summary
  return (
    <div className="import-done">
      <div className="batch-result">
        迁移完成：自动保留 {s.kept} 条，重新指定 {s.remapped} 条，
        手动放弃 {s.discarded} 条，失配丢弃 {s.dropped} 条。
      </div>
      <p className="hint">正文与全部标注已在同一事务中更新，当前文档共 {result.codings.length} 条标注。</p>
      <div className="modal-actions">
        <button className="primary-btn" onClick={onClose}>
          关闭并查看新正文
        </button>
      </div>
    </div>
  )
}

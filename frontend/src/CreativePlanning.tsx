import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import {
  Archive,
  ArrowDown,
  ArrowUp,
  BookOpenText,
  Check,
  ChevronRight,
  CircleDashed,
  Clock3,
  GitBranch,
  History,
  LoaderCircle,
  Merge,
  Pencil,
  Plus,
  Save,
  Scissors,
  Sparkles,
  UserRound,
  UsersRound,
  X,
} from 'lucide-react'
import type {
  CreativeBrief,
  CreativeChapter,
  CreativeCharacter,
  CreativePlanningWorkspace,
  CreativeProposal,
  CreativeRevisionHistory,
  CreativeSection,
} from './types'

type EntityKind = 'proposal' | 'character' | 'chapter' | 'section'
type EditableEntity = CreativeProposal | CreativeCharacter | CreativeChapter | CreativeSection
type Editor = { kind: EntityKind; mode: 'create' | 'edit'; parentId?: string; data: Record<string, string | number> }

const planningApi = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  const headers = new Headers(options?.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...options, headers })
  if (!response.ok) {
    const raw = await response.text()
    try {
      const parsed = JSON.parse(raw)
      throw new Error(parsed.detail || raw)
    } catch (error) {
      if (error instanceof SyntaxError) throw new Error(raw || `请求失败：${response.status}`)
      throw error
    }
  }
  return response.json()
}

const formatTime = (value: string) => new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
}).format(new Date(value))

const entityLabel: Record<EntityKind | 'brief', string> = {
  brief: '创作简报', proposal: '剧情提案', character: '角色卡', chapter: '章节', section: '小节',
}

const emptyEditor = (kind: EntityKind, parentId?: string): Editor => {
  if (kind === 'proposal') return { kind, mode: 'create', data: { title: '', synopsis: '', core_conflict: '', ending: '' } }
  if (kind === 'character') return { kind, mode: 'create', data: { name: '', identity: '', goal: '', obstacle: '', personality: '', appearance: '', voice: '', relationships: '', reference_notes: '' } }
  if (kind === 'chapter') return { kind, mode: 'create', data: { title: '', summary: '', pacing_goal: '', planned_seconds: 0 } }
  return { kind, mode: 'create', parentId, data: { title: '', summary: '', pacing_goal: '', planned_seconds: 0 } }
}

const editEntity = (kind: EntityKind, entity: EditableEntity): Editor => {
  const data: Record<string, string | number> = {}
  Object.entries(entity).forEach(([key, value]) => {
    if (typeof value === 'string' || typeof value === 'number') data[key] = value
  })
  return { kind, mode: 'edit', parentId: kind === 'section' ? (entity as CreativeSection).chapter_id : undefined, data }
}

function VersionHistory({ history, onClose }: { history: CreativeRevisionHistory; onClose: () => void }) {
  return <div className="modal-backdrop planning-modal-backdrop">
    <section className="planning-history" role="dialog" aria-modal="true" aria-label="版本历史">
      <header><div><span className="eyebrow">不可变修订</span><h2>{entityLabel[history.entity_type]}版本历史</h2></div><button onClick={onClose} aria-label="关闭"><X /></button></header>
      <div className="planning-history-list">
        {history.revisions.map((item) => {
          const title = String(item.snapshot.title || item.snapshot.name || item.snapshot.theme || entityLabel[history.entity_type])
          return <article key={item.id}><span>R{item.revision}</span><div><strong>{title || '未命名'}</strong><p>{String(item.snapshot.summary || item.snapshot.synopsis || item.snapshot.identity || item.snapshot.status || '已保存快照')}</p><small>{item.source} · {formatTime(item.created_at)}</small></div></article>
        })}
      </div>
      <footer><p>历史修订只读，当前编辑不会覆盖这些快照。</p><button className="button secondary" onClick={onClose}>关闭</button></footer>
    </section>
  </div>
}

function EntityEditor({ editor, busy, onChange, onClose, onSubmit }: {
  editor: Editor
  busy: boolean
  onChange: (data: Editor['data']) => void
  onClose: () => void
  onSubmit: (event: FormEvent) => void
}) {
  const set = (key: string, value: string | number) => onChange({ ...editor.data, [key]: value })
  const title = `${editor.mode === 'create' ? '新建' : '编辑'}${entityLabel[editor.kind]}`
  return <div className="modal-backdrop planning-modal-backdrop"><form className="planning-editor" onSubmit={onSubmit}>
    <header><div><span className="eyebrow">人工创作 · 自动保存修订</span><h2>{title}</h2></div><button type="button" onClick={onClose}><X /></button></header>
    {editor.kind === 'proposal' && <>
      <label>提案标题<input required value={String(editor.data.title || '')} onChange={(event) => set('title', event.target.value)} /></label>
      <label>剧情梗概<textarea required rows={5} value={String(editor.data.synopsis || '')} onChange={(event) => set('synopsis', event.target.value)} /></label>
      <label>核心冲突<textarea rows={3} value={String(editor.data.core_conflict || '')} onChange={(event) => set('core_conflict', event.target.value)} /></label>
      <label>结局设计<textarea rows={3} value={String(editor.data.ending || '')} onChange={(event) => set('ending', event.target.value)} /></label>
    </>}
    {editor.kind === 'character' && <>
      <div className="planning-form-grid"><label>角色名<input required value={String(editor.data.name || '')} onChange={(event) => set('name', event.target.value)} /></label><label>状态<select value={String(editor.data.status || 'draft')} onChange={(event) => set('status', event.target.value)}><option value="draft">草稿</option><option value="approved">已批准</option></select></label></div>
      <label>身份<textarea rows={2} value={String(editor.data.identity || '')} onChange={(event) => set('identity', event.target.value)} /></label>
      <div className="planning-form-grid"><label>目标<textarea rows={3} value={String(editor.data.goal || '')} onChange={(event) => set('goal', event.target.value)} /></label><label>阻碍<textarea rows={3} value={String(editor.data.obstacle || '')} onChange={(event) => set('obstacle', event.target.value)} /></label></div>
      <label>性格<textarea rows={2} value={String(editor.data.personality || '')} onChange={(event) => set('personality', event.target.value)} /></label>
      <div className="planning-form-grid"><label>视觉连续性<textarea rows={4} value={String(editor.data.appearance || '')} onChange={(event) => set('appearance', event.target.value)} placeholder="发型、脸部、服装、体态和不能变化的特征" /></label><label>声音连续性<textarea rows={4} value={String(editor.data.voice || '')} onChange={(event) => set('voice', event.target.value)} placeholder="音色、语速、口音、情绪基线" /></label></div>
      <label>人物关系<textarea rows={3} value={String(editor.data.relationships || '')} onChange={(event) => set('relationships', event.target.value)} /></label>
      <label>参考素材说明<textarea rows={2} value={String(editor.data.reference_notes || '')} onChange={(event) => set('reference_notes', event.target.value)} /></label>
    </>}
    {(editor.kind === 'chapter' || editor.kind === 'section') && <>
      <div className="planning-form-grid"><label>{editor.kind === 'chapter' ? '章节标题' : '小节标题'}<input required value={String(editor.data.title || '')} onChange={(event) => set('title', event.target.value)} /></label><label>状态<select value={String(editor.data.status || 'draft')} onChange={(event) => set('status', event.target.value)}><option value="draft">草稿</option><option value="approved">已批准</option></select></label></div>
      <label>摘要<textarea required rows={5} value={String(editor.data.summary || '')} onChange={(event) => set('summary', event.target.value)} /></label>
      <div className="planning-form-grid"><label>节奏目标<input value={String(editor.data.pacing_goal || '')} onChange={(event) => set('pacing_goal', event.target.value)} /></label><label>预计时长（秒）<input type="number" min="0" step="0.1" value={Number(editor.data.planned_seconds || 0)} onChange={(event) => set('planned_seconds', Number(event.target.value))} /></label></div>
    </>}
    <footer><span>保存会生成新的只读修订，来源标记为 human:ui。</span><div><button type="button" className="button secondary" onClick={onClose}>取消</button><button className="button primary" disabled={busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}保存修订</button></div></footer>
  </form></div>
}

export function CreativePlanning({ projectId, onOpenScript, setNotice }: {
  projectId: string
  onOpenScript: () => void
  setNotice: (message: string) => void
}) {
  const [workspace, setWorkspace] = useState<CreativePlanningWorkspace | null>(null)
  const [brief, setBrief] = useState<CreativeBrief | null>(null)
  const [editor, setEditor] = useState<Editor | null>(null)
  const [history, setHistory] = useState<CreativeRevisionHistory | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const data = await planningApi<CreativePlanningWorkspace>('/api/creative-planning')
      setWorkspace(data)
      setBrief(data.brief)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创作规划加载失败')
    }
  }, [])

  useEffect(() => { load() }, [load, projectId])

  const mutate = async (path: string, method: string, body: Record<string, unknown>) => {
    setBusy(true)
    try {
      const data = await planningApi<CreativePlanningWorkspace>(path, { method, body: JSON.stringify(body) })
      setWorkspace(data)
      setBrief(data.brief)
      setError('')
      return data
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '操作失败'
      setError(message)
      setNotice(message)
      return null
    } finally {
      setBusy(false)
    }
  }

  const saveBrief = async (event: FormEvent) => {
    event.preventDefault()
    if (!brief) return
    const result = await mutate('/api/creative-planning/brief', 'PUT', {
      ...brief,
      source: 'human:ui',
      base_revision: brief.revision,
    })
    if (result) setNotice('创作简报已保存为新修订')
  }

  const submitEditor = async (event: FormEvent) => {
    event.preventDefault()
    if (!editor) return
    const data: Record<string, string | number> = { ...editor.data, source: 'human:ui' }
    let path = ''
    let method = editor.mode === 'create' ? 'POST' : 'PATCH'
    if (editor.mode === 'edit') data.base_revision = Number(editor.data.revision)
    if (editor.kind === 'proposal') path = editor.mode === 'create' ? '/api/creative-planning/proposals' : `/api/creative-planning/proposals/${editor.data.id}`
    if (editor.kind === 'character') path = editor.mode === 'create' ? '/api/creative-planning/characters' : `/api/creative-planning/characters/${editor.data.id}`
    if (editor.kind === 'chapter') path = editor.mode === 'create' ? '/api/creative-planning/chapters' : `/api/creative-planning/chapters/${editor.data.id}`
    if (editor.kind === 'section') path = editor.mode === 'create' ? `/api/creative-planning/chapters/${editor.parentId}/sections` : `/api/creative-planning/sections/${editor.data.id}`
    const result = await mutate(path, method, data)
    if (result) {
      setEditor(null)
      setNotice(`${entityLabel[editor.kind]}已保存并创建新修订`)
    }
  }

  const finalize = async (proposal: CreativeProposal) => {
    const result = await mutate(`/api/creative-planning/proposals/${proposal.id}/finalize`, 'POST', { base_revision: proposal.revision, source: 'human:ui-finalize' })
    if (result) setNotice(`“${proposal.title}”已定案；其他提案自动回到草稿`)
  }

  const archive = async (kind: EntityKind, item: EditableEntity) => {
    if (!window.confirm(`归档“${'name' in item ? item.name : item.title}”？历史修订仍会保留。`)) return
    const base = kind === 'section' ? `/api/creative-planning/sections/${item.id}` : `/api/creative-planning/${kind === 'character' ? 'characters' : `${kind}s`}/${item.id}`
    const result = await mutate(`${base}/archive`, 'POST', { base_revision: item.revision, source: 'human:ui-archive' })
    if (result) setNotice(`${entityLabel[kind]}已归档，历史版本仍可追溯`)
  }

  const showHistory = async (kind: EntityKind | 'brief', id: string) => {
    try { setHistory(await planningApi(`/api/creative-planning/history/${kind}/${id}`)) }
    catch (reason) { setNotice(reason instanceof Error ? reason.message : '无法读取版本历史') }
  }

  const reorder = async (kind: 'chapter' | 'section', items: Array<{ id: string }>, index: number, offset: -1 | 1, chapterId?: string) => {
    const target = index + offset
    if (target < 0 || target >= items.length) return
    const ids = items.map((item) => item.id)
    ;[ids[index], ids[target]] = [ids[target], ids[index]]
    const path = kind === 'chapter' ? '/api/creative-planning/chapters/order' : `/api/creative-planning/chapters/${chapterId}/sections/order`
    await mutate(path, 'PUT', { ids, source: 'human:ui-reorder' })
  }

  const splitChapter = async (chapter: CreativeChapter, section: CreativeSection) => {
    const title = window.prompt('新章节标题', `${chapter.title}（续）`)
    if (!title?.trim()) return
    const result = await mutate(`/api/creative-planning/chapters/${chapter.id}/split`, 'POST', { section_id: section.id, new_title: title, source: 'human:ui-split' })
    if (result) setNotice(`已从“${section.title}”起拆为新章节`)
  }

  const splitSection = async (section: CreativeSection) => {
    const title = window.prompt('拆分后新增的小节标题', `${section.title}（续）`)
    if (!title?.trim()) return
    const divider = Math.max(1, Math.floor(section.summary.length / 2))
    const before = window.prompt('保留在当前小节的摘要', section.summary.slice(0, divider))
    if (before === null) return
    const after = window.prompt('写入新小节的摘要', section.summary.slice(divider))
    if (after === null) return
    const result = await mutate(`/api/creative-planning/sections/${section.id}/split`, 'POST', { new_title: title, summary_before: before, summary_after: after, source: 'human:ui-split' })
    if (result) setNotice(`“${section.title}”已拆分为两个可独立维护的小节`)
  }

  const mergeEntity = async (kind: 'chapter' | 'section', source: CreativeChapter | CreativeSection, target: CreativeChapter | CreativeSection) => {
    if (!window.confirm(`把“${source.title}”合并到“${target.title}”？源条目会归档。`)) return
    const result = await mutate(`/api/creative-planning/${kind === 'chapter' ? 'chapters' : 'sections'}/${source.id}/merge`, 'POST', { target_id: target.id, source: 'human:ui-merge' })
    if (result) setNotice(`已合并${entityLabel[kind]}并保留两个条目的修订历史`)
  }

  const plannedSeconds = useMemo(() => workspace?.chapters.reduce((sum, chapter) => sum + chapter.sections.reduce((subtotal, section) => subtotal + section.planned_seconds, 0), 0) || 0, [workspace])

  if (!workspace || !brief) return <main className="planning-loading">{error ? <><CircleDashed size={28} /><strong>创作规划暂时无法加载</strong><p>{error}</p><button className="button secondary" onClick={load}>重试</button></> : <><LoaderCircle className="spin" /><span>正在建立创作规划工作区…</span></>}</main>

  return <main className="creative-planning">
    <section className="planning-hero">
      <div><span className="eyebrow">前期创作 · 版本化内容模型</span><h1>创作规划</h1><p>先把主题、剧情、角色与章节结构定清楚，再进入剧本打磨。这里的每次保存都会留下不可变修订。</p></div>
      <div className="planning-hero-actions"><span className={workspace.summary.ready ? 'ready' : ''}>{workspace.summary.ready ? <Check size={16} /> : <CircleDashed size={16} />}{workspace.summary.ready ? '规划底座已就绪' : '继续完成规划门禁'}</span><button className="button secondary" onClick={onOpenScript}>进入剧本开发<ChevronRight size={16} /></button></div>
    </section>

    <section className="planning-progress">
      {workspace.next_actions.map((item, index) => <article className={item.complete ? 'complete' : ''} key={item.id}><span>{item.complete ? <Check size={14} /> : index + 1}</span><div><strong>{item.label}</strong><small>{item.action}</small></div></article>)}
    </section>

    {error && <button className="planning-error" onClick={() => setError('')}>{error}<X size={14} /></button>}

    <section className="planning-section brief-section">
      <header><div><span>01</span><div><h2>创作简报</h2><p>约束所有后续提案，批准后仍可继续修订。</p></div></div><button className="history-button" onClick={() => showHistory('brief', brief.project_id)}><History size={15} />R{brief.revision} · 历史</button></header>
      <form className="brief-form" onSubmit={saveBrief}>
        <label className="wide">主题与表达<textarea rows={3} value={brief.theme} onChange={(event) => setBrief({ ...brief, theme: event.target.value })} placeholder="这个故事最终想让观众感受到或思考什么？" /></label>
        <label>类型<input value={brief.genre} onChange={(event) => setBrief({ ...brief, genre: event.target.value })} placeholder="例如：都市悬疑" /></label>
        <label>基调<input value={brief.tone} onChange={(event) => setBrief({ ...brief, tone: event.target.value })} placeholder="例如：克制、阴冷、逐步升级" /></label>
        <label>目标受众<input value={brief.audience} onChange={(event) => setBrief({ ...brief, audience: event.target.value })} /></label>
        <label>目标时长（秒）<input type="number" min="0" step="1" value={brief.target_duration} onChange={(event) => setBrief({ ...brief, target_duration: Number(event.target.value) })} /></label>
        <label className="wide">制作与内容约束<textarea rows={3} value={brief.constraints} onChange={(event) => setBrief({ ...brief, constraints: event.target.value })} placeholder="场景数量、角色数量、不能出现的内容、横屏规格等" /></label>
        <div className="brief-actions"><label className="approval-switch"><input type="checkbox" checked={brief.status === 'approved'} onChange={(event) => setBrief({ ...brief, status: event.target.checked ? 'approved' : 'draft' })} /><span>{brief.status === 'approved' ? '简报已批准' : '仍是草稿'}</span></label><button className="button primary" disabled={busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}保存新修订</button></div>
      </form>
    </section>

    <section className="planning-section proposal-section">
      <header><div><span>02</span><div><h2>剧情提案</h2><p>至少保留两个可比较方案，任何时刻只有一个定案。</p></div></div><button className="button secondary" onClick={() => setEditor(emptyEditor('proposal'))}><Plus size={15} />新增提案</button></header>
      <div className="proposal-grid">
        {workspace.proposals.map((proposal) => <article className={proposal.status === 'finalized' ? 'finalized' : ''} key={proposal.id}>
          <div className="proposal-head"><span>方案 {String(proposal.ordinal).padStart(2, '0')}</span><em>{proposal.status === 'finalized' ? '当前定案' : '备选草稿'}</em></div>
          <h3>{proposal.title}</h3><p>{proposal.synopsis || '尚未填写剧情梗概'}</p>
          <dl><div><dt>核心冲突</dt><dd>{proposal.core_conflict || '—'}</dd></div><div><dt>结局</dt><dd>{proposal.ending || '—'}</dd></div></dl>
          <footer><span>R{proposal.revision} · {formatTime(proposal.updated_at)}</span><div><button title="版本历史" onClick={() => showHistory('proposal', proposal.id)}><History size={14} /></button><button title="编辑" onClick={() => setEditor(editEntity('proposal', proposal))}><Pencil size={14} /></button>{proposal.status !== 'finalized' && <button title="归档" onClick={() => archive('proposal', proposal)}><Archive size={14} /></button>}{proposal.status !== 'finalized' && <button className="finalize" onClick={() => finalize(proposal)}><Check size={14} />定案</button>}</div></footer>
        </article>)}
      </div>
    </section>

    <section className="planning-section characters-section">
      <header><div><span>03</span><div><h2>角色管理</h2><p>把关系、外观和声音连续性固化在进入剧本之前。</p></div></div><button className="button secondary" onClick={() => setEditor(emptyEditor('character'))}><Plus size={15} />新增角色</button></header>
      <div className="character-grid">
        {workspace.characters.map((character) => <article key={character.id}><div className="character-avatar"><UserRound size={22} /></div><div className="character-copy"><div><h3>{character.name}</h3><em className={character.status}>{character.status === 'approved' ? '已批准' : '草稿'}</em></div><strong>{character.identity || '身份待补充'}</strong><p>{character.goal ? `目标：${character.goal}` : '目标待补充'}</p><small>{character.relationships || '人物关系待补充'}</small></div><div className="character-actions"><span>R{character.revision}</span><button onClick={() => showHistory('character', character.id)}><History size={14} /></button><button onClick={() => setEditor(editEntity('character', character))}><Pencil size={14} /></button><button onClick={() => archive('character', character)}><Archive size={14} /></button></div></article>)}
        {!workspace.characters.length && <button className="planning-empty-card" onClick={() => setEditor(emptyEditor('character'))}><UsersRound size={24} /><strong>还没有角色卡</strong><span>创建第一个角色并记录视觉、声音和关系连续性。</span></button>}
      </div>
    </section>

    <section className="planning-section structure-section">
      <header><div><span>04</span><div><h2>章节与小节</h2><p>两级结构均可编辑、排序、拆分、合并或归档。</p></div></div><div className="structure-summary"><span><GitBranch size={14} />{workspace.summary.chapter_count} 章 / {workspace.summary.section_count} 节</span><span><Clock3 size={14} />{plannedSeconds.toFixed(1)} 秒</span><button className="button secondary" onClick={() => setEditor(emptyEditor('chapter'))}><Plus size={15} />新增章节</button></div></header>
      <div className="chapter-list">
        {workspace.chapters.map((chapter, chapterIndex) => <article className="chapter-card" key={chapter.id}>
          <header><span className="chapter-index">CH {String(chapterIndex + 1).padStart(2, '0')}</span><div><h3>{chapter.title}</h3><p>{chapter.summary || '章节摘要待补充'}</p><small>{chapter.pacing_goal || '节奏目标待补充'} · {chapter.planned_seconds.toFixed(1)} 秒 · R{chapter.revision}</small></div><div className="chapter-actions"><button disabled={chapterIndex === 0} title="上移章节" onClick={() => reorder('chapter', workspace.chapters, chapterIndex, -1)}><ArrowUp size={14} /></button><button disabled={chapterIndex === workspace.chapters.length - 1} title="下移章节" onClick={() => reorder('chapter', workspace.chapters, chapterIndex, 1)}><ArrowDown size={14} /></button>{chapterIndex > 0 && <button title="并入上一章" onClick={() => mergeEntity('chapter', chapter, workspace.chapters[chapterIndex - 1])}><Merge size={14} /></button>}<button title="历史" onClick={() => showHistory('chapter', chapter.id)}><History size={14} /></button><button title="编辑" onClick={() => setEditor(editEntity('chapter', chapter))}><Pencil size={14} /></button><button title="归档章节" onClick={() => archive('chapter', chapter)}><Archive size={14} /></button></div></header>
          <div className="section-list">
            {chapter.sections.map((section, sectionIndex) => <div className="section-row" key={section.id}><span className="section-number">{chapterIndex + 1}.{sectionIndex + 1}</span><div><strong>{section.title}</strong><p>{section.summary || '小节摘要待补充'}</p><small>{section.pacing_goal || '节奏待补充'} · {section.planned_seconds.toFixed(1)} 秒 · R{section.revision} · {section.status === 'approved' ? '已批准' : '草稿'}</small></div><div className="section-actions"><button disabled={sectionIndex === 0} title="上移小节" onClick={() => reorder('section', chapter.sections, sectionIndex, -1, chapter.id)}><ArrowUp size={13} /></button><button disabled={sectionIndex === chapter.sections.length - 1} title="下移小节" onClick={() => reorder('section', chapter.sections, sectionIndex, 1, chapter.id)}><ArrowDown size={13} /></button>{sectionIndex > 0 && <button title="从这里拆为新章" onClick={() => splitChapter(chapter, section)}><Scissors size={13} /></button>}<button title="拆分小节" onClick={() => splitSection(section)}><GitBranch size={13} /></button>{sectionIndex > 0 && <button title="并入上一小节" onClick={() => mergeEntity('section', section, chapter.sections[sectionIndex - 1])}><Merge size={13} /></button>}<button title="版本历史" onClick={() => showHistory('section', section.id)}><History size={13} /></button><button title="编辑" onClick={() => setEditor(editEntity('section', section))}><Pencil size={13} /></button><button title="归档" onClick={() => archive('section', section)}><Archive size={13} /></button></div></div>)}
            <button className="add-section" onClick={() => setEditor(emptyEditor('section', chapter.id))}><Plus size={14} />新增小节</button>
          </div>
        </article>)}
        {!workspace.chapters.length && <button className="structure-empty" onClick={() => setEditor(emptyEditor('chapter'))}><BookOpenText size={26} /><strong>从定案剧情拆出第一章</strong><span>章节用于组织大的剧情阶段，小节承载后续剧本打磨单元。</span></button>}
      </div>
    </section>

    <section className="planning-handoff"><Sparkles size={20} /><div><strong>规划完成后进入剧本开发</strong><p>本页面只维护人工内容底座，不调用 Agent、不生成分镜，也不会提交 GPU。</p></div><button className="button primary" onClick={onOpenScript}>进入小节剧本打磨<ChevronRight size={16} /></button></section>

    {editor && <EntityEditor editor={editor} busy={busy} onChange={(data) => setEditor({ ...editor, data })} onClose={() => setEditor(null)} onSubmit={submitEditor} />}
    {history && <VersionHistory history={history} onClose={() => setHistory(null)} />}
  </main>
}

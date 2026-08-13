import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Bot,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clapperboard,
  Clock3,
  FileDiff,
  LoaderCircle,
  LockKeyhole,
  Pencil,
  RefreshCw,
  Save,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import type {
  Project,
  ScriptAct,
  ScriptAgentProvider,
  ScriptAgentRun,
  ScriptScene,
  ScriptWorkspace,
  StoryboardSyncFieldDiff,
  StoryboardSyncPreview,
  StoryboardSyncRow,
} from './types'

type AgentScope = 'episode' | 'act' | 'scene'

const scopeLabels: Record<AgentScope, string> = {
  episode: '整集大纲',
  act: '本章',
  scene: '本场',
}

const runStateLabels: Record<ScriptAgentRun['state'], string> = {
  queued: '排队中',
  running: '生成中',
  completed: '草案待审阅',
  failed: '生成失败',
  applied: '已应用',
  rejected: '已拒绝',
}

const scriptApi = async <T,>(path: string, options?: RequestInit): Promise<T> => {
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

const formatSeconds = (seconds: number) => seconds >= 60
  ? `${Math.floor(seconds / 60)}分${Math.round(seconds % 60)}秒`
  : `${Math.round(seconds)}秒`

const providerLabel = (providers: ScriptAgentProvider[], id: string) => providers.find((item) => item.id === id)?.label || id

function Tension({ value }: { value: number }) {
  return <span className="script-tension" aria-label={`张力 ${value}/5`}>{[1, 2, 3, 4, 5].map((level) => <i className={level <= value ? 'filled' : ''} key={level} />)}</span>
}

function ReviewDialog({ run, providers, onClose, onApply, onReject, busy }: {
  run: ScriptAgentRun
  providers: ScriptAgentProvider[]
  onClose: () => void
  onApply: () => void
  onReject: () => void
  busy: boolean
}) {
  const base = run.base_payload as Record<string, any>
  const proposal = run.proposed_payload as Record<string, any>
  const baseScene = run.scope === 'scene' ? base.scene : null
  const nextScene = run.scope === 'scene' ? proposal.scene : null
  const baseAct = run.scope === 'act' ? base.act : null
  const nextAct = run.scope === 'act' ? proposal.act : null
  const fields = [
    ['场景目标', 'goal'],
    ['冲突', 'conflict'],
    ['转折', 'turning_point'],
    ['结尾钩子', 'hook'],
    ['正文', 'content'],
  ] as const
  const baseActs = (base.acts || []) as ScriptAct[]
  const proposedActs = (proposal.acts || []) as Array<Record<string, any>>
  const baseSceneCount = baseActs.reduce((sum, act) => sum + (act.scenes?.length || 0), 0)
  const proposedSceneCount = proposedActs.reduce((sum, act) => sum + (act.scenes?.length || 0), 0)

  return <div className="modal-backdrop script-modal-backdrop">
    <section className="script-review-modal" role="dialog" aria-modal="true" aria-label="Agent 草案差异审阅">
      <header>
        <div><span className="eyebrow">Agent 草案审阅</span><h2>{scopeLabels[run.scope]}修改提案</h2><p>{providerLabel(providers, run.provider)} · {run.instruction}</p></div>
        <button onClick={onClose} aria-label="关闭"><X /></button>
      </header>
      {run.scope === 'scene' && baseScene && nextScene && <div className="script-diff-grid">
        <div className="script-diff-heading"><span>当前版本</span><span>Agent 提案</span></div>
        {fields.map(([label, key]) => <div className="script-diff-row" key={key}>
          <strong>{label}</strong>
          <p>{baseScene[key] || '—'}</p>
          <p className={baseScene[key] !== nextScene[key] ? 'changed' : ''}>{nextScene[key] || '—'}</p>
        </div>)}
      </div>}
      {run.scope === 'act' && baseAct && nextAct && <div className="structure-diff">
        <article><span>当前章节</span><strong>{baseAct.title}</strong><p>{baseAct.summary}</p><small>{baseAct.scenes?.length || 0} 个场景 · {formatSeconds(baseAct.planned_seconds || 0)}</small></article>
        <ChevronRight />
        <article className="proposed"><span>Agent 提案</span><strong>{nextAct.title}</strong><p>{nextAct.summary}</p><small>{nextAct.scenes?.length || 0} 个场景 · {formatSeconds(nextAct.planned_seconds || 0)}</small></article>
      </div>}
      {run.scope === 'episode' && <div className="structure-diff">
        <article><span>当前大纲</span><strong>{base.document?.title}</strong><p>{base.document?.summary}</p><small>{baseActs.length} 章 · {baseSceneCount} 场</small></article>
        <ChevronRight />
        <article className="proposed"><span>Agent 提案</span><strong>{proposal.title}</strong><p>{proposal.summary}</p><small>{proposedActs.length} 章 · {proposedSceneCount} 场</small></article>
      </div>}
      <div className="script-review-warning"><ShieldCheck size={17} /><span><strong>应用后创建新版本</strong>当前大纲和历史版本仍可追溯；下游分镜与已生成视频不会被自动覆盖。</span></div>
      <footer>
        <button className="button secondary" disabled={busy} onClick={onReject}>拒绝提案</button>
        <button className="button primary" disabled={busy} onClick={onApply}>{busy ? <LoaderCircle className="spin" /> : <Check />}接受并创建新版本</button>
      </footer>
    </section>
  </div>
}

function AgentPromptDialog({ scope, provider, providers, initialInstruction, onClose, onSubmit, busy }: {
  scope: AgentScope
  provider: 'codex' | 'kimi'
  providers: ScriptAgentProvider[]
  initialInstruction: string
  onClose: () => void
  onSubmit: (provider: 'codex' | 'kimi', instruction: string) => void
  busy: boolean
}) {
  const [selectedProvider, setSelectedProvider] = useState(provider)
  const [instruction, setInstruction] = useState(initialInstruction)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    onSubmit(selectedProvider, instruction)
  }
  return <div className="modal-backdrop script-modal-backdrop">
    <form className="agent-prompt-modal" onSubmit={submit}>
      <header><div><span className="eyebrow">本地 Agent 服务</span><h2>Agent 生成{scopeLabels[scope]}</h2></div><button type="button" onClick={onClose}><X /></button></header>
      <label>执行器<select value={selectedProvider} onChange={(event) => setSelectedProvider(event.target.value as 'codex' | 'kimi')}>{providers.map((item) => <option value={item.id} disabled={!item.available} key={item.id}>{item.label}{item.available ? ` · ${item.version || '已连接'}` : ' · 不可用'}</option>)}</select></label>
      <label>创作要求<textarea required minLength={2} rows={7} value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="例如：加强第二幕的压力升级，让每一场都产生可视化变化，并保留结尾反转。" /></label>
      <p className="agent-safety-copy"><ShieldCheck size={15} />Agent 结果先进入草案，不直接覆盖当前大纲。</p>
      <footer><button type="button" className="button secondary" onClick={onClose}>取消</button><button className="button primary" disabled={busy || instruction.trim().length < 2}>{busy ? <LoaderCircle className="spin" /> : <Sparkles />}开始生成</button></footer>
    </form>
  </div>
}

const syncActionLabels: Record<StoryboardSyncPreview['rows'][number]['action'], string> = {
  create: '新增',
  update: '更新',
  unchanged: '不变',
  protected: '受保护',
  preserve: '保留',
}

const syncDecisionLabels: Record<StoryboardSyncFieldDiff['decision'], string> = {
  create: '写入新镜头',
  update: '更新',
  keep: '保留原值',
  protected: '跳过',
}

const formatSyncValue = (value: StoryboardSyncFieldDiff['before'], field: StoryboardSyncFieldDiff['field']) => {
  if (value === null || value === '') return '（空）'
  return field === 'seconds' ? `${value} 秒` : String(value)
}

function StoryboardSyncReviewRow({ row, expanded, onToggle }: {
  row: StoryboardSyncRow
  expanded: boolean
  onToggle: () => void
}) {
  const changedFields = row.field_diffs.filter((item) => item.changed).length
  const treatment = row.action === 'unchanged'
    ? '字段一致，可展开复核'
    : row.action === 'preserve'
      ? '不删除原分镜'
      : row.action === 'protected'
        ? `${changedFields} 项建议值将跳过`
        : `${changedFields} 项字段${row.action === 'create' ? '将写入' : '将更新'}`
  return <article className={`sync-review-row sync-row-${row.action}`}>
    <button className="sync-review-summary" onClick={onToggle} aria-expanded={expanded}>
      <span className="sync-scene-identity"><small>{row.act_code || '原分镜'}{row.scene_code ? ` · ${row.scene_code}` : ''}</small><strong>{row.scene_title || '剧本中已移除'}</strong></span>
      <span className="sync-shot-identity"><small>{row.shot_id ? `当前分镜 #${row.current?.ordinal || '—'}` : '当前没有对应分镜'}</small><strong>{row.current?.title || '将创建新分镜'}</strong></span>
      <span className="sync-row-treatment"><span className={`sync-action sync-${row.action}`}>{syncActionLabels[row.action]}</span><small>{treatment}</small></span>
      <ChevronDown className={expanded ? 'expanded' : ''} />
    </button>
    {expanded ? <div className="sync-review-detail">
      {row.action === 'protected' ? <p className="sync-detail-callout protected"><AlertTriangle />检测到{row.protected_reasons.join('、')}，下面的剧本建议值仅供比较，本次不会覆盖现有分镜。</p> : null}
      {row.action === 'preserve' ? <p className="sync-detail-callout preserved"><ShieldCheck />锁定剧本中没有对应场景。该分镜及其视频、提示词会原样保留。</p> : null}
      {row.action === 'unchanged' ? <p className="sync-detail-callout unchanged"><Check />镜头标题、画面描述、对白、时长均与锁定剧本一致。</p> : null}
      <div className="sync-field-diff" role="table" aria-label={`${row.scene_title || row.current?.title || '分镜'}字段差异`}>
        <div className="sync-field-diff-head" role="row"><span>字段</span><span>当前分镜</span><span>锁定剧本值</span><span>本次处理</span></div>
        {row.field_diffs.map((item) => <div className={`sync-field-diff-row decision-${item.decision}`} role="row" key={item.field}>
          <strong>{item.label}</strong>
          <p>{formatSyncValue(item.before, item.field)}</p>
          <p className={item.changed ? 'changed' : ''}>{formatSyncValue(item.after, item.field)}</p>
          <span><em>{syncDecisionLabels[item.decision]}</em><small>{item.note}</small></span>
        </div>)}
      </div>
    </div> : null}
  </article>
}

function StoryboardSyncDialog({ preview, busy, onClose, onApply }: {
  preview: StoryboardSyncPreview
  busy: boolean
  onClose: () => void
  onApply: () => void
}) {
  const historical = preview.review_mode === 'history'
  const firstReviewIndex = preview.rows.findIndex((row) => row.action !== 'unchanged')
  const [expandedRow, setExpandedRow] = useState(firstReviewIndex >= 0 ? firstReviewIndex : 0)
  const impactLabel = [
    preview.summary.create ? `新增 ${preview.summary.create} 个` : '',
    preview.summary.update ? `更新 ${preview.summary.update} 个` : '',
  ].filter(Boolean).join('、')
  return <div className="modal-backdrop script-modal-backdrop">
    <section className="storyboard-sync-modal" role="dialog" aria-modal="true" aria-label="剧本同步到分镜预览">
      <header>
        <div><span className="eyebrow">{historical ? '已执行同步记录' : '待确认同步'} · 剧本 v{preview.script_version} → 分镜</span><h2>逐镜同步差异</h2><p>{historical && preview.applied_at ? `${new Date(preview.applied_at).toLocaleString('zh-CN')} 已执行。` : ''}展开每个镜头，核对执行前分镜、锁定剧本值与字段处理。</p></div>
        <button onClick={onClose} aria-label="关闭"><X /></button>
      </header>
      <div className="sync-summary" aria-label="同步变更统计">
        {(['create', 'update', 'unchanged', 'protected', 'preserve'] as const).map((action) => <article className={`sync-${action}`} key={action}><strong>{preview.summary[action]}</strong><span>{syncActionLabels[action]}</span></article>)}
      </div>
      <p className={`sync-review-intro ${preview.can_apply && !historical ? 'has-changes' : 'is-current'}`}>
        {historical
          ? <>这是一条<strong>已执行记录</strong>：当时{impactLabel ? `${impactLabel}镜头` : '没有可执行变化'}；当前页面不会再次修改分镜。</>
          : preview.can_apply
          ? <>本次将<strong>{impactLabel}</strong>镜头；请重点展开金色条目核对字段。</>
          : <>未发现需要同步的变化：当前 <strong>{preview.summary.unchanged}</strong> 个镜头已经与剧本 v{preview.script_version} 一致。仍可逐镜展开复核。</>}
      </p>
      <div className="sync-review-list">
        {preview.rows.map((row, index) => <StoryboardSyncReviewRow
          row={row}
          expanded={expandedRow === index}
          onToggle={() => setExpandedRow((current) => current === index ? -1 : index)}
          key={`${row.scene_id || 'shot'}-${row.shot_id || index}`}
        />)}
      </div>
      <div className="sync-safety-grid">
        <span><ShieldCheck />已有提示词保留</span><span><ShieldCheck />生产镜头受保护</span><span><ShieldCheck />移除场景不删镜头</span>
      </div>
      {preview.summary.protected > 0 && <p className="sync-protected-note"><AlertTriangle />{preview.summary.protected} 个镜头已有生产产物，本次跳过其剧情字段更新；可在分镜页人工处理。</p>}
      <footer>
        {preview.can_apply && !historical ? <><button className="button secondary" disabled={busy} onClick={onClose}>暂不同步</button><button className="button primary" disabled={busy} onClick={onApply}>{busy ? <LoaderCircle className="spin" /> : <Clapperboard />}确认：{impactLabel}镜头</button></> : <button className="button primary" disabled={busy} onClick={onClose}><Check />已复核，关闭</button>}
      </footer>
    </section>
  </div>
}

export function ScriptStudio({ project, setNotice, onProjectRefresh, onOpenStoryboard }: {
  project: Project
  setNotice: (message: string) => void
  onProjectRefresh: () => Promise<void>
  onOpenStoryboard: () => void
}) {
  const [workspace, setWorkspace] = useState<ScriptWorkspace | null>(null)
  const [selectedSceneId, setSelectedSceneId] = useState('')
  const [provider, setProvider] = useState<'codex' | 'kimi'>('codex')
  const [promptTarget, setPromptTarget] = useState<{ scope: AgentScope; targetId?: string; instruction?: string } | null>(null)
  const [reviewRun, setReviewRun] = useState<ScriptAgentRun | null>(null)
  const [editing, setEditing] = useState(false)
  const [sceneDraft, setSceneDraft] = useState<Partial<ScriptScene>>({})
  const [syncPreview, setSyncPreview] = useState<StoryboardSyncPreview | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    const data = await scriptApi<ScriptWorkspace>('/api/script')
    setWorkspace(data)
    const available = data.providers.find((item) => item.available)
    if (available && !data.providers.find((item) => item.id === provider && item.available)) setProvider(available.id)
    setSelectedSceneId((current) => data.acts.some((act) => act.scenes.some((scene) => scene.id === current)) ? current : data.acts.flatMap((act) => act.scenes)[0]?.id || '')
    return data
  }, [provider])

  useEffect(() => {
    setWorkspace(null)
    refresh().catch((error) => setNotice(error instanceof Error ? error.message : '剧本加载失败'))
  }, [project.id, refresh, setNotice])

  const activeRun = workspace?.agent_runs.find((run) => run.state === 'queued' || run.state === 'running')
  useEffect(() => {
    if (!activeRun) return
    const timer = window.setInterval(() => refresh().catch(() => undefined), 1800)
    return () => window.clearInterval(timer)
  }, [activeRun, refresh])

  const selectedAct = useMemo(() => workspace?.acts.find((act) => act.scenes.some((scene) => scene.id === selectedSceneId)) || workspace?.acts[0], [workspace, selectedSceneId])
  const selectedScene = useMemo(() => selectedAct?.scenes.find((scene) => scene.id === selectedSceneId) || selectedAct?.scenes[0], [selectedAct, selectedSceneId])
  const relevantRun = workspace?.agent_runs.find((run) => run.target_id === selectedScene?.id)
    || workspace?.agent_runs.find((run) => run.target_id === selectedAct?.id)
    || workspace?.agent_runs.find((run) => run.scope === 'episode')
  const connected = Boolean(workspace?.providers.some((item) => item.available))

  const startEditing = () => {
    if (!selectedScene) return
    setSceneDraft({ ...selectedScene })
    setEditing(true)
  }

  const saveScene = async () => {
    if (!selectedScene) return
    setBusy(true)
    try {
      const data = await scriptApi<ScriptWorkspace>(`/api/script/sections/${selectedScene.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          title: sceneDraft.title,
          summary: sceneDraft.summary,
          goal: sceneDraft.goal,
          conflict: sceneDraft.conflict,
          turning_point: sceneDraft.turning_point,
          hook: sceneDraft.hook,
          content: sceneDraft.content,
          planned_seconds: sceneDraft.planned_seconds,
          tension: sceneDraft.tension,
        }),
      })
      setWorkspace(data)
      setEditing(false)
      setNotice('场景内容已保存为当前草稿')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '场景保存失败')
    } finally {
      setBusy(false)
    }
  }

  const submitAgent = async (selectedProvider: 'codex' | 'kimi', instruction: string) => {
    if (!promptTarget) return
    setBusy(true)
    try {
      await scriptApi<ScriptAgentRun>('/api/script/agent-runs', {
        method: 'POST',
        body: JSON.stringify({ scope: promptTarget.scope, target_id: promptTarget.targetId, provider: selectedProvider, instruction }),
      })
      setProvider(selectedProvider)
      setPromptTarget(null)
      await refresh()
      setNotice(`${providerLabel(workspace?.providers || [], selectedProvider)} 已开始生成${scopeLabels[promptTarget.scope]}草案`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Agent 任务提交失败')
    } finally {
      setBusy(false)
    }
  }

  const mutateRun = async (run: ScriptAgentRun, action: 'apply' | 'reject') => {
    setBusy(true)
    try {
      const data = await scriptApi<ScriptWorkspace>(`/api/script/agent-runs/${run.id}/${action}`, { method: 'POST' })
      setWorkspace(data)
      setReviewRun(null)
      setNotice(action === 'apply' ? 'Agent 提案已应用，并创建了新的剧本版本' : 'Agent 提案已拒绝，当前剧本未改变')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '提案处理失败')
    } finally {
      setBusy(false)
    }
  }

  const lockScript = async () => {
    setBusy(true)
    try {
      const data = await scriptApi<ScriptWorkspace>('/api/script/lock', { method: 'POST' })
      setWorkspace(data)
      setNotice(`大纲 v${data.document.version} 已锁定；下一步可以编译为候选分镜`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '大纲锁定失败')
    } finally {
      setBusy(false)
    }
  }

  const previewStoryboardSync = async () => {
    setBusy(true)
    try {
      const data = await scriptApi<StoryboardSyncPreview>('/api/script/storyboard-sync/preview', { method: 'POST' })
      setSyncPreview(data)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '分镜同步预览失败')
    } finally {
      setBusy(false)
    }
  }

  const reviewLatestStoryboardSync = () => {
    const sync = workspace?.latest_storyboard_sync
    if (!sync) return
    setSyncPreview({
      document_id: sync.document_id,
      project_id: sync.project_id,
      script_version: sync.script_version,
      plan_hash: sync.plan_hash,
      summary: sync.summary,
      rows: sync.plan,
      can_apply: false,
      safety: {
        existing_prompts_preserved: true,
        production_shots_protected: true,
        unmatched_shots_preserved: true,
      },
      review_mode: 'history',
      applied_at: sync.applied_at,
    })
  }

  const applyStoryboardSync = async () => {
    if (!syncPreview) return
    setBusy(true)
    try {
      const data = await scriptApi<{ workspace: ScriptWorkspace; sync: { state: 'applied' | 'partial'; summary: StoryboardSyncPreview['summary'] } }>('/api/script/storyboard-sync/apply', {
        method: 'POST',
        body: JSON.stringify({ script_version: syncPreview.script_version, plan_hash: syncPreview.plan_hash }),
      })
      setWorkspace(data.workspace)
      setSyncPreview(null)
      await onProjectRefresh()
      const changed = data.sync.summary.create + data.sync.summary.update
      setNotice(`剧本 v${syncPreview.script_version} 已同步到分镜，共处理 ${changed} 个镜头${data.sync.state === 'partial' ? '；受保护镜头已跳过' : ''}`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '分镜同步失败')
    } finally {
      setBusy(false)
    }
  }

  if (!workspace || !selectedAct || !selectedScene) return <main className="script-loading"><LoaderCircle className="spin" />正在建立剧本工作区…</main>

  const scriptApproved = workspace.document.status === 'approved'
  const latestSyncCurrent = workspace.latest_storyboard_sync?.script_version === workspace.document.version

  return <>
    <main className="script-studio">
      <section className="script-main">
        <header className="script-heading">
          <div><span className="eyebrow">{project.episode} · 剧本结构</span><h1>剧本开发</h1><p><strong>{workspace.document.title}</strong><span>大纲 v{workspace.document.version}</span><span>{workspace.document.status === 'approved' ? '已定稿' : '草稿'}</span><small>自动保存于 {new Date(workspace.document.updated_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</small></p></div>
          <div className="script-heading-actions">
            <div className="agent-global-action"><button disabled={!connected || Boolean(activeRun)} onClick={() => setPromptTarget({ scope: 'episode' })}><Sparkles size={16} />Agent 生成大纲</button><select aria-label="选择大纲 Agent" value={provider} onChange={(event) => setProvider(event.target.value as 'codex' | 'kimi')}>{workspace.providers.map((item) => <option value={item.id} disabled={!item.available} key={item.id}>{item.label}</option>)}</select></div>
            <button className="button primary lock-outline" disabled={busy || Boolean(activeRun) || scriptApproved} onClick={lockScript}><LockKeyhole size={16} />{scriptApproved ? '大纲已锁定' : '锁定大纲'}</button>
            <button className="button secondary storyboard-sync-trigger" title={scriptApproved ? latestSyncCurrent ? '查看最近一次已执行同步的逐镜字段差异' : '预览锁定剧本与当前分镜的差异' : '请先锁定大纲'} disabled={busy || Boolean(activeRun) || !scriptApproved} onClick={latestSyncCurrent ? reviewLatestStoryboardSync : previewStoryboardSync}><Clapperboard size={16} />{latestSyncCurrent ? '查看上次同步差异' : '同步到分镜'}</button>
            <small className={connected ? 'agent-connected' : 'agent-offline'}><i />本地 Agent 服务 · {connected ? '已连接' : '未连接'}</small>
            <small className="agent-boundary"><ShieldCheck size={13} />Agent 结果先进入草案，不直接覆盖当前大纲</small>
          </div>
        </header>

        <div className="script-breadcrumb"><span>{project.episode}</span><ChevronRight /><span>{selectedAct.code}</span><ChevronRight /><strong>{selectedScene.code} {selectedScene.title}</strong><span className="script-total"><Clock3 />预计总长 {formatSeconds(workspace.document.total_seconds)}</span></div>

        <section className="act-grid" style={{ gridTemplateColumns: `repeat(${Math.max(1, workspace.acts.length)}, minmax(0, 1fr))` }}>
          {workspace.acts.map((act) => <article className="act-column" key={act.id}>
            <header><div><strong>{act.code} <span>{act.title}</span></strong><small>预计时长 {formatSeconds(act.planned_seconds)}</small></div><button title="Agent 生成或重写本章" disabled={Boolean(activeRun)} onClick={() => setPromptTarget({ scope: 'act', targetId: act.id })}><Sparkles size={15} /></button></header>
            <div className="act-table-head"><span>场次</span><span>场景 / 目标</span><span>时长</span><span>张力</span></div>
            <div className="act-scenes">
              {act.scenes.map((scene) => <button className={scene.id === selectedScene.id ? 'selected' : ''} onClick={() => { setSelectedSceneId(scene.id); setEditing(false) }} key={scene.id}>
                <span>{scene.code}</span><span><strong>{scene.title}</strong><small>{scene.summary}</small></span><span>{formatSeconds(scene.planned_seconds)}</span><Tension value={scene.tension} />
              </button>)}
              {!act.scenes.length && <div className="act-empty"><span>本章还没有场景</span><button onClick={() => setPromptTarget({ scope: 'act', targetId: act.id })}><Sparkles />让 Agent 生成</button></div>}
            </div>
          </article>)}
        </section>
        <footer className="script-footer"><span>剧本字数（大纲） <strong>{workspace.acts.flatMap((act) => act.scenes).reduce((sum, scene) => sum + scene.content.length, 0).toLocaleString('zh-CN')} 字</strong></span><span><RefreshCw />{workspace.versions.length} 个可追溯版本</span>{latestSyncCurrent ? <button onClick={onOpenStoryboard}><Check />v{workspace.document.version} 已同步 · 进入分镜</button> : <span>锁定后预览并同步到“分镜与生成”</span>}</footer>
      </section>

      <aside className="script-inspector">
        <header><div><strong>{selectedScene.code} · {selectedScene.title}</strong><small>{selectedAct.code} / {selectedAct.title}</small></div>{editing ? <button onClick={() => setEditing(false)}><X /></button> : <button onClick={startEditing}><Pencil /></button>}</header>
        {editing ? <div className="scene-edit-form">
          <label>场景标题<input value={sceneDraft.title || ''} onChange={(event) => setSceneDraft({ ...sceneDraft, title: event.target.value })} /></label>
          <label>一句话剧情<textarea rows={2} value={sceneDraft.summary || ''} onChange={(event) => setSceneDraft({ ...sceneDraft, summary: event.target.value })} /></label>
          <label>场景目标<textarea rows={2} value={sceneDraft.goal || ''} onChange={(event) => setSceneDraft({ ...sceneDraft, goal: event.target.value })} /></label>
          <label>冲突<textarea rows={2} value={sceneDraft.conflict || ''} onChange={(event) => setSceneDraft({ ...sceneDraft, conflict: event.target.value })} /></label>
          <label>转折<textarea rows={2} value={sceneDraft.turning_point || ''} onChange={(event) => setSceneDraft({ ...sceneDraft, turning_point: event.target.value })} /></label>
          <label>结尾钩子<textarea rows={2} value={sceneDraft.hook || ''} onChange={(event) => setSceneDraft({ ...sceneDraft, hook: event.target.value })} /></label>
          <div className="scene-edit-numbers"><label>预计时长<input type="number" min="1" max="600" value={sceneDraft.planned_seconds || 0} onChange={(event) => setSceneDraft({ ...sceneDraft, planned_seconds: Number(event.target.value) })} /></label><label>张力<select value={sceneDraft.tension || 1} onChange={(event) => setSceneDraft({ ...sceneDraft, tension: Number(event.target.value) })}>{[1, 2, 3, 4, 5].map((value) => <option value={value} key={value}>{value} / 5</option>)}</select></label></div>
          <label>动作与对白<textarea rows={6} value={sceneDraft.content || ''} onChange={(event) => setSceneDraft({ ...sceneDraft, content: event.target.value })} /></label>
          <button className="button primary" disabled={busy} onClick={saveScene}><Save size={16} />保存本场</button>
        </div> : <div className="scene-detail">
          <section><h3>场景目标</h3><p>{selectedScene.goal || '尚未填写'}</p></section>
          <section><h3>冲突</h3><p>{selectedScene.conflict || '尚未填写'}</p></section>
          <section><h3>转折</h3><p>{selectedScene.turning_point || '尚未填写'}</p></section>
          <section><h3>结尾钩子</h3><p>{selectedScene.hook || '尚未填写'}</p></section>
          <div className="scene-detail-meta"><span>预计时长 <strong>{formatSeconds(selectedScene.planned_seconds)}</strong></span><span>张力 <Tension value={selectedScene.tension} /></span></div>
        </div>}
        <div className="scene-agent-action"><button disabled={Boolean(activeRun)} onClick={() => setPromptTarget({ scope: 'scene', targetId: selectedScene.id })}><Sparkles />Agent 生成本场</button><select aria-label="选择场景 Agent" value={provider} onChange={(event) => setProvider(event.target.value as 'codex' | 'kimi')}>{workspace.providers.map((item) => <option value={item.id} disabled={!item.available} key={item.id}>{item.label}</option>)}</select></div>
        <p className="agent-boundary inspector-boundary"><ShieldCheck />Agent 结果先进入草案，不直接覆盖当前内容</p>

        <section className="agent-task-panel">
          <header><div><Bot /><strong>Agent 任务</strong></div>{relevantRun && <span className={`agent-state state-${relevantRun.state}`}>{runStateLabels[relevantRun.state]}</span>}</header>
          {relevantRun ? <>
            <div className="agent-task-meta"><span>{providerLabel(workspace.providers, relevantRun.provider)}</span><small>{scopeLabels[relevantRun.scope]}</small></div>
            <h4>用户指令</h4><p>{relevantRun.instruction}</p>
            <h4>执行状态</h4><p>{relevantRun.message}</p>
            {relevantRun.error && <p className="agent-error"><AlertTriangle />{relevantRun.error}</p>}
            <div className="agent-task-actions">
              {relevantRun.state === 'completed' && <button onClick={() => setReviewRun(relevantRun)}><FileDiff />查看差异</button>}
              {(relevantRun.state === 'completed' || relevantRun.state === 'failed' || relevantRun.state === 'rejected') && <button onClick={() => setPromptTarget({ scope: relevantRun.scope, targetId: relevantRun.target_id, instruction: relevantRun.instruction })}><RefreshCw />重新生成</button>}
              {(relevantRun.state === 'queued' || relevantRun.state === 'running') && <span><LoaderCircle className="spin" />请等待本地 Agent 返回</span>}
            </div>
          </> : <div className="agent-task-empty"><Bot /><strong>尚无 Agent 草案</strong><p>可以从整集、章节或当前场景发起生成。</p></div>}
        </section>
      </aside>
    </main>

    {promptTarget && <AgentPromptDialog scope={promptTarget.scope} provider={provider} providers={workspace.providers} initialInstruction={promptTarget.instruction || ''} onClose={() => setPromptTarget(null)} onSubmit={submitAgent} busy={busy} />}
    {reviewRun && <ReviewDialog run={reviewRun} providers={workspace.providers} onClose={() => setReviewRun(null)} onApply={() => mutateRun(reviewRun, 'apply')} onReject={() => mutateRun(reviewRun, 'reject')} busy={busy} />}
    {syncPreview && <StoryboardSyncDialog preview={syncPreview} busy={busy} onClose={() => setSyncPreview(null)} onApply={applyStoryboardSync} />}
  </>
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, ChevronRight, Clock3, LoaderCircle, RefreshCw, RotateCcw, ShieldCheck, Sparkles } from 'lucide-react'

type StrategyType = 'ref2va_regenerate' | 'original_model_regenerate' | 'deterministic_scale'
type Strategy = {
  type: StrategyType
  label: string
  operation: string
  kind: 'model_regeneration' | 'pixel_scaling'
  gpu_required: boolean
  drift_review_required: boolean
}
type HDPlan = {
  id: string
  revision: number
  strategy_type: StrategyType
  strategy: Strategy
  target_width: number
  target_height: number
  plan_hash: string
  estimated_operation: string
}
type HDValidation = {
  id: string
  plan_id: string
  validation_hash: string
  adapter: string
  model_id: string
  workflow_id: string
  gpu_submitted: boolean
  created_at: string
}
type HDJob = {
  id: string
  attempt: number
  state: string
  revision: number
  message: string
  error?: string
  retry_safe: boolean
  gpu_submitted: boolean
  created_at: string
}
type HDArtifact = {
  id: string
  version: number
  strategy_type: StrategyType
  strategy_kind: 'model_regeneration' | 'pixel_scaling'
  strategy: Strategy
  width: number
  height: number
  duration_seconds: number
  has_audio: boolean
  prompt: string
  seed?: number
  plan_hash: string
  model_id: string
  workflow_id: string
  output_sha256: string
  source_sha256: string
  video_url: string
  created_at: string
}
type HDReview = {
  id: string
  artifact_id: string
  revision: number
  decision: 'pass' | 'needs_changes' | 'reject'
  scores: Record<string, number>
  drift_confirmed: boolean
  note: string
  watched_seconds: number
  media_probe: Record<string, unknown>
  created_at: string
}
type HDMasterVersion = {
  id: string
  revision: number
  artifact_id: string
  action: 'select' | 'rollback'
  rollback_of_revision?: number
  review_revision: number
  note: string
  current: boolean
  created_at: string
}
type HDShotWorkspace = {
  shot: { id: string; title: string; ordinal: number }
  locked_draft_master?: {
    candidate_id: string
    master_version_id: string
    master_revision: number
    prompt: string
    seed?: number
    original_mode: 'fl2va' | 'ref2va'
    media: { path: string; checksum_sha256: string }
  }
  master_issue?: string
  strategies: Strategy[]
  plans: HDPlan[]
  validations: HDValidation[]
  jobs: HDJob[]
  artifacts: HDArtifact[]
  reviews: HDReview[]
  master_versions: HDMasterVersion[]
  current_plan?: HDPlan
  current_validation?: HDValidation
  current_hd_master?: HDMasterVersion
  summary: { artifact_count: number; passed_count: number; active_job_count: number; ready_for_assembly: boolean }
}
type HDWorkspace = {
  project: { id: string; title: string; episode: string }
  shots: HDShotWorkspace[]
  summary: { shot_count: number; locked_master_count: number; hd_selected_count: number; active_job_count: number }
}

const scoreFields = [
  ['story_match', '剧情匹配'],
  ['identity_continuity', '人物连续性'],
  ['temporal_stability', '时序稳定'],
  ['visual_detail', '视觉细节'],
  ['audio_quality', '声音质量'],
] as const

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!response.ok) {
    const body = await response.text()
    try {
      const detail = JSON.parse(body).detail
      throw new Error(typeof detail === 'string' ? detail : detail?.message || body)
    } catch (error) {
      if (error instanceof Error && error.message !== 'Unexpected end of JSON input') throw error
      throw new Error(body || `请求失败：${response.status}`)
    }
  }
  return response.json()
}

function time(value?: string) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}

function jobLabel(state: string) {
  return ({ queued: '排队中', running: '处理中', completed: '已完成', failed: '失败', submission_outcome_unknown: '待人工对账' } as Record<string, string>)[state] || state
}

export function HDWorkbench({ projectId, setNotice, onOpenTimeline }: {
  projectId: string
  setNotice: (message: string) => void
  onOpenTimeline: () => void
}) {
  const [workspace, setWorkspace] = useState<HDWorkspace | null>(null)
  const [selectedShotId, setSelectedShotId] = useState('')
  const [selectedArtifactId, setSelectedArtifactId] = useState('')
  const [strategy, setStrategy] = useState<StrategyType>('deterministic_scale')
  const [busy, setBusy] = useState('')
  const [decision, setDecision] = useState<'pass' | 'needs_changes' | 'reject'>('needs_changes')
  const [scores, setScores] = useState<Record<string, number>>({ story_match: 3, identity_continuity: 3, temporal_stability: 3, visual_detail: 3, audio_quality: 3 })
  const [driftConfirmed, setDriftConfirmed] = useState(false)
  const [note, setNote] = useState('')
  const [watched, setWatched] = useState(0)
  const videoRef = useRef<HTMLVideoElement | null>(null)

  const load = useCallback(async (quiet = false) => {
    try {
      const result = await request<HDWorkspace>('/api/hd/workspace')
      setWorkspace(result)
      setSelectedShotId((current) => result.shots.some(item => item.shot.id === current) ? current : result.shots[0]?.shot.id || '')
    } catch (error) {
      if (!quiet) setNotice(error instanceof Error ? error.message : '高清工作台加载失败')
    }
  }, [setNotice])

  useEffect(() => { void load() }, [load, projectId])
  useEffect(() => {
    if (!workspace?.summary.active_job_count) return
    const timer = window.setInterval(() => void load(true), 1500)
    return () => window.clearInterval(timer)
  }, [load, workspace?.summary.active_job_count])

  const selected = workspace?.shots.find(item => item.shot.id === selectedShotId) || workspace?.shots[0]
  const selectedArtifact = selected?.artifacts.find(item => item.id === selectedArtifactId) || selected?.artifacts[0]
  const selectedReview = selected?.reviews.find(item => item.artifact_id === selectedArtifact?.id)
  const currentArtifact = selected?.artifacts.find(item => item.id === selected.current_hd_master?.artifact_id)
  const lowMasterUrl = selected?.locked_draft_master ? `/api/candidates/${selected.locked_draft_master.candidate_id}/video` : ''
  const currentStrategy = selected?.strategies.find(item => item.type === strategy)
  const validForCurrentPlan = selected?.current_validation?.plan_id === selected?.current_plan?.id
  const unresolved = selected?.jobs.find(item => item.state === 'submission_outcome_unknown')
  const canSubmit = Boolean(validForCurrentPlan && selected?.current_validation && !selected?.summary.active_job_count)

  useEffect(() => {
    setSelectedArtifactId(selected?.artifacts[0]?.id || '')
  }, [selected?.shot.id, selected?.artifacts[0]?.id])
  useEffect(() => {
    setDecision(selectedReview?.decision || 'needs_changes')
    setScores(selectedReview?.scores || { story_match: 3, identity_continuity: 3, temporal_stability: 3, visual_detail: 3, audio_quality: 3 })
    setDriftConfirmed(Boolean(selectedReview?.drift_confirmed))
    setNote(selectedReview?.note || '')
    setWatched(selectedReview?.watched_seconds || 0)
  }, [selectedReview?.id, selectedArtifact?.id])

  const act = async (label: string, action: () => Promise<unknown>, success: string) => {
    setBusy(label)
    try {
      await action()
      setNotice(success)
      await load()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : `${label}失败`)
    } finally {
      setBusy('')
    }
  }

  const createPlan = () => selected && act('plan', () => request(`/api/hd/shots/${selected.shot.id}/plans`, {
    method: 'POST', body: JSON.stringify({ strategy_type: strategy, target_width: 1344, target_height: 768 }),
  }), `已创建 ${currentStrategy?.label || '高清'} 策略版本`)

  const validate = () => selected?.current_plan && act('validate', () => request('/api/hd/validations', {
    method: 'POST', body: JSON.stringify({ plan_id: selected.current_plan?.id, expected_plan_hash: selected.current_plan?.plan_hash }),
  }), '高清 dry-run 通过：未提交 GPU')

  const submit = async () => {
    if (!selected?.current_validation) return
    const gpu = selected.current_plan?.strategy.gpu_required
    if (!window.confirm(gpu ? '确认提交本机 GPU 高清任务？模型重生成可能产生人物、动作或声音漂移。' : '确认执行确定性 FFmpeg 放大？该操作不生成新细节，也不占用 GPU。')) return
    await act('submit', () => request(`/api/hd/shots/${selected.shot.id}/jobs`, {
      method: 'POST', body: JSON.stringify({
        validation_id: selected.current_validation?.id,
        expected_validation_hash: selected.current_validation?.validation_hash,
        idempotency_key: crypto.randomUUID(), confirm: true,
      }),
    }), gpu ? '高清任务已提交；关闭页面后仍会继续跟踪' : '确定性放大已进入本地任务队列')
  }

  const review = () => selected && selectedArtifact && act('review', () => request(`/api/hd/shots/${selected.shot.id}/reviews`, {
    method: 'POST', body: JSON.stringify({
      artifact_id: selectedArtifact.id, decision, scores, drift_confirmed: driftConfirmed,
      note, watched_seconds: Math.max(watched, videoRef.current?.currentTime || 0),
    }),
  }), decision === 'pass' ? '高清审片已通过，可以定稿' : '高清审片结论已追加保存')

  const selectArtifact = () => selected && selectedArtifact && act('select', () => request(`/api/hd/shots/${selected.shot.id}/select`, {
    method: 'POST', body: JSON.stringify({
      artifact_id: selectedArtifact.id, base_revision: selected.master_versions[0]?.revision || 0, note,
    }),
  }), '高清版本已追加为当前定稿')

  const rollback = (version: HDMasterVersion) => selected && act('rollback', () => request(`/api/hd/shots/${selected.shot.id}/rollback`, {
    method: 'POST', body: JSON.stringify({
      target_revision: version.revision, base_revision: selected.master_versions[0]?.revision || 0,
      note: `人工回滚到高清 R${version.revision}`, confirm: true,
    }),
  }), `已追加回滚修订，恢复高清 R${version.revision}`)

  const resolve = () => {
    if (!unresolved || !window.confirm(`只在已经核对适配器和 ComfyUI，确认任务 ${unresolved.id} 没有发生外部提交时继续。是否记录“零提交”证据并解除门禁？`)) return
    void act('resolve', () => request(`/api/hd/jobs/${unresolved.id}/resolve`, {
      method: 'POST', body: JSON.stringify({
        expected_revision: unresolved.revision, note: '人工检查 ComfyUI / 适配器记录，确认没有外部提交', confirm_no_external_submission: true,
      }),
    }), '已记录零提交对账证据，可以安全重试')
  }

  if (!workspace) return <main className="page hd-page"><div className="loading"><LoaderCircle className="spin" /> 正在读取高清证据链…</div></main>

  return <main className="page hd-page">
    <div className="page-heading hd-heading">
      <div><span className="eyebrow">逐镜高清与交付</span><h1>母版之后，装配之前</h1><p>只处理已锁定草稿母版。模型重生成与确定性放大使用不同类型、门禁、证据和审片语义。</p></div>
      <div className="hd-summary"><span><strong>{workspace.summary.locked_master_count}</strong> / {workspace.summary.shot_count}<small>草稿母版</small></span><span><strong>{workspace.summary.hd_selected_count}</strong> / {workspace.summary.shot_count}<small>高清定稿</small></span><button className="button secondary" onClick={() => void load()}><RefreshCw size={15} />刷新</button></div>
    </div>

    <section className="hd-workspace">
      <nav className="hd-shot-list" aria-label="高清镜头列表">
        {workspace.shots.map(item => <button key={item.shot.id} className={item.shot.id === selected?.shot.id ? 'active' : ''} onClick={() => setSelectedShotId(item.shot.id)}>
          <span>{String(item.shot.ordinal).padStart(2, '0')}</span><div><strong>{item.shot.title}</strong><small>{item.current_hd_master ? `高清 R${item.current_hd_master.revision} 已定稿` : item.locked_draft_master ? item.current_plan ? `${item.current_plan.strategy.label} · 待完成` : '可选择高清策略' : '缺少锁定草稿母版'}</small></div>{item.current_hd_master ? <Check size={16} /> : <ChevronRight size={16} />}
        </button>)}
      </nav>

      {selected && <div className="hd-detail">
        {!selected.locked_draft_master ? <section className="hd-blocked"><AlertTriangle /><div><strong>这个镜头不能进入高清处理</strong><p>{selected.master_issue}</p><small>先回到审片台，用至少两条可信低清候选完成结构化审片并锁定草稿母版。</small></div></section> : <>
          <section className="hd-source-card">
            <div><span className="eyebrow">锁定草稿母版</span><h2>{selected.shot.title}</h2><p>{selected.locked_draft_master.original_mode.toUpperCase()} · Seed {selected.locked_draft_master.seed ?? '—'} · 母版 R{selected.locked_draft_master.master_revision}</p></div>
            <code title={selected.locked_draft_master.media.checksum_sha256}>{selected.locked_draft_master.media.checksum_sha256.slice(0, 12)}</code>
          </section>

          <section className="hd-strategy-panel">
            <header><div><span className="eyebrow">01 · 选择路线</span><h2>目标 1344×768 横屏</h2></div>{selected.current_plan && <span>策略 R{selected.current_plan.revision} · {selected.current_plan.plan_hash.slice(0, 12)}</span>}</header>
            <div className="hd-strategy-grid">{selected.strategies.map(item => <button key={item.type} className={strategy === item.type ? 'active' : ''} onClick={() => setStrategy(item.type)}>
              <span>{item.kind === 'pixel_scaling' ? 'SCALE' : item.type === 'ref2va_regenerate' ? 'REF2VA' : 'ORIGINAL'}</span><strong>{item.label}</strong><p>{item.operation}</p><small>{item.gpu_required ? '需要 GPU · 必须确认漂移' : '不使用 GPU · 不产生新细节'}</small>
            </button>)}</div>
            <div className="hd-step-actions"><button className="button secondary" disabled={Boolean(busy)} onClick={createPlan}>{busy === 'plan' ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}保存新策略版本</button><button className="button secondary" disabled={!selected.current_plan || Boolean(busy)} onClick={validate}>{busy === 'validate' ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />}H3 / FFmpeg dry-run</button><button className="button primary" disabled={!canSubmit || Boolean(busy)} onClick={() => void submit()}>{busy === 'submit' ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}{selected.current_plan?.strategy.gpu_required ? '明确确认 GPU 提交' : '确认执行保真放大'}</button></div>
            {selected.current_validation && <div className="hd-validation-proof"><ShieldCheck size={16} /><span><strong>dry-run 已冻结，GPU submitted = false</strong><small>{selected.current_validation.adapter} · {selected.current_validation.model_id} · {selected.current_validation.workflow_id}</small></span><code>{selected.current_validation.validation_hash.slice(0, 12)}</code></div>}
          </section>

          {selected.jobs.length > 0 && <section className="hd-jobs"><header><span className="eyebrow">02 · 高清任务</span><small>重启可恢复 · 未知提交默认失败关闭</small></header>{selected.jobs.map(job => <article key={job.id} className={`hd-job ${job.state}`}><Clock3 size={15} /><div><strong>Attempt {job.attempt} · {jobLabel(job.state)}</strong><small>{job.message} · {time(job.created_at)}</small>{job.error && <em>{job.error}</em>}</div><code>{job.id.slice(-8)}</code></article>)}{unresolved && <button className="button secondary" onClick={resolve}>确认未发生外部提交并解除门禁</button>}</section>}

          {selected.artifacts.length > 0 && <section className="hd-review-panel">
            <header><div><span className="eyebrow">03 · 对比审片</span><h2>低清母版 vs 高清版本</h2></div><small>每次结论和定稿都是 append-only 修订</small></header>
            <div className="hd-comparison"><figure><video controls preload="metadata" src={lowMasterUrl} /><figcaption><strong>低清锁定母版</strong><span>内容连续性基准 · {selected.locked_draft_master.original_mode.toUpperCase()}</span></figcaption></figure>{selectedArtifact && <figure><video ref={videoRef} controls preload="metadata" src={selectedArtifact.video_url} onTimeUpdate={(event) => setWatched(value => Math.max(value, event.currentTarget.currentTime))} /><figcaption><strong>高清 V{selectedArtifact.version} · {selectedArtifact.strategy.label}</strong><span>{selectedArtifact.width}×{selectedArtifact.height} · {selectedArtifact.duration_seconds.toFixed(3)} 秒 · {selectedArtifact.strategy_kind === 'pixel_scaling' ? '像素缩放' : '模型重生成'}</span></figcaption></figure>}</div>
            <div className="hd-artifact-strip">{selected.artifacts.map(artifact => <button key={artifact.id} className={artifact.id === selectedArtifact?.id ? 'active' : ''} onClick={() => setSelectedArtifactId(artifact.id)}><span>V{artifact.version}</span><strong>{artifact.strategy.label}</strong><small>{artifact.model_id} · {artifact.output_sha256.slice(0, 10)}</small>{currentArtifact?.id === artifact.id && <em>当前定稿</em>}</button>)}</div>
            {selectedArtifact && <div className="hd-review-form"><div className="hd-score-grid">{scoreFields.map(([key, label]) => <label key={key}><span>{label}</span><select value={scores[key]} onChange={event => setScores({ ...scores, [key]: Number(event.target.value) })}>{[1, 2, 3, 4, 5].map(value => <option key={value} value={value}>{value} 分</option>)}</select></label>)}</div><div className="hd-decisions"><button className={decision === 'pass' ? 'active pass' : ''} onClick={() => setDecision('pass')}>通过</button><button className={decision === 'needs_changes' ? 'active' : ''} onClick={() => setDecision('needs_changes')}>保留修改</button><button className={decision === 'reject' ? 'active reject' : ''} onClick={() => setDecision('reject')}>淘汰</button></div>{selectedArtifact.strategy.drift_review_required && <label className="hd-drift"><input type="checkbox" checked={driftConfirmed} onChange={event => setDriftConfirmed(event.target.checked)} /><span><strong>我已逐段比较人物、动作、运镜和声音漂移</strong><small>模型重生成不会因为沿用 seed 就复现同一个镜头。</small></span></label>}<label>审片备注<textarea rows={3} value={note} onChange={event => setNote(event.target.value)} placeholder="记录漂移、细节收益和后续装配注意事项" /></label><div className="hd-step-actions"><button className="button secondary" disabled={Boolean(busy)} onClick={review}>保存审片修订</button><button className="button primary" disabled={selectedReview?.decision !== 'pass' || Boolean(busy) || currentArtifact?.id === selectedArtifact.id} onClick={selectArtifact}>{currentArtifact?.id === selectedArtifact.id ? '当前高清定稿' : '选为高清定稿'}</button></div></div>}
          </section>}

          {selected.master_versions.length > 0 && <section className="hd-history"><header><div><span className="eyebrow">04 · 定稿历史</span><h2>选择与回滚不会覆盖历史</h2></div>{selected.current_hd_master && <button className="button primary" onClick={onOpenTimeline}>进入成片装配 <ChevronRight size={15} /></button>}</header>{selected.master_versions.map(version => <article key={version.id}><span>R{version.revision}</span><div><strong>高清 V{selected.artifacts.find(item => item.id === version.artifact_id)?.version || '?'}</strong><small>{version.action === 'rollback' ? `回滚自 R${version.rollback_of_revision}` : '人工选择'} · 审片 R{version.review_revision} · {time(version.created_at)}</small></div>{version.current ? <em>当前</em> : <button onClick={() => rollback(version)}><RotateCcw size={14} />回滚到此版</button>}</article>)}</section>}
        </>}
      </div>}
    </section>
  </main>
}

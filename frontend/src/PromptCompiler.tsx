import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Clipboard,
  FileCheck2,
  Image,
  LoaderCircle,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Video,
  Volume2,
} from 'lucide-react'
import type { PromptPlan } from './types'

type Props = {
  projectId: string
  setNotice: (message: string) => void
  onOpenStoryboard: (shotId: string) => void
}

const statusCopy = {
  preview: { label: '待预检', detail: '仅为实时预览，尚未验证节点图' },
  validated: { label: '预检通过', detail: 'H3 已构建节点图，尚未提交 GPU' },
  approved: { label: '已批准', detail: '生产任务会使用这份不可变快照' },
}

const roleLabels: Record<string, string> = {
  identity: '人物身份', costume: '服装造型', location: '场景', style: '画风', prop: '道具',
  action: '动作', camera: '运镜', performance: '表演', voice: '声音', ambience: '环境声',
  effects: '音效', music: '音乐', generic: '通用',
}

const mediaIcons = { image: Image, video: Video, audio: Volume2 }

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    const detail = payload?.detail
    const message = typeof detail === 'string' ? detail : detail?.message || `请求失败：${response.status}`
    throw new Error(message)
  }
  return response.json()
}

function formatTime(value?: string) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

export function PromptCompiler({ projectId, setNotice, onOpenStoryboard }: Props) {
  const [plans, setPlans] = useState<PromptPlan[]>([])
  const [selectedShotId, setSelectedShotId] = useState('')
  const [loading, setLoading] = useState(true)
  const [action, setAction] = useState<'dry-run' | 'approve' | ''>('')
  const [error, setError] = useState('')

  const selected = useMemo(
    () => plans.find((plan) => plan.shot.id === selectedShotId) || plans[0],
    [plans, selectedShotId],
  )

  const load = useCallback(async (preserveShot = true) => {
    setLoading(true)
    setError('')
    try {
      const next = await api<PromptPlan[]>('/api/prompt-plans')
      setPlans(next)
      setSelectedShotId((current) => preserveShot && next.some((item) => item.shot.id === current) ? current : next[0]?.shot.id || '')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '无法加载编译计划')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load(false) }, [load, projectId])

  const replacePlan = (next: PromptPlan) => {
    setPlans((current) => current.map((item) => item.shot.id === next.shot.id ? next : item))
  }

  const dryRun = async () => {
    if (!selected || action) return
    setAction('dry-run')
    setError('')
    try {
      const result = await api<PromptPlan>(`/api/shots/${selected.shot.id}/prompt-plan/dry-run`, {
        method: 'POST', body: JSON.stringify({ plan_hash: selected.plan_hash }),
      })
      replacePlan(result)
      setNotice('H3 节点图预检通过：没有提交 GPU，也没有生成视频')
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : 'H3 预检失败'
      setError(message)
      setNotice(message)
      await load()
    } finally {
      setAction('')
    }
  }

  const approve = async () => {
    if (!selected || selected.status !== 'validated' || action) return
    setAction('approve')
    setError('')
    try {
      const result = await api<PromptPlan>(`/api/shots/${selected.shot.id}/prompt-plan/approve`, {
        method: 'POST', body: JSON.stringify({ plan_hash: selected.plan_hash }),
      })
      replacePlan(result)
      setNotice('生成计划已批准；镜头或圣经变更后会自动要求重新预检')
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '批准失败'
      setError(message)
      setNotice(message)
      await load()
    } finally {
      setAction('')
    }
  }

  if (loading && !selected) return <main className="compiler-loading"><LoaderCircle className="spin" size={20} />正在编译项目镜头…</main>

  return <main className="compiler-page">
    <header className="compiler-hero">
      <div>
        <span className="eyebrow">H3 PROMPT COMPILER</span>
        <h1>生成计划</h1>
        <p>把分镜、锁定的生产圣经和真实素材编译为可追溯的 H3 输入；原始镜头提示词始终保留。</p>
      </div>
      <div className="compiler-flow" aria-label="编译流程">
        <span>分镜事实</span><ArrowRight size={14} /><span>编译预览</span><ArrowRight size={14} /><span>H3 dry-run</span><ArrowRight size={14} /><strong>人工批准</strong>
      </div>
    </header>

    <section className="compiler-layout">
      <aside className="compiler-shot-list">
        <div className="compiler-list-head"><span>镜头计划</span><button title="刷新编译预览" onClick={() => void load()} disabled={loading}><RefreshCw className={loading ? 'spin' : ''} size={15} /></button></div>
        {plans.map((plan) => {
          const copy = statusCopy[plan.status]
          return <button key={plan.shot.id} className={selected?.shot.id === plan.shot.id ? 'selected' : ''} onClick={() => setSelectedShotId(plan.shot.id)}>
            <span className={`compiler-state-dot ${plan.status}`} />
            <span className="compiler-shot-copy"><strong>{String(plan.shot.ordinal).padStart(2, '0')} · {plan.shot.title}</strong><small>{plan.mode} · {plan.spec.frames} 帧 · {plan.spec.actual_seconds.toFixed(2)} 秒</small></span>
            <em className={plan.status}>{copy.label}</em>
          </button>
        })}
        {!plans.length && <div className="compiler-empty">项目还没有镜头</div>}
      </aside>

      {selected && <section className="compiler-workarea">
        <div className="compiler-plan-head">
          <div><span>{selected.shot.scene_code} · 镜头 {String(selected.shot.ordinal).padStart(2, '0')}</span><h2>{selected.shot.title}</h2><p>{selected.shot.description}</p></div>
          <div className={`compiler-status-card ${selected.status}`}>
            {selected.status === 'approved' ? <LockKeyhole size={18} /> : selected.status === 'validated' ? <CheckCircle2 size={18} /> : <FileCheck2 size={18} />}
            <span><strong>{statusCopy[selected.status].label}</strong><small>{statusCopy[selected.status].detail}</small></span>
          </div>
        </div>

        <div className="compiler-spec-row">
          <div><span>执行模式</span><strong>{selected.mode}</strong></div>
          <div><span>生成规格</span><strong>{selected.spec.resolution}</strong></div>
          <div><span>精确时长</span><strong>{selected.spec.frames} 帧 / {selected.spec.actual_seconds.toFixed(3)} 秒</strong></div>
          <div><span>候选策略</span><strong>{selected.spec.candidate_count} 条 × {selected.spec.steps} 步</strong></div>
        </div>

        {(selected.stale || selected.blocking.length > 0 || selected.warnings.length > 0 || error) && <div className="compiler-alerts">
          {error && <div className="blocking"><AlertTriangle size={16} /><span>{error}</span></div>}
          {selected.stale_reasons.map((item) => <div className="blocking" key={`stale-${item}`}><RefreshCw size={16} /><span>已批准计划已过期：{item}。请重新 dry-run 并批准。</span></div>)}
          {selected.blocking.map((item) => <div className="blocking" key={item}><AlertTriangle size={16} /><span>{item}</span></div>)}
          {selected.warnings.map((item) => <div className="warning" key={item}><AlertTriangle size={16} /><span>{item}</span></div>)}
        </div>}

        <div className="compiler-grid">
          <section className="compiler-card compiler-input-card">
            <header><div><span>01</span><strong>输入证据</strong></div><small>{selected.bible.length} 条圣经 · {selected.references.length} 个素材</small></header>
            <div className="compiler-source-block">
              <label>原始镜头提示词 <em>只读 · 不覆盖</em></label>
              <p>{selected.shot.prompt || '尚未填写'}</p>
              {selected.shot.dialogue && <blockquote>对白：{selected.shot.dialogue}</blockquote>}
              {selected.shot.sound && <blockquote>声音：{selected.shot.sound}</blockquote>}
              {selected.storyboard_source && <small>分镜来源：小节“{selected.storyboard_source.section_title}” · 已同步 R{selected.storyboard_source.last_synced_revision} · 当前 R{selected.storyboard_source.current_section_revision}</small>}
            </div>
            <div className="compiler-evidence-list">
              {selected.bible.map((entry) => <div key={entry.id}><ShieldCheck size={16} /><span><strong>{entry.name}</strong><small>{entry.entry_type} · 修订 {entry.revision} · {entry.apply_globally ? '全局' : '本镜头'}{entry.source_type === 'creative_character' ? ` · 来源角色卡 R${entry.source_revision}` : ''}</small></span><em>{entry.asset_ids.length ? `${entry.asset_ids.length} 素材` : '纯文本'}</em></div>)}
              {!selected.bible.length && <p className="compiler-muted">没有进入编译的锁定生产圣经条目</p>}
            </div>
          </section>

          <section className="compiler-card compiler-reference-card">
            <header><div><span>02</span><strong>动态引用映射</strong></div><small>按真实上传顺序生成标签</small></header>
            <div className="compiler-reference-list">
              {selected.references.map((reference) => {
                const Icon = mediaIcons[reference.media_type]
                return <div key={reference.id}><Icon size={17} /><span className="compiler-ref-tag">{reference.tag}{reference.audio_tag ? ` + ${reference.audio_tag}` : ''}</span><span><strong>{reference.asset_name}</strong><small>{reference.source === 'bible' ? `生产圣经 · ${reference.source_label}` : '镜头手工引用'} · {roleLabels[reference.role] || reference.role}</small></span></div>
              })}
              {!selected.references.length && <div className="compiler-no-refs"><Sparkles size={19} /><span><strong>FL2VA 文本路径</strong><small>没有引用素材，仅使用文本、画面和对白事实。</small></span></div>}
            </div>
          </section>
        </div>

        <section className="compiler-card compiler-output-card">
          <header><div><span>03</span><strong>编译输出</strong></div><button onClick={() => { void navigator.clipboard.writeText(selected.compiled_prompt); setNotice('编译结果已复制') }}><Clipboard size={14} />复制</button></header>
          <div className="compiler-sections">
            {selected.sections.map((section) => <article key={section.id}><label>{section.label}</label><pre>{section.content}</pre></article>)}
          </div>
          <footer><span>SHA-256 计划指纹</span><code>{selected.plan_hash}</code></footer>
        </section>

        <div className="compiler-actionbar">
          <div><strong>生产门禁</strong><span>{selected.status === 'approved' ? `批准于 ${formatTime(selected.approved_at)}` : selected.status === 'validated' ? `预检于 ${formatTime(selected.validated_at)}` : '先验证节点图，再批准进入生产队列'}</span></div>
          <button className="button secondary" onClick={() => onOpenStoryboard(selected.shot.id)}>返回编辑分镜</button>
          <button className="button secondary" disabled={!selected.ready || Boolean(action)} onClick={() => void dryRun()}>{action === 'dry-run' ? <LoaderCircle className="spin" size={16} /> : <FileCheck2 size={16} />}{selected.status === 'preview' ? '运行 H3 dry-run' : '重新预检'}</button>
          <button className="button primary" disabled={selected.status !== 'validated' || Boolean(action)} onClick={() => void approve()}>{action === 'approve' ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}批准生成计划</button>
        </div>
      </section>}
    </section>
  </main>
}

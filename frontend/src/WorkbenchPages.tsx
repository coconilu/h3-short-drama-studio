import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  Archive,
  ArchiveRestore,
  Check,
  ChevronRight,
  CircleGauge,
  Clock3,
  Copy,
  Download,
  Film,
  FolderKanban,
  Grid2X2,
  HardDrive,
  List,
  LoaderCircle,
  Bot,
  MoreHorizontal,
  RefreshCw,
  Search,
  Server,
  Settings2,
  WandSparkles,
} from 'lucide-react'
import type { ArchiveReconciliation, Health, LocalAgentProvider, Workbench, WorkbenchActivity, WorkbenchProject, WorkspaceSettings } from './types'

type ProjectDestination = 'overview' | 'planning' | 'storyboard' | 'assets'

const phaseClass: Record<WorkbenchProject['category'], string> = {
  planning: 'planning',
  production: 'production',
  completed: 'completed',
  archived: 'archived',
}

function relativeTime(value: string) {
  const delta = Math.max(0, Date.now() - new Date(value).getTime())
  const minutes = Math.floor(delta / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  return days < 30 ? `${days} 天前` : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(new Date(value))
}

function bytes(value: number) {
  if (value < 1024 * 1024) return `${Math.max(0.1, value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`
}

function ProjectArtwork({ project }: { project: WorkbenchProject }) {
  if (project.thumbnail) return <img src={project.thumbnail} alt={`${project.title} 项目封面`} />
  return <div className="project-artwork-empty"><Film size={22} /><span>暂无封面</span></div>
}

function ActivityIcon({ item }: { item: WorkbenchActivity }) {
  if (item.item_type === 'export') return <Film size={15} />
  if (item.item_type === 'archive') return <Archive size={15} />
  if (item.kind === 'delivery') return <Check size={15} />
  if (item.kind === 'validation') return <CircleGauge size={15} />
  return <WandSparkles size={15} />
}

function ActivityRow({ item, compact = false }: { item: WorkbenchActivity; compact?: boolean }) {
  return <div className={`global-activity-row ${compact ? 'compact' : ''}`}>
    <span className="activity-icon"><ActivityIcon item={item} /></span>
    <span className="activity-copy">
      <strong>{item.title}</strong>
      <small>{item.project_title} · {item.message}</small>
    </span>
    <span className="activity-state"><em>{item.state}</em><time>{relativeTime(item.updated_at)}</time></span>
  </div>
}

export function ProjectsWorkbench({ workbench, health, search, onOpenProject, onOpenSettings, onCreateArchive, onArchiveProject, onRestoreProject, onResolveArchiveReconciliation }: {
  workbench: Workbench
  health: Health | null
  search: string
  onOpenProject: (projectId: string, destination: ProjectDestination) => void
  onOpenSettings: () => void
  onCreateArchive: (projectId: string) => Promise<void>
  onArchiveProject: (projectId: string) => Promise<void>
  onRestoreProject: (projectId: string) => Promise<void>
  onResolveArchiveReconciliation: (record: ArchiveReconciliation, note: string) => Promise<void>
}) {
  const [filter, setFilter] = useState<'all' | WorkbenchProject['category']>('all')
  const [sort, setSort] = useState<'active' | 'updated' | 'created' | 'progress'>('active')
  const [view, setView] = useState<'list' | 'grid'>('list')
  const [openMenu, setOpenMenu] = useState<string | null>(null)
  const [busyProject, setBusyProject] = useState<string | null>(null)
  const [busyReconciliation, setBusyReconciliation] = useState<string | null>(null)
  const [reconciliationNotes, setReconciliationNotes] = useState<Record<string, string>>({})
  const [reconciliationConfirmations, setReconciliationConfirmations] = useState<Record<string, boolean>>({})
  const runProjectAction = async (projectId: string, action: (projectId: string) => Promise<void>) => {
    setBusyProject(projectId)
    try {
      await action(projectId)
      setOpenMenu(null)
    } finally {
      setBusyProject(null)
    }
  }
  const resolveReconciliation = async (record: ArchiveReconciliation) => {
    const note = reconciliationNotes[record.id]?.trim() || ''
    if (!reconciliationConfirmations[record.id] || note.length < 8) return
    if (!window.confirm(`确认“${record.project_title || record.project_id}”没有存活归档进程，并解除这条异常冻结吗？`)) return
    setBusyReconciliation(record.id)
    try {
      await onResolveArchiveReconciliation(record, note)
    } finally {
      setBusyReconciliation(null)
    }
  }
  const reconciliationReason: Record<ArchiveReconciliation['reason'], string> = {
    taskless_lease: '旧版冻结缺少归档任务',
    terminal_task_lease: '终态任务仍持有冻结',
    task_project_mismatch: '任务与冻结所属项目不一致',
    task_lease_owner_mismatch: '任务与冻结 owner 不一致',
  }
  const projects = useMemo(() => {
    const query = search.trim().toLocaleLowerCase('zh-CN')
    return [...workbench.projects]
      .filter((project) => filter === 'all' || project.category === filter)
      .filter((project) => !query || `${project.title} ${project.episode} ${project.logline}`.toLocaleLowerCase('zh-CN').includes(query))
      .sort((left, right) => {
        if (sort === 'active' && left.active !== right.active) return left.active ? -1 : 1
        if (sort === 'progress') return right.progress - left.progress
        const field = sort === 'created' ? 'created_at' : 'updated_at'
        return new Date(right[field]).getTime() - new Date(left[field]).getTime()
      })
  }, [filter, search, sort, workbench.projects])

  const metrics = [
    ['项目', workbench.summary.project_count, '个工作区', FolderKanban],
    ['待生成镜头', workbench.summary.pending_generation, '个镜头', WandSparkles],
    ['待审片', workbench.summary.pending_review, '个镜头', Film],
    ['活动队列', workbench.queue.length, '个任务', Activity],
  ] as const

  return <main className="global-page projects-workbench">
    <div className="workbench-columns">
      <div className="workbench-main-column">
        <section className="workbench-heading">
          <div><h1>所有项目</h1><p>集中管理短剧、生产进度与本机运行状态。</p></div>
        </section>
        <section className="summary-strip" aria-label="项目概况">
          {metrics.map(([label, value, suffix, Icon]) => <article key={label}><Icon size={18} /><span>{label}<strong>{value}</strong><small>{suffix}</small></span></article>)}
        </section>
        <section className="project-surface">
        <div className="project-toolbar">
          <select aria-label="筛选项目状态" value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}><option value="all">全部状态</option><option value="production">制作中</option><option value="planning">筹备中</option><option value="completed">已完成</option><option value="archived">已归档</option></select>
          <select aria-label="项目排序" value={sort} onChange={(event) => setSort(event.target.value as typeof sort)}><option value="active">当前项目优先</option><option value="updated">最近更新</option><option value="created">最近创建</option><option value="progress">制作进度</option></select>
          <div className="view-switch" aria-label="项目视图">
            <button className={view === 'list' ? 'active' : ''} aria-label="列表视图" title="列表视图" onClick={() => setView('list')}><List size={16} /></button>
            <button className={view === 'grid' ? 'active' : ''} aria-label="网格视图" title="网格视图" onClick={() => setView('grid')}><Grid2X2 size={15} /></button>
          </div>
        </div>

        <div className={`project-collection ${view}`}>
          {projects.map((project) => <article className={`project-record ${project.archived ? 'archived' : ''}`} key={project.id}>
            <div className="project-artwork"><ProjectArtwork project={project} /></div>
            <div className="project-record-main">
              <span className="project-record-title"><strong>{project.title}</strong><em>{project.episode}</em>{project.active && <small>当前项目</small>}</span>
              <p>{project.logline}</p>
              <div className="project-facts">
                <span><Film size={14} /><strong>{project.shot_count} 个镜头</strong><small>镜头总数</small></span>
                <span><Check size={14} /><strong>{project.final_count} 个已定稿</strong><small>已完成镜头</small></span>
                <span><Clock3 size={14} /><strong>{project.planned_seconds.toFixed(1)} 秒</strong><small>计划时长</small></span>
                <span><Activity size={14} /><strong>{relativeTime(project.updated_at)}</strong><small>更新时间</small></span>
              </div>
              <div className="project-progress"><label>制作进度</label><span><i style={{ width: `${project.progress}%` }} /></span><small>{project.progress}%（{project.final_count}/{project.shot_count}）</small></div>
            </div>
            <div className="project-record-actions">
              <span className={`phase ${phaseClass[project.category]}`}>{project.phase}</span>
              <div>{project.archived
                ? <button className="continue-project" disabled={busyProject === project.id} onClick={() => runProjectAction(project.id, onRestoreProject)}><ArchiveRestore size={15} />恢复项目</button>
                : <button className="continue-project" onClick={() => onOpenProject(project.id, project.shot_count ? 'storyboard' : 'planning')}>{project.shot_count ? '继续制作' : '开始规划'}<ChevronRight size={15} /></button>}
                <div className="project-menu">
                  <button aria-label={`${project.title} 项目操作`} title="项目操作" onClick={() => setOpenMenu(openMenu === project.id ? null : project.id)}><MoreHorizontal size={18} /></button>
                  {openMenu === project.id && <div className="project-menu-popover">
                    {!project.archived && <button onClick={() => onOpenProject(project.id, 'overview')}>打开项目概览</button>}
                    {!project.archived && <button onClick={() => onOpenProject(project.id, 'planning')}>进入创作规划</button>}
                    {!project.archived && <button onClick={() => onOpenProject(project.id, 'storyboard')}>进入剧本与分镜</button>}
                    {!project.archived && <button onClick={() => onOpenProject(project.id, 'assets')}>查看项目素材</button>}
                    <button disabled={busyProject === project.id} onClick={() => runProjectAction(project.id, onCreateArchive)}><Download size={13} />创建并下载归档包</button>
                    {project.archived
                      ? <button disabled={busyProject === project.id} onClick={() => runProjectAction(project.id, onRestoreProject)}><ArchiveRestore size={13} />恢复到工作台</button>
                      : <button disabled={project.active || busyProject === project.id} title={project.active ? '请先切换到另一个项目' : ''} onClick={() => runProjectAction(project.id, onArchiveProject)}><Archive size={13} />归档项目</button>}
                  </div>}
                </div>
              </div>
            </div>
          </article>)}
          {!projects.length && <div className="global-empty"><Search size={24} /><strong>没有符合条件的项目</strong><span>调整搜索词或状态筛选即可恢复列表。</span></div>}
        </div>
        </section>
      </div>

      <aside className="workbench-rail">
        {!!workbench.archive_reconciliations.length && <section className="archive-reconciliation-panel" aria-label="归档冻结对账">
          <div className="rail-heading"><h2>归档冻结对账</h2><ArchiveRestore size={15} /></div>
          <p className="archive-reconciliation-warning">检测到旧版或不一致的归档冻结。系统不会自动解除；请先确认本机没有存活归档进程。</p>
          {workbench.archive_reconciliations.map((record) => <article key={record.id} className="archive-reconciliation-card">
            <header><strong>{record.project_title || record.project_id}</strong><em>{record.state === 'resolving' ? '清理中' : record.state === 'cleanup_failed' ? '清理失败' : `R${record.revision}`}</em></header>
            <p>{reconciliationReason[record.reason] || record.reason}</p>
            {record.cleanup_error && <p className="archive-reconciliation-error">上次清理未完成：{record.cleanup_error}</p>}
            <small>lease {record.lease_id}{record.task_id ? ` · task ${record.task_id}` : ' · 无 task'}</small>
            {record.resolution_stage && <small>
              阶段 {record.resolution_stage}
              {record.resolution_owner_pid ? ` · resolver PID ${record.resolution_owner_pid}` : ''}
              {record.resolution_heartbeat_at ? ` · 心跳 ${relativeTime(record.resolution_heartbeat_at)}` : ''}
            </small>}
            <textarea
              aria-label={`${record.project_title || record.project_id}归档对账说明`}
              placeholder="记录检查过的进程、日志和判断依据（至少 8 个字符）"
              disabled={record.state === 'resolving'}
              value={reconciliationNotes[record.id] || ''}
              onChange={(event) => setReconciliationNotes((items) => ({ ...items, [record.id]: event.target.value }))}
            />
            <label className="archive-reconciliation-confirm">
              <input
                type="checkbox"
                checked={!!reconciliationConfirmations[record.id]}
                disabled={record.state === 'resolving'}
                onChange={(event) => setReconciliationConfirmations((items) => ({ ...items, [record.id]: event.target.checked }))}
              />
              我已确认没有存活归档进程
            </label>
            <button
              onClick={() => resolveReconciliation(record)}
              disabled={record.state === 'resolving' || busyReconciliation === record.id || !reconciliationConfirmations[record.id] || (reconciliationNotes[record.id]?.trim().length || 0) < 8}
            >
              {record.state === 'resolving' || busyReconciliation === record.id ? <LoaderCircle className="spin" size={13} /> : <ArchiveRestore size={13} />}
              {record.state === 'resolving' ? '正在清理受管路径' : record.state === 'cleanup_failed' ? '重试清理并解除冻结' : '确认并解除异常冻结'}
            </button>
          </article>)}
        </section>}
        <section><div className="rail-heading"><h2>最近活动</h2><Activity size={15} /></div>{workbench.activities.slice(0, 5).map((item) => <ActivityRow item={item} compact key={item.id} />)}{!workbench.activities.length && <p className="rail-empty">还没有生产活动。</p>}</section>
        <section className="system-card">
          <div className="rail-heading"><h2>系统状态</h2><button aria-label="打开系统设置" title="打开系统设置" onClick={onOpenSettings}><Settings2 size={15} /></button></div>
          <div><span><i className={health?.comfyui === 'online' ? 'online' : ''} />ComfyUI</span><strong>{health?.comfyui === 'online' ? '在线' : '离线'}</strong></div>
          <div><span><i className={health?.export_worker === 'online' ? 'online' : ''} />导出服务</span><strong>{health?.export_worker === 'online' ? '在线' : '离线'}</strong></div>
          <div><span><Server size={13} />GPU</span><strong title={health?.gpu || ''}>{health?.gpu ? health.gpu.replace(/^.*?(NVIDIA|AMD|Intel)/, '$1').split(':')[0].trim() : '未识别'}</strong></div>
          <div><span><HardDrive size={13} />受管文件</span><strong>{bytes(workbench.storage.managed_bytes)}</strong></div>
          <div><span><Server size={13} />磁盘可用</span><strong>{bytes(workbench.storage.disk_free_bytes)}</strong></div>
        </section>
      </aside>
    </div>
  </main>
}

export function GlobalActivityPage({ workbench, onOpenProject }: { workbench: Workbench; onOpenProject: (projectId: string, destination: ProjectDestination) => void }) {
  return <main className="global-page simple-global-page">
    <div className="page-heading"><div><h1>最近活动</h1><p>跨项目查看 dry-run、生成、审片和导出记录。</p></div></div>
    <section className="global-list"><div className="global-list-header"><span>活动</span><span>项目</span><span>状态</span><span>时间</span></div>{workbench.activities.map((item) => <button className="activity-table-row" key={item.id} onClick={() => onOpenProject(item.project_id, item.item_type === 'export' ? 'overview' : 'storyboard')}><span><ActivityIcon item={item} /><span><strong>{item.title}</strong><small>{item.message}</small></span></span><span>{item.project_title}</span><em>{item.state}</em><time>{relativeTime(item.updated_at)}</time></button>)}{!workbench.activities.length && <div className="global-empty"><Activity size={24} /><strong>还没有活动记录</strong><span>完成一次 dry-run 或生成任务后，这里会自动出现记录。</span></div>}</section>
  </main>
}

export function GlobalQueuePage({ workbench, onOpenProject }: { workbench: Workbench; onOpenProject: (projectId: string, destination: ProjectDestination) => void }) {
  return <main className="global-page simple-global-page">
    <div className="page-heading"><div><h1>全局队列</h1><p>这里只显示正在排队、生成、导出或取消中的真实任务。</p></div><span className="queue-total">{workbench.queue.length} 个活动任务</span></div>
    <section className="global-list queue-list">{workbench.queue.map((item) => <button className="queue-record" key={item.id} onClick={() => onOpenProject(item.project_id, item.item_type === 'export' ? 'overview' : 'storyboard')}><span className="activity-icon"><ActivityIcon item={item} /></span><span><strong>{item.title}</strong><small>{item.project_title} · {item.message}</small></span><em>{item.state}</em><ChevronRight size={16} /></button>)}{!workbench.queue.length && <div className="global-empty tall"><Check size={25} /><strong>当前没有占用 GPU 的任务</strong><span>队列是实时状态，不会用历史任务填充。</span></div>}</section>
  </main>
}

export function SettingsPage({ settings, health, onSave, onTest, onRestartApi }: {
  settings: WorkspaceSettings
  health: Health | null
  onSave: (settings: WorkspaceSettings) => Promise<void>
  onTest: (comfyuiUrl: string) => Promise<void>
  onRestartApi: () => Promise<void>
}) {
  const [draft, setDraft] = useState(settings)
  const [saving, setSaving] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [providers, setProviders] = useState<LocalAgentProvider[]>([])
  const [probing, setProbing] = useState('')
  const [savingProvider, setSavingProvider] = useState('')
  useEffect(() => setDraft(settings), [settings])
  useEffect(() => {
    fetch('/api/local-agents/providers').then(async (response) => {
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }).then(setProviders).catch(() => setProviders([]))
  }, [])
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setSaving(true)
    try { await onSave(draft) } finally { setSaving(false) }
  }
  const supervisor = health?.supervisor
  const apiService = supervisor?.services?.api
  const webService = supervisor?.services?.web
  const restart = async () => {
    setRestarting(true)
    try { await onRestartApi() } finally { window.setTimeout(() => setRestarting(false), 3000) }
  }
  const copyLogs = async () => {
    const logs = [apiService?.log_path, webService?.log_path, supervisor?.status_path].filter(Boolean).join('\n')
    await navigator.clipboard.writeText(logs)
  }
  const probeAgent = async (providerId: string) => {
    setProbing(providerId)
    try {
      const response = await fetch(`/api/local-agents/providers/${providerId}/probe`, { method: 'POST' })
      if (!response.ok) throw new Error(await response.text())
      const provider: LocalAgentProvider = await response.json()
      setProviders((items) => items.map((item) => item.id === provider.id ? provider : item))
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '探测失败'
      setProviders((items) => items.map((item) => item.id === providerId ? { ...item, callable: false, probe_state: 'unavailable', auth_state: 'unavailable', model_state: 'unavailable', callable_state: 'unavailable', last_error: message, action_hint: `探测失败：${message}` } : item))
    } finally { setProbing('') }
  }
  const updateProviderDraft = (providerId: string, values: Partial<LocalAgentProvider>) => {
    setProviders((items) => items.map((item) => item.id === providerId ? { ...item, ...values } : item))
  }
  const saveProvider = async (provider: LocalAgentProvider) => {
    setSavingProvider(provider.id)
    try {
      const response = await fetch(`/api/local-agents/providers/${provider.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          executable_path: provider.executable_path || '',
          enabled: provider.enabled,
          timeout_seconds: provider.timeout_seconds,
          model: provider.model,
        }),
      })
      if (!response.ok) throw new Error(await response.text())
      const saved: LocalAgentProvider = await response.json()
      setProviders((items) => items.map((item) => item.id === saved.id ? saved : item))
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '保存失败'
      setProviders((items) => items.map((item) => item.id === provider.id ? { ...item, last_error: message, action_hint: `保存失败：${message}` } : item))
    } finally { setSavingProvider('') }
  }
  return <main className="global-page settings-page">
    <div className="page-heading"><div><h1>系统设置</h1><p>管理本机连接、默认导出参数与工作台偏好。</p></div><span className={`connection ${health?.comfyui === 'online' ? 'online' : ''}`}><i />ComfyUI {health?.comfyui === 'online' ? '在线' : '离线'}</span></div>
    <form onSubmit={submit} className="settings-form">
      <section className="runtime-diagnostics"><div className="settings-section-copy"><CircleGauge size={18} /><div><h2>运行与恢复</h2><p>显示真实进程状态；只有统一启动器托管的服务才能从工作台安全重启。</p></div></div><div><div className="runtime-service-grid"><article className={health?.api === 'online' ? 'online' : ''}><span><i />工作台 API</span><strong>{health?.api === 'online' ? '在线' : '离线'}</strong><small>PID {health?.api_pid || '—'} · 已运行 {Math.floor((health?.api_uptime_seconds || 0) / 60)} 分钟</small></article><article className={apiService?.managed ? 'online' : ''}><span><i />守护进程</span><strong>{supervisor?.state === 'online' ? '已托管' : '未托管'}</strong><small>{supervisor?.message || '尚未读取状态'}</small></article><article className={webService?.state === 'online' || webService?.state === 'external' ? 'online' : ''}><span><i />网页服务</span><strong>{webService?.state === 'online' ? '在线' : webService?.state === 'external' ? '外部运行' : '未知'}</strong><small>端口 {webService?.port || 4173} · 已恢复 {webService?.restarts || 0} 次</small></article></div><div className="runtime-controls"><span>数据库：<code>{health?.database_path || '读取中'}</code></span><div><button type="button" className="button secondary" onClick={copyLogs} disabled={!supervisor}><Copy size={15} />复制日志路径</button><button type="button" className="button secondary danger-outline" onClick={restart} disabled={restarting || supervisor?.state !== 'online' || !apiService?.managed}>{restarting ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}{restarting ? '等待重连' : '重启 API'}</button></div></div></div></section>
      <section><div className="settings-section-copy"><Bot size={18} /><div><h2>本地 Agent</h2><p>分别配置并探测 Codex/Kimi CLI；安装、登录、模型配置与实际可调用性分开显示，探测不会消耗模型额度。</p></div></div><div className="agent-provider-grid">{providers.map((provider) => <article className={provider.callable ? 'online' : ''} key={provider.id}><header><span><i />{provider.label}</span><strong>{provider.callable_state === 'verified' ? '可调用·已验证' : provider.callable ? '可尝试·未验证' : provider.installed ? '配置异常' : '未安装'}</strong></header><p>{provider.action_hint}</p><div className="agent-provider-fields"><label>可执行文件<input value={provider.executable_path || ''} onChange={(event) => updateProviderDraft(provider.id, { executable_path: event.target.value })} placeholder={provider.adapter} /></label><label>模型（可选）<input value={provider.model} onChange={(event) => updateProviderDraft(provider.id, { model: event.target.value })} placeholder="使用 CLI 默认模型" /></label><label>超时秒数<input type="number" min="1" max="3600" value={provider.timeout_seconds} onChange={(event) => updateProviderDraft(provider.id, { timeout_seconds: Number(event.target.value) })} /></label><label className="switch-field"><input type="checkbox" checked={provider.enabled} onChange={(event) => updateProviderDraft(provider.id, { enabled: event.target.checked })} /><span>启用</span></label></div><small>安装 {provider.installed ? '已确认' : '未确认'} · 登录 {provider.auth_state} · 模型 {provider.model_state} · 调用 {provider.callable_state}<br />{provider.version || provider.last_error || '尚未探测'}</small><footer><code>{provider.adapter} · {provider.capabilities.length} 层合同</code><div><button type="button" className="button secondary" disabled={probing === provider.id} onClick={() => probeAgent(provider.id)}>{probing === provider.id ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}探测</button><button type="button" className="button secondary" disabled={savingProvider === provider.id} onClick={() => saveProvider(provider)}>{savingProvider === provider.id ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}保存并探测</button></div></footer></article>)}{!providers.length && <div className="agent-provider-empty">Agent 状态暂时无法读取；不影响人工编辑。</div>}</div></section>
      <section><div className="settings-section-copy"><Server size={18} /><div><h2>ComfyUI 连接</h2><p>保存后，健康检查和后续任务会使用这个地址。</p></div></div><div className="settings-fields"><label>服务地址<input value={draft.comfyui_url} onChange={(event) => setDraft({ ...draft, comfyui_url: event.target.value })} /></label><button type="button" className="button secondary" onClick={() => onTest(draft.comfyui_url)}>测试当前连接</button></div></section>
      <section><div className="settings-section-copy"><Film size={18} /><div><h2>默认导出</h2><p>只影响新提交的横屏成片任务，不改写已有版本。</p></div></div><div className="settings-fields export-defaults"><label>宽度<input type="number" min="320" max="3840" value={draft.default_export_width} onChange={(event) => setDraft({ ...draft, default_export_width: Number(event.target.value) })} /></label><label>高度<input type="number" min="180" max="2160" value={draft.default_export_height} onChange={(event) => setDraft({ ...draft, default_export_height: Number(event.target.value) })} /></label><label className="switch-field"><input type="checkbox" checked={draft.polish_audio} onChange={(event) => setDraft({ ...draft, polish_audio: event.target.checked })} /><span>启用音频润色</span></label></div></section>
      <section><div className="settings-section-copy"><Settings2 size={18} /><div><h2>界面偏好</h2><p>侧栏状态会跨重启保存；默认首页在下次打开时生效。</p></div></div><div className="settings-fields preference-fields"><label>默认首页<select value={draft.default_landing_page} onChange={(event) => setDraft({ ...draft, default_landing_page: event.target.value as WorkspaceSettings['default_landing_page'] })}><option value="projects">所有项目</option><option value="activity">最近活动</option><option value="global-queue">全局队列</option></select></label><label>信息密度<select value={draft.density} onChange={(event) => setDraft({ ...draft, density: event.target.value as WorkspaceSettings['density'] })}><option value="comfortable">舒适</option><option value="compact">紧凑</option></select></label><label className="switch-field"><input type="checkbox" checked={draft.sidebar_collapsed} onChange={(event) => setDraft({ ...draft, sidebar_collapsed: event.target.checked })} /><span>默认收起侧栏</span></label></div></section>
      <section><div className="settings-section-copy"><HardDrive size={18} /><div><h2>本机路径</h2><p>这些是当前后端实际使用的只读路径，避免误把“已配置”当成“已运行”。</p></div></div><div className="runtime-paths"><label>ComfyUI 根目录<code>{draft.runtime.comfyui_root}</code></label><label>H3 项目目录<code>{draft.runtime.h3_project_root}</code></label><label>受管素材目录<code>{draft.runtime.asset_root}</code></label><label>导出目录<code>{draft.runtime.export_root}</code></label></div></section>
      <div className="settings-actions"><span>{draft.runtime.h3_adapter ? 'H3 适配脚本已检测到' : 'H3 适配脚本缺失'}</span><button className="button primary" disabled={saving}>{saving ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}{saving ? '保存中' : '保存设置'}</button></div>
    </form>
  </main>
}

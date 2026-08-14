import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDown,
  ArrowUp,
  Activity,
  AlertTriangle,
  BookMarked,
  BookOpenText,
  Boxes,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleGauge,
  Clapperboard,
  Clock3,
  Download,
  FileCheck2,
  Film,
  FolderKanban,
  FolderPlus,
  ImagePlus,
  Library,
  Link2,
  ListVideo,
  LoaderCircle,
  PanelLeftClose,
  PanelLeftOpen,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Save,
  ScrollText,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Trash2,
  Upload,
  UserRound,
  Volume2,
  WandSparkles,
  X,
} from 'lucide-react'
import type { Asset, BatchGenerationResult, Candidate, CandidateReview, DeliveryPlanItem, DeliveryWorkspace, DryRunResult, ExportPreflight, ExportRun, Health, Job, ProductionAcceptance, ProductionBatch, ProductionConflictGroup, Project, ProjectArchive, Promotion, PromptPlan, ReviewWorkspace, RoughCut, Shot, ShotReference, Workbench, WorkspaceSettings } from './types'
import { GlobalActivityPage, GlobalQueuePage, ProjectsWorkbench, SettingsPage } from './WorkbenchPages'
import { ScriptStudio } from './ScriptStudio'
import { CreativePlanning } from './CreativePlanning'
import { ProductionBible } from './ProductionBible'
import { PromptCompiler } from './PromptCompiler'
import { HDWorkbench } from './HDWorkbench'

const globalNavItems = [
  { id: 'projects', label: '所有项目', icon: FolderKanban },
  { id: 'activity', label: '最近活动', icon: Activity },
  { id: 'global-queue', label: '全局队列', icon: ListVideo },
]

const projectNavItems = [
  { id: 'overview', label: '项目概览', icon: CircleGauge },
  { id: 'planning', label: '创作规划', icon: BookOpenText },
  { id: 'script', label: '剧本开发', icon: ScrollText },
  { id: 'bible', label: '生产圣经', icon: BookMarked },
  { id: 'compiler', label: '生成计划', icon: FileCheck2 },
  { id: 'storyboard', label: '分镜与生成', icon: Clapperboard },
  { id: 'assets', label: '素材库', icon: UserRound },
  { id: 'queue', label: '项目队列', icon: ListVideo },
  { id: 'review', label: '审片台', icon: Film },
  { id: 'hd', label: '高清交付', icon: Sparkles },
  { id: 'timeline', label: '成片交付', icon: Library },
]

const globalPageIds = new Set(['projects', 'activity', 'global-queue', 'settings'])

export const activeGenerationStates = new Set([
  '提交中', '已提交待对账', '提交状态未知', '已提交', '排队中', '运行中',
])
const activeExportStates = new Set(['排队中', '恢复排队', '导出中', '取消中'])

type ApiOptions = RequestInit & { timeoutMs?: number }

const api = async <T,>(path: string, options?: ApiOptions): Promise<T> => {
  const { timeoutMs = 10000, ...fetchOptions } = options || {}
  const headers = new Headers(options?.headers)
  if (!(options?.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(path, { ...fetchOptions, headers, signal: controller.signal })
    if (!response.ok) {
      const text = await response.text()
      let message = text
      try {
        const detail = JSON.parse(text).detail
        if (typeof detail === 'string') message = detail
        else if (detail && typeof detail === 'object') {
          const record = detail as { message?: string; issues?: Array<{ shot_id?: string; message?: string }> }
          const issueText = record.issues?.map((item) => `${item.shot_id ? `${displayShotId(item.shot_id)}：` : ''}${item.message || '检查失败'}`).join('；')
          message = [record.message, issueText].filter(Boolean).join('。') || text
        }
      } catch { /* response is not JSON */ }
      throw new Error(message || `请求失败：${response.status}`)
    }
    return response.json()
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw new Error(`API 响应超时（${timeoutMs / 1000} 秒）`)
    if (error instanceof TypeError) throw new Error('无法连接镜场 API，正在自动重试')
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

function ServiceUnavailable({ error, attempting, onRetry }: { error: string; attempting: boolean; onRetry: () => void }) {
  const copyStartCommand = async () => {
    await navigator.clipboard.writeText('powershell -ExecutionPolicy Bypass -File .\\scripts\\start.ps1')
  }
  return <main className="service-unavailable">
    <section>
      <div className="recovery-brand">镜场 <span>LOCAL STUDIO</span></div>
      <div className="recovery-status"><AlertTriangle size={19} /><span><strong>工作台 API 尚未连接</strong><small>网页仍在运行，每 3 秒自动重试一次</small></span></div>
      <h1>项目数据没有丢失，<br />只是本机服务暂时离线。</h1>
      <p>{error}</p>
      <dl><div><dt>检查地址</dt><dd>127.0.0.1:8765/api/health</dd></div><div><dt>本地数据</dt><dd>保存在 SQLite 与 runtime 目录，不会因刷新页面而删除</dd></div></dl>
      <div className="recovery-actions"><button className="button primary" disabled={attempting} onClick={onRetry}>{attempting ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}{attempting ? '正在重连' : '立即重连'}</button><button className="button secondary" onClick={copyStartCommand}>复制启动命令</button></div>
    </section>
  </main>
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status status-${status}`}>{status}</span>
}

const referenceRoleOptions: Record<'image' | 'video' | 'audio', Array<[string, string]>> = {
  image: [['identity', '人物身份'], ['costume', '服装造型'], ['location', '场景连续性'], ['style', '画风光色'], ['prop', '关键道具'], ['generic', '通用参考']],
  video: [['action', '动作节奏'], ['camera', '运镜构图'], ['performance', '表演参考'], ['generic', '通用参考']],
  audio: [['voice', '角色声音'], ['ambience', '环境声'], ['effects', '音效'], ['music', '音乐'], ['generic', '通用参考']],
}

const mediaLabels = { image: '图片', video: '视频', audio: '音频' }

function defaultReferenceRole(asset: Asset) {
  if (asset.media_type === 'video') return 'action'
  if (asset.media_type === 'audio') return asset.kind.includes('声音') ? 'voice' : 'ambience'
  if (asset.kind.includes('角色') || asset.kind.includes('人物')) return 'identity'
  if (asset.kind.includes('画风')) return 'style'
  if (asset.kind.includes('道具')) return 'prop'
  if (asset.kind.includes('场景')) return 'location'
  return 'generic'
}

function AssetPreview({ asset, compact = false }: { asset: Asset; compact?: boolean }) {
  if (asset.media_type === 'video') return <video className={compact ? 'compact-media' : ''} src={asset.preview} muted preload="metadata" />
  if (asset.media_type === 'audio') return <div className={`audio-preview ${compact ? 'compact-media' : ''}`}><Volume2 size={compact ? 18 : 28} /><span>音频参考</span></div>
  return <img className={compact ? 'compact-media' : ''} src={asset.preview} alt={asset.name} />
}

function ShotThumbnail({ shot }: { shot: Shot }) {
  return shot.thumbnail ? <img src={shot.thumbnail} alt="" /> : <div className="shot-thumbnail-empty">待生成</div>
}

function displayShotId(shotId: string) {
  if (!shotId.startsWith('project-')) return shotId
  const match = shotId.match(/-(S\d+)-(\d{3})$/)
  return match ? `${match[1]}-${match[2]}` : shotId
}

function assetMeta(asset: Asset) {
  const size = asset.size_bytes ? `${(asset.size_bytes / 1024 / 1024).toFixed(1)} MB` : ''
  const dimensions = asset.width && asset.height ? `${asset.width}×${asset.height}` : ''
  const duration = asset.duration_seconds ? `${asset.duration_seconds.toFixed(2)} 秒` : ''
  return [mediaLabels[asset.media_type || 'image'], dimensions, duration, size].filter(Boolean).join(' · ')
}

function App() {
  const [activePage, setActivePage] = useState('projects')
  const [project, setProject] = useState<Project | null>(null)
  const [workbench, setWorkbench] = useState<Workbench | null>(null)
  const [settings, setSettings] = useState<WorkspaceSettings | null>(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [projectSearch, setProjectSearch] = useState('')
  const [health, setHealth] = useState<Health | null>(null)
  const [assets, setAssets] = useState<Asset[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [roughCut, setRoughCut] = useState<RoughCut>({ available: false })
  const [exportPreflight, setExportPreflight] = useState<ExportPreflight | null>(null)
  const [exportRuns, setExportRuns] = useState<ExportRun[]>([])
  const [deliveryWorkspace, setDeliveryWorkspace] = useState<DeliveryWorkspace | null>(null)
  const [acceptance, setAcceptance] = useState<ProductionAcceptance | null>(null)
  const [selectedShotId, setSelectedShotId] = useState('EP01-S01-03')
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [promotions, setPromotions] = useState<Promotion[]>([])
  const [reviewWorkspace, setReviewWorkspace] = useState<ReviewWorkspace | null>(null)
  const [references, setReferences] = useState<ShotReference[]>([])
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [startupError, setStartupError] = useState('')
  const [startupAttempting, setStartupAttempting] = useState(false)
  const [startupRetry, setStartupRetry] = useState(0)
  const [apiOnline, setApiOnline] = useState(true)
  const [modal, setModal] = useState<'new' | 'project' | 'confirm' | null>(null)
  const initialPreferencesApplied = useRef(false)
  const apiOnlineRef = useRef(true)

  const selectedShot = useMemo(
    () => project?.shots.find((shot) => shot.id === selectedShotId) || project?.shots[0],
    [project, selectedShotId],
  )

  const refresh = useCallback(async () => {
    const [projectData, workbenchData, settingsData, healthData, assetData, jobData, roughCutData, preflightData, exportRunData, deliveryData, acceptanceData] = await Promise.all([
      api<Project>('/api/project'),
      api<Workbench>('/api/workbench'),
      api<WorkspaceSettings>('/api/settings'),
      api<Health>('/api/health'),
      api<Asset[]>('/api/assets'),
      api<Job[]>('/api/jobs'),
      api<RoughCut>('/api/exports/current'),
      api<ExportPreflight>('/api/exports/preflight'),
      api<ExportRun[]>('/api/exports'),
      api<DeliveryWorkspace>('/api/delivery-plan'),
      api<ProductionAcceptance>('/api/acceptance'),
    ])
    setProject(projectData)
    setWorkbench(workbenchData)
    setSettings(settingsData)
    setHealth(healthData)
    setAssets(assetData)
    setJobs(jobData)
    setRoughCut(roughCutData)
    setExportPreflight(preflightData)
    setExportRuns(exportRunData)
    setDeliveryWorkspace(deliveryData)
    setAcceptance(acceptanceData)
    setSelectedShotId((current) => projectData.shots.some((shot) => shot.id === current) ? current : projectData.shots[0]?.id || '')
    if (!initialPreferencesApplied.current) {
      initialPreferencesApplied.current = true
      setSidebarCollapsed(settingsData.sidebar_collapsed)
      setActivePage(settingsData.default_landing_page)
    }
  }, [])

  const refreshShotReview = useCallback(async (shotId: string) => {
    const [candidateData, promotionData, referenceData, reviewData] = await Promise.all([
      api<Candidate[]>(`/api/shots/${shotId}/candidates`),
      api<Promotion[]>(`/api/shots/${shotId}/promotions`),
      api<ShotReference[]>(`/api/shots/${shotId}/references`),
      api<ReviewWorkspace>(`/api/shots/${shotId}/review-workspace`),
    ])
    setCandidates(candidateData)
    setPromotions(promotionData)
    setReferences(referenceData)
    setReviewWorkspace(reviewData)
  }, [])

  useEffect(() => {
    let completed = false
    let pending = false
    const attempt = async () => {
      if (completed || pending) return
      pending = true
      setStartupAttempting(true)
      try {
        await refresh()
        completed = true
        apiOnlineRef.current = true
        setApiOnline(true)
        setStartupError('')
      } catch (error) {
        apiOnlineRef.current = false
        setApiOnline(false)
        setStartupError(error instanceof Error ? error.message : '无法连接镜场 API')
      } finally {
        pending = false
        setStartupAttempting(false)
        setLoading(false)
      }
    }
    attempt()
    const timer = window.setInterval(attempt, 3000)
    return () => window.clearInterval(timer)
  }, [refresh, startupRetry])

  useEffect(() => {
    if (!project) return
    let probing = false
    const probe = async () => {
      if (probing) return
      probing = true
      try {
        const nextHealth = await api<Health>('/api/health', { timeoutMs: 3000 })
        setHealth(nextHealth)
        if (!apiOnlineRef.current) {
          await refresh()
          setNotice('镜场 API 已恢复，工作区数据已重新同步')
        }
        apiOnlineRef.current = true
        setApiOnline(true)
      } catch {
        apiOnlineRef.current = false
        setApiOnline(false)
      } finally {
        probing = false
      }
    }
    const timer = window.setInterval(probe, 4000)
    return () => window.clearInterval(timer)
  }, [project, refresh])

  useEffect(() => {
    if (!selectedShotId) return
    refreshShotReview(selectedShotId).catch(() => {
      setCandidates([])
      setPromotions([])
      setReferences([])
      setReviewWorkspace(null)
    })
  }, [selectedShotId, jobs, refreshShotReview])

  const syncShot = useCallback(async (shotId: string, announce = true) => {
    try {
      const result = await api<{ message: string }>(`/api/shots/${shotId}/sync`, { method: 'POST' })
      if (announce) setNotice(result.message)
      await refresh()
    } catch (error) {
      if (announce) setNotice(error instanceof Error ? error.message : '同步失败')
    }
  }, [refresh])

  const resolveReconciliation = useCallback(async (
    job: Job,
    action: 'confirm_not_submitted' | 'accept_current_manifest',
  ) => {
    const label = action === 'confirm_not_submitted' ? '确认 ComfyUI 未收到本次提交' : '接受当前 manifest 与 ComfyUI 证据'
    const note = window.prompt(`${label}\n请输入人工核验依据（会写入审计记录）：`, '')?.trim()
    if (!note) return
    if (!window.confirm(`${label}？\n该动作会被记录，且不会静默重复提交 GPU。`)) return
    try {
      const result = await api<{ message: string }>(`/api/shots/${job.shot_id}/reconciliation/resolve`, {
        method: 'POST',
        body: JSON.stringify({
          action,
          job_id: job.id,
          expected_revision: job.reconciliation_revision || 0,
          note,
          resolved_by: 'human:workbench',
          confirm: true,
        }),
      })
      setNotice(result.message)
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '人工对账失败')
    }
  }, [refresh])

  useEffect(() => {
    const shotIds = [...new Set(
      jobs.filter((job) => job.kind === 'draft' && activeGenerationStates.has(job.state)).map((job) => job.shot_id),
    )]
    if (!shotIds.length) return
    const timer = window.setInterval(async () => {
      await Promise.all(shotIds.map((shotId) => syncShot(shotId, false)))
    }, 5000)
    return () => window.clearInterval(timer)
  }, [jobs, syncShot])

  useEffect(() => {
    if (!exportRuns.some((run) => activeExportStates.has(run.state))) return
    const timer = window.setInterval(() => {
      refresh().catch((error) => setNotice(error instanceof Error ? error.message : '导出状态同步失败'))
    }, 2500)
    return () => window.clearInterval(timer)
  }, [exportRuns, refresh])

  const submitExport = async () => {
    setNotice('正在提交平台横屏导出…')
    try {
      const run = await api<ExportRun>('/api/exports', {
        method: 'POST',
        body: JSON.stringify({
          width: settings?.default_export_width || 1344,
          height: settings?.default_export_height || 768,
          polish_audio: settings?.polish_audio ?? true,
        }),
      })
      setExportRuns((current) => [run, ...current])
      setNotice('导出任务已进入后台队列；页面会自动同步状态')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '导出任务提交失败')
    }
  }

  const mutateExport = async (runId: string, action: 'cancel' | 'retry' | 'activate') => {
    const labels = { cancel: '取消', retry: '重试', activate: '切换版本' }
    try {
      await api<ExportRun>(`/api/export-runs/${runId}/${action}`, { method: 'POST' })
      setNotice(`${labels[action]}操作已提交`)
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : `${labels[action]}失败`)
    }
  }

  const switchProject = async (projectId: string, destination?: 'overview' | 'planning' | 'script' | 'storyboard' | 'assets') => {
    if (projectId === project?.id) {
      if (destination) setActivePage(destination)
      return
    }
    setNotice('正在切换项目工作区…')
    try {
      const activated = await api<Project>(`/api/projects/${projectId}/activate`, { method: 'POST' })
      setSelectedShotId(activated.shots[0]?.id || '')
      await refresh()
      if (destination) setActivePage(destination)
      setNotice(`已切换到“${activated.title}”`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '项目切换失败')
    }
  }

  const createProjectArchive = async (projectId: string) => {
    setNotice('正在冻结项目数据并校验归档包…')
    try {
      const archive = await api<ProjectArchive>(`/api/projects/${projectId}/archives`, { method: 'POST', timeoutMs: 120000 })
      const verification = await api<{ ok: boolean; errors: string[]; archive: ProjectArchive }>(`/api/project-archives/${archive.id}/verify`, { method: 'POST', timeoutMs: 120000 })
      if (!verification.ok) throw new Error(`归档校验失败：${verification.errors.join('；')}`)
      const link = document.createElement('a')
      link.href = verification.archive.download_url
      link.download = ''
      document.body.appendChild(link)
      link.click()
      link.remove()
      setNotice(`归档 R${archive.revision} 已校验并下载：${archive.media_count} 个媒体文件，SHA-256 ${archive.checksum_sha256.slice(0, 12)}`)
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '项目归档包生成失败')
    }
  }

  const archiveProject = async (projectId: string) => {
    if (!window.confirm('归档前会先创建并校验一个可下载项目包。项目数据不会删除，确定继续吗？')) return
    setNotice('正在创建安全快照并归档项目…')
    try {
      const result = await api<{ archive: ProjectArchive }>(`/api/projects/${projectId}/archive`, { method: 'POST', timeoutMs: 120000 })
      await refresh()
      setNotice(`项目已归档并保留 R${result.archive.revision} 备份，可随时恢复到工作台`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '项目归档失败')
    }
  }

  const restoreProject = async (projectId: string) => {
    setNotice('正在恢复项目到工作台…')
    try {
      await api(`/api/projects/${projectId}/restore`, { method: 'POST' })
      await refresh()
      setNotice('项目已恢复；原归档包和历史版本保持不变')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '项目恢复失败')
    }
  }

  const runAcceptance = async () => {
    setNotice('正在冻结当前生产验收证据…')
    try {
      const result = await api<ProductionAcceptance>('/api/acceptance/run', { method: 'POST', timeoutMs: 120000 })
      setAcceptance(result)
      setNotice(`验收报告已冻结：${result.status} · ${result.latest_run?.report_hash.slice(0, 12)}`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '生产验收失败')
    }
  }

  const signoffDelivery = async (category: 'picture_continuity' | 'sound', decision: 'pass' | 'reject', note: string) => {
    setNotice('正在记录整片人工确认…')
    try {
      const result = await api<ProductionAcceptance>('/api/acceptance/signoffs', {
        method: 'POST', body: JSON.stringify({ category, decision, note, source: 'studio-human-review' }),
      })
      setAcceptance(result)
      setNotice('整片人工确认已记录为可追溯修订')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '人工确认记录失败')
    }
  }

  const updateShot = async (shotId: string, updates: Partial<Shot>) => {
    const changed = await api<Shot>(`/api/shots/${shotId}`, {
      method: 'PATCH',
      body: JSON.stringify(updates),
    })
    setProject((current) => current ? { ...current, shots: current.shots.map((shot) => shot.id === changed.id ? changed : shot) } : current)
    setNotice('镜头设置已保存')
    return changed
  }

  const validateShot = async (draft: Shot) => {
    setNotice('正在构建 H3 节点图…')
    try {
      await updateShot(draft.id, draft)
      const result = await api<DryRunResult>(`/api/shots/${draft.id}/generate`, {
        method: 'POST', body: JSON.stringify({ dry_run: true, confirm: false }),
      })
      setNotice(`${result.mode} dry-run 通过：${result.message}`)
      await refresh()
      return result
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '校验失败')
      throw error
    }
  }

  const prepareGeneration = async (draft: Shot) => {
    await updateShot(draft.id, draft)
    setModal('confirm')
  }

  const submitGeneration = async () => {
    if (!selectedShot) return
    setModal(null)
    setNotice('正在提交 ComfyUI 生成任务…')
    try {
      const result = await api<{ message: string }>(`/api/shots/${selectedShot.id}/generate`, {
        method: 'POST', body: JSON.stringify({ dry_run: false, confirm: true }),
      })
      setNotice(result.message)
      await refresh()
      setActivePage('queue')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '提交失败')
    }
  }

  const toggleSidebar = async () => {
    const next = !sidebarCollapsed
    setSidebarCollapsed(next)
    try {
      const saved = await api<WorkspaceSettings>('/api/settings', {
        method: 'PATCH', body: JSON.stringify({ sidebar_collapsed: next }),
      })
      setSettings(saved)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '侧栏偏好保存失败')
    }
  }

  const saveSettings = async (draft: WorkspaceSettings) => {
    const payload = {
      sidebar_collapsed: draft.sidebar_collapsed,
      default_landing_page: draft.default_landing_page,
      density: draft.density,
      comfyui_url: draft.comfyui_url,
      default_export_width: draft.default_export_width,
      default_export_height: draft.default_export_height,
      polish_audio: draft.polish_audio,
    }
    try {
      const saved = await api<WorkspaceSettings>('/api/settings', { method: 'PATCH', body: JSON.stringify(payload) })
      setSettings(saved)
      setSidebarCollapsed(saved.sidebar_collapsed)
      setNotice('系统设置已保存')
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '系统设置保存失败')
      throw error
    }
  }

  const testConnection = async (comfyuiUrl: string) => {
    try {
      const result = await api<{ message: string; gpu?: string }>('/api/settings/test-connection', { method: 'POST', body: JSON.stringify({ comfyui_url: comfyuiUrl }) })
      setNotice(`${result.message}${result.gpu ? ` · ${result.gpu}` : ''}`)
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '连接测试失败')
    }
  }

  const restartApi = async () => {
    const result = await api<{ message: string }>('/api/runtime/restart-api', { method: 'POST' })
    setNotice(result.message)
    apiOnlineRef.current = false
    setApiOnline(false)
  }

  if (loading || !project || !workbench || !settings) {
    if (!loading && startupError) return <ServiceUnavailable error={startupError} attempting={startupAttempting} onRetry={() => setStartupRetry((value) => value + 1)} />
    return <div className="loading"><LoaderCircle className="spin" /> 正在打开镜场…</div>
  }

  const isGlobalPage = globalPageIds.has(activePage)
  const activeLabel = [...globalNavItems, ...projectNavItems, { id: 'settings', label: '系统设置', icon: Settings }].find((item) => item.id === activePage)?.label || '工作台'

  return (
    <div className={`app-shell ${sidebarCollapsed ? 'sidebar-collapsed' : ''} density-${settings.density}`}>
      <aside className="sidebar" aria-label="工作台导航">
        <div className="brand"><span>镜场</span><button onClick={toggleSidebar} aria-label={sidebarCollapsed ? '展开侧栏' : '收起侧栏'} title={sidebarCollapsed ? '展开侧栏' : '收起侧栏'}>{sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}<em>{sidebarCollapsed ? '展开' : '收起'}</em></button></div>
        <div className="nav-section-label"><span>工作台</span></div>
        <nav className="global-nav">
          {globalNavItems.map(({ id, label, icon: Icon }) => (
            <button key={id} aria-label={label} title={label} className={activePage === id ? 'nav-active' : ''} onClick={() => setActivePage(id)}>
              <Icon size={20} strokeWidth={1.6} /><span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="project-context">
          <span>当前项目</span>
          <select aria-label="切换当前项目" value={project.id} onChange={(event) => switchProject(event.target.value, 'overview')}>{workbench.projects.map((item) => <option value={item.id} key={item.id}>{item.title}</option>)}</select>
          <button className="collapsed-project" aria-label={`打开当前项目 ${project.title}`} title={project.title} onClick={() => setActivePage('overview')}><FolderKanban size={19} /></button>
        </div>
        <nav className="project-nav">
          {projectNavItems.map(({ id, label, icon: Icon }) => (
            <button key={id} aria-label={label} title={label} className={activePage === id ? 'nav-active' : ''} onClick={() => setActivePage(id)}>
              <Icon size={20} strokeWidth={1.6} /><span>{label}</span>
            </button>
          ))}
        </nav>
        <button className={`settings ${activePage === 'settings' ? 'nav-active' : ''}`} onClick={() => setActivePage('settings')} title="系统设置"><Settings size={19} /><span>系统设置</span></button>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-breadcrumb"><span>{isGlobalPage ? '工作台' : project.title}</span><ChevronRight size={14} /><strong>{activeLabel}</strong>{!isGlobalPage && <small>{project.episode}</small>}</div>
          <div className="top-actions">
            {activePage === 'projects' && <label className="topbar-search"><Search size={15} /><input aria-label="搜索项目" value={projectSearch} onChange={(event) => setProjectSearch(event.target.value)} placeholder="搜索项目名称" /></label>}
            <button className={`connection ${health?.comfyui === 'online' ? 'online' : ''}`} onClick={() => setActivePage('settings')} title="打开系统设置"><i />ComfyUI {health?.comfyui === 'online' ? '在线' : '离线'}</button>
            <button className={`button ${activePage === 'projects' ? 'primary' : 'secondary'}`} onClick={() => setModal('project')}><FolderPlus size={17} />新建项目</button>
            {!isGlobalPage && activePage !== 'script' && activePage !== 'planning' && <button className="button secondary" onClick={() => setModal('new')}><Plus size={17} />新建镜头</button>}
          </div>
        </header>

        {!apiOnline && <div className="service-banner"><LoaderCircle className="spin" size={15} /><span><strong>API 连接中断</strong> 已加载的数据仍可查看，写操作请等待自动重连。</span><button onClick={() => setActivePage('settings')}>查看诊断</button></div>}

        {notice && <button className="notice" onClick={() => setNotice('')}>{notice}<X size={15} /></button>}

        {activePage === 'projects' && <ProjectsWorkbench workbench={workbench} health={health} search={projectSearch} onOpenProject={switchProject} onOpenSettings={() => setActivePage('settings')} onCreateArchive={createProjectArchive} onArchiveProject={archiveProject} onRestoreProject={restoreProject} />}
        {activePage === 'activity' && <GlobalActivityPage workbench={workbench} onOpenProject={switchProject} />}
        {activePage === 'global-queue' && <GlobalQueuePage workbench={workbench} onOpenProject={switchProject} />}
        {activePage === 'settings' && <SettingsPage settings={settings} health={health} onSave={saveSettings} onTest={testConnection} onRestartApi={restartApi} />}

        {activePage === 'planning' && <CreativePlanning key={project.id} projectId={project.id} setNotice={setNotice} onOpenScript={() => setActivePage('script')} onOpenStoryboard={async () => { await refresh(); setActivePage('storyboard') }} />}
        {activePage === 'script' && <ScriptStudio project={project} setNotice={setNotice} onProjectRefresh={refresh} onOpenStoryboard={() => setActivePage('storyboard')} />}
        {activePage === 'bible' && <ProductionBible key={project.id} projectId={project.id} assets={assets} setNotice={setNotice} />}
        {activePage === 'compiler' && <PromptCompiler key={project.id} projectId={project.id} setNotice={setNotice} onOpenStoryboard={(shotId) => { setSelectedShotId(shotId); setActivePage('storyboard') }} />}

        {activePage === 'storyboard' && selectedShot && (
          <Storyboard
            project={project}
            selectedShot={selectedShot}
            selectedShotId={selectedShotId}
            assets={assets}
            references={references}
            onSelect={setSelectedShotId}
            onUpdate={updateShot}
            onDryRun={validateShot}
            onPrepareGeneration={prepareGeneration}
            onReferencesChange={setReferences}
            onRefresh={refresh}
            onBatchSubmitted={async () => { await refresh(); setActivePage('queue') }}
            setNotice={setNotice}
          />
        )}
        {activePage === 'storyboard' && !selectedShot && <main className="empty-project-stage"><BookOpenText size={28} /><h1>先完成创作规划</h1><p>这个项目还没有镜头。请先确定剧情、角色、章节与小节，再进入剧本开发。</p><button className="button primary" onClick={() => setActivePage('planning')}>打开创作规划</button></main>}
        {activePage === 'overview' && <Overview project={project} health={health} assets={assets} jobs={jobs} acceptance={acceptance} onOpenStoryboard={() => setActivePage('script')} onRunAcceptance={runAcceptance} onSignoff={signoffDelivery} />}
        {activePage === 'assets' && <AssetsPage assets={assets} onRefresh={refresh} setNotice={setNotice} />}
        {activePage === 'queue' && <QueuePage jobs={jobs} onSync={syncShot} onResolve={resolveReconciliation} />}
        {activePage === 'review' && selectedShot && <ReviewPage project={project} shot={selectedShot} candidates={candidates} promotions={promotions} reviewWorkspace={reviewWorkspace} onSelectShot={setSelectedShotId} onRefresh={async () => { await Promise.all([refresh(), refreshShotReview(selectedShot.id)]) }} setNotice={setNotice} />}
        {activePage === 'review' && !selectedShot && <main className="empty-project-stage"><Film size={28} /><h1>还没有可审片的镜头</h1><p>完成创作规划和剧本开发后，再同步分镜并生成候选。</p><button className="button primary" onClick={() => setActivePage('planning')}>返回创作规划</button></main>}
        {activePage === 'hd' && <HDWorkbench key={project.id} projectId={project.id} setNotice={setNotice} onOpenTimeline={async () => { await refresh(); setActivePage('timeline') }} />}
        {activePage === 'timeline' && <TimelinePage shots={project.shots} roughCut={roughCut} preflight={exportPreflight} exportRuns={exportRuns} deliveryWorkspace={deliveryWorkspace} onExport={submitExport} onRunAction={mutateExport} onRefresh={refresh} setNotice={setNotice} />}
      </div>

      {modal === 'new' && <NewShotModal onClose={() => setModal(null)} onCreated={async (shot) => { await refresh(); setSelectedShotId(shot.id); setActivePage('storyboard'); setModal(null); setNotice('新镜头已加入分镜表') }} />}
      {modal === 'project' && <NewProjectModal onClose={() => setModal(null)} onCreated={async (created) => { setSelectedShotId(created.shots[0]?.id || ''); await refresh(); setActivePage('planning'); setModal(null); setNotice(`“${created.title}”工作区已创建，请先完善创作规划`) }} />}
      {modal === 'confirm' && selectedShot && <ConfirmModal shot={selectedShot} onClose={() => setModal(null)} onConfirm={submitGeneration} />}
    </div>
  )
}

function Storyboard({ project, selectedShot, selectedShotId, assets, references, onSelect, onUpdate, onDryRun, onPrepareGeneration, onReferencesChange, onRefresh, onBatchSubmitted, setNotice }: {
  project: Project
  selectedShot: Shot
  selectedShotId: string
  assets: Asset[]
  references: ShotReference[]
  onSelect: (id: string) => void
  onUpdate: (shotId: string, updates: Partial<Shot>) => Promise<Shot>
  onDryRun: (draft: Shot) => Promise<DryRunResult | undefined>
  onPrepareGeneration: (draft: Shot) => Promise<void>
  onReferencesChange: (references: ShotReference[]) => void
  onRefresh: () => Promise<void>
  onBatchSubmitted: () => Promise<void>
  setNotice: (value: string) => void
}) {
  const [draft, setDraft] = useState(selectedShot)
  const [dryRun, setDryRun] = useState<DryRunResult | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [batchSelection, setBatchSelection] = useState<Set<string>>(new Set())
  const [batchResult, setBatchResult] = useState<BatchGenerationResult | null>(null)
  const [batchValidatedKey, setBatchValidatedKey] = useState('')
  const [batchIdempotencyKey, setBatchIdempotencyKey] = useState('')
  const [batchBusy, setBatchBusy] = useState(false)
  const [batchConfirmOpen, setBatchConfirmOpen] = useState(false)
  const [batchScope, setBatchScope] = useState('manual')
  const [batchResolution, setBatchResolution] = useState('608x352')
  const [batchSeconds, setBatchSeconds] = useState(5.17)
  const [batchCandidateCount, setBatchCandidateCount] = useState(2)
  useEffect(() => { setDraft(selectedShot); setDryRun(null); setPickerOpen(false) }, [selectedShot.id])
  useEffect(() => { setBatchSelection(new Set()); setBatchResult(null); setBatchValidatedKey(''); setBatchIdempotencyKey(''); setBatchConfirmOpen(false) }, [project.id])

  const availableAssets = assets.filter((asset) => asset.bindable && !references.some((reference) => reference.asset_id === asset.id))

  const bindAsset = async (asset: Asset) => {
    try {
      const updated = await api<ShotReference[]>(`/api/shots/${selectedShot.id}/references`, {
        method: 'POST', body: JSON.stringify({ asset_id: asset.id, role: defaultReferenceRole(asset) }),
      })
      onReferencesChange(updated)
      setDryRun(null)
      setPickerOpen(false)
      setNotice(`已绑定“${asset.name}”，引用顺序已更新`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '素材绑定失败')
    }
  }

  const updateReferenceRole = async (reference: ShotReference, role: string) => {
    try {
      const updated = await api<ShotReference[]>(`/api/shots/${selectedShot.id}/references/${reference.id}`, {
        method: 'PATCH', body: JSON.stringify({ role }),
      })
      onReferencesChange(updated)
      setDryRun(null)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '引用用途更新失败')
    }
  }

  const moveReference = async (reference: ShotReference, offset: -1 | 1) => {
    const group = references.filter((item) => item.reference_type === reference.reference_type).sort((a, b) => a.ordinal - b.ordinal)
    const index = group.findIndex((item) => item.id === reference.id)
    const target = index + offset
    if (index < 0 || target < 0 || target >= group.length) return
    const ordered = [...group]
    ;[ordered[index], ordered[target]] = [ordered[target], ordered[index]]
    try {
      const updated = await api<ShotReference[]>(`/api/shots/${selectedShot.id}/references/order`, {
        method: 'PUT', body: JSON.stringify({ reference_type: reference.reference_type, reference_ids: ordered.map((item) => item.id) }),
      })
      onReferencesChange(updated)
      setDryRun(null)
      setNotice(`${mediaLabels[reference.reference_type]}引用顺序已更新`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '引用排序失败')
    }
  }

  const unbindReference = async (reference: ShotReference) => {
    try {
      const updated = await api<ShotReference[]>(`/api/shots/${selectedShot.id}/references/${reference.id}`, { method: 'DELETE' })
      onReferencesChange(updated)
      setDryRun(null)
      setNotice(`已从镜头解绑“${reference.asset.name}”`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '解绑失败')
    }
  }

  const runDry = async () => {
    setBusy(true)
    setDryRun(null)
    try {
      const result = await onDryRun(draft)
      if (result) setDryRun(result)
    } finally {
      setBusy(false)
    }
  }

  const selectionKey = (ids: string[]) => [...ids].sort().join('|')
  const changeBatchSelection = (shotId: string) => {
    setBatchScope('manual')
    setBatchSelection((current) => {
      const next = new Set(current)
      if (next.has(shotId)) next.delete(shotId)
      else next.add(shotId)
      return next
    })
    setBatchResult(null)
    setBatchValidatedKey('')
    setBatchIdempotencyKey('')
  }
  const selectUnfinished = () => {
    setBatchSelection(new Set(project.shots.filter((shot) => shot.status !== '已定稿' && shot.status !== '生成中').map((shot) => shot.id)))
    setBatchResult(null)
    setBatchValidatedKey('')
    setBatchIdempotencyKey('')
  }
  const clearBatchSelection = () => {
    setBatchScope('manual')
    setBatchSelection(new Set())
    setBatchResult(null)
    setBatchValidatedKey('')
    setBatchIdempotencyKey('')
  }
  const chapters = useMemo(() => {
    const values = new Map<string, string>()
    project.shots.forEach((shot) => {
      if (shot.source_mapping?.chapter_id) values.set(shot.source_mapping.chapter_id, shot.source_mapping.chapter_title)
    })
    return [...values.entries()]
  }, [project.shots])
  const selectBatchScope = (scope: string) => {
    setBatchScope(scope)
    const selected = scope === 'project'
      ? project.shots
      : scope === 'manual'
        ? []
        : project.shots.filter((shot) => shot.source_mapping?.chapter_id === scope)
    setBatchSelection(new Set(selected.filter((shot) => shot.status !== '已定稿' && shot.status !== '生成中').map((shot) => shot.id)))
    setBatchResult(null)
    setBatchValidatedKey('')
    setBatchIdempotencyKey('')
  }
  const applyBatchSpec = async () => {
    const [width, height] = batchResolution.split('x').map(Number)
    setBatchBusy(true)
    try {
      for (const shotId of batchSelection) {
        await onUpdate(shotId, { width, height, seconds: batchSeconds, candidate_count: batchCandidateCount })
      }
      setBatchResult(null)
      setBatchValidatedKey('')
      setBatchIdempotencyKey('')
      setNotice(`已将 ${batchSelection.size} 个镜头统一为 ${width}×${height} / ${batchSeconds} 秒 / ${batchCandidateCount} 候选；旧批准计划已过期，请完成 dry-run 与批准后再建批次。`)
      await onRefresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '批量规格更新失败')
    } finally { setBatchBusy(false) }
  }
  const runBatchDry = async () => {
    const shotIds = [...batchSelection]
    if (!shotIds.length) return
    setBatchBusy(true)
    try {
      if (batchSelection.has(draft.id)) await onUpdate(draft.id, draft)
      const result = await api<BatchGenerationResult>('/api/production-batches/preflight', {
        method: 'POST', body: JSON.stringify({ shot_ids: shotIds }),
      })
      setBatchResult(result)
      setBatchValidatedKey(selectionKey(shotIds))
      setBatchIdempotencyKey(crypto.randomUUID())
      setNotice(`生产门禁：${result.passed_count || 0}/${result.requested_count} 个镜头的批准计划有效，未占用 GPU`)
      await onRefresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '批量 dry-run 失败')
    } finally {
      setBatchBusy(false)
    }
  }
  const submitBatch = async () => {
    const shotIds = [...batchSelection]
    setBatchBusy(true)
    try {
      const result = await api<ProductionBatch>('/api/production-batches', {
        method: 'POST', body: JSON.stringify({
          shot_ids: shotIds, name: `${project.episode} 分镜生产`, max_attempts: 3, confirm: true,
          preflight_hash: batchResult?.preflight_hash, idempotency_key: batchIdempotencyKey,
        }),
      })
      setNotice(`生产批次已冻结 ${result.item_count} 个镜头；调度器将逐镜提交并在重启后恢复`)
      setBatchConfirmOpen(false)
      clearBatchSelection()
      await onBatchSubmitted()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '批量提交失败')
    } finally {
      setBatchBusy(false)
    }
  }
  const selectedShotIds = [...batchSelection]
  const batchReady = Boolean(batchResult?.ok && batchResult.preflight_hash && batchIdempotencyKey && batchValidatedKey === selectionKey(selectedShotIds))
  const selectedCandidateCount = project.shots.filter((shot) => batchSelection.has(shot.id)).reduce((sum, shot) => sum + shot.candidate_count, 0)

  return (
    <main className="storyboard-layout">
      <section className="shot-board">
        <div className="page-heading">
          <div><span className="eyebrow">制作阶段 · 分镜规划</span><h1>剧本与分镜</h1><p>{project.logline}</p></div>
          <div className="progress-number"><strong>{project.shots.filter(s => s.status === '已定稿').length}</strong><span>/ {project.shots.length} 定稿</span></div>
        </div>
        <section className="batch-panel">
          <div><span className="eyebrow">整集生产批次</span><strong>{batchSelection.size ? `已选 ${batchSelection.size} 个镜头` : '选择镜头后检查生产门禁'}</strong><small>只接受“生成计划”中已完成 H3 dry-run 并人工批准的当前哈希；提交后由可恢复调度器逐镜执行。</small></div>
          <div className="batch-spec-controls">
            <label>选择范围<select value={batchScope} onChange={(event) => selectBatchScope(event.target.value)}><option value="manual">手工勾选</option><option value="project">整个项目</option>{chapters.map(([id, title]) => <option value={id} key={id}>章节 · {title}</option>)}</select></label>
            <label>低清规格<select value={batchResolution} onChange={(event) => setBatchResolution(event.target.value)}><option value="608x352">608 × 352</option><option value="768x448">768 × 448</option></select></label>
            <label>时长<select value={batchSeconds} onChange={(event) => setBatchSeconds(Number(event.target.value))}><option value={5.17}>5.17 秒</option><option value={8}>8 秒</option></select></label>
            <label>每镜候选<select value={batchCandidateCount} onChange={(event) => setBatchCandidateCount(Number(event.target.value))}><option value={2}>2 条</option><option value={3}>3 条</option><option value={4}>4 条</option></select></label>
            <button className="button secondary" disabled={!batchSelection.size || batchBusy} onClick={applyBatchSpec}>应用到所选</button>
          </div>
          <div className="batch-actions"><button className="button secondary" disabled={batchBusy} onClick={selectUnfinished}>选择未定稿</button><button className="button secondary" disabled={!batchSelection.size || batchBusy} onClick={clearBatchSelection}>清空</button><button className="button primary" disabled={!batchSelection.size || batchBusy} onClick={runBatchDry}>{batchBusy ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />}检查生产门禁</button><button className="button secondary" disabled={!batchReady || batchBusy} onClick={() => setBatchConfirmOpen(true)}><Sparkles size={15} />创建生产批次</button></div>
          {batchResult && <div className="batch-results">{batchResult.results.map((result) => <span className={result.ok ? 'passed' : 'failed'} key={result.shot_id}><b>{displayShotId(result.shot_id)}</b>{result.ok ? `计划与 dry-run 凭证已冻结 · ${result.validation_hash?.slice(0, 10)}` : (result.reasons || [result.message]).filter(Boolean).join('；')}</span>)}</div>}
        </section>
        <div className="shot-progress" style={{ gridTemplateColumns: `repeat(${project.shots.length}, 1fr)` }}>
          {project.shots.map((shot, index) => <button key={shot.id} className={`${shot.id === selectedShotId ? 'current' : ''} ${shot.status === '已定稿' ? 'done' : ''}`} onClick={() => onSelect(shot.id)}><span>{String(index + 1).padStart(2, '0')}</span></button>)}
        </div>
        <div className="shot-list-header"><span /><span>镜头</span><span>画面与对白</span><span>规格</span><span>状态</span></div>
        <div className="shot-list">
          {project.shots.map((shot) => (
            <article key={shot.id} className={`shot-row ${shot.id === selectedShotId ? 'selected' : ''}`} onClick={() => onSelect(shot.id)}>
              <button className={`batch-check ${batchSelection.has(shot.id) ? 'checked' : ''}`} aria-label={`${batchSelection.has(shot.id) ? '取消选择' : '选择'} ${displayShotId(shot.id)}`} onClick={(event) => { event.stopPropagation(); changeBatchSelection(shot.id) }}>{batchSelection.has(shot.id) && <Check size={13} />}</button>
              <div className="shot-code"><strong>{displayShotId(shot.id)}</strong><span>{shot.scene_code}</span></div>
              <ShotThumbnail shot={shot} />
              <div className="shot-copy"><strong>{shot.title}</strong><p>{shot.description}</p>{shot.dialogue && <em>{shot.dialogue}</em>}{shot.sound && <small>声音：{shot.sound}</small>}{shot.source_mapping ? <small className="shot-source">来源：小节“{shot.source_mapping.section_title}” · 同步 R{shot.source_mapping.last_synced_revision}</small> : <small className="shot-source manual">历史手工分镜 · 无小节映射</small>}</div>
              <div className="shot-spec"><span>{shot.width}×{shot.height}</span><span>{shot.seconds} 秒</span><span>{shot.candidate_count} 条候选</span></div>
              <StatusPill status={shot.status} />
              <ChevronRight size={16} />
            </article>
          ))}
        </div>
      </section>
      <aside className="inspector">
        <div className="inspector-title"><div><span>镜头检查器</span><strong>{displayShotId(selectedShot.id)}</strong></div><button title="保存" onClick={() => onUpdate(draft.id, draft)}><Save size={18} /></button></div>
        <label>镜头标题<input value={draft.title} onChange={(event) => { setDraft({ ...draft, title: event.target.value }); setDryRun(null) }} /></label>
        <label>画面提示词<textarea rows={6} value={draft.prompt} onChange={(event) => { setDraft({ ...draft, prompt: event.target.value }); setDryRun(null) }} /></label>
        <label>声音提示<textarea rows={3} value={draft.sound || ''} onChange={(event) => { setDraft({ ...draft, sound: event.target.value }); setDryRun(null) }} /></label>
        <div className="reference-section">
          <div className="label-line"><span>生成路线与参考</span><button className="text-action" onClick={() => setPickerOpen(true)}><Link2 size={13} />绑定素材</button></div>
          <div className={`generation-route ${references.length ? 'reference' : 'text-only'}`}><span>{references.length ? 'REF2VA · 多模态参考' : 'FL2VA · 纯文本生成'}</span><p>{references.length ? '提示词与已排序的图片、视频或音频共同驱动生成。' : '无需任何素材；仅使用下面的画面提示词生成视频与音频。'}</p></div>
          {references.length === 0 ? <button className="reference-empty" onClick={() => setPickerOpen(true)}><Plus size={17} />可选：添加素材后自动切换 Ref2VA</button> : (
            <div className="reference-list">
              {references.map((reference) => {
                const group = references.filter((item) => item.reference_type === reference.reference_type)
                const index = group.findIndex((item) => item.id === reference.id)
                return <article key={reference.id}>
                  <AssetPreview asset={reference.asset} compact />
                  <div className="reference-copy"><strong>{reference.tag}{reference.audio_tag ? ` · ${reference.audio_tag}` : ''}</strong><span>{reference.asset.name}</span><select aria-label={`${reference.asset.name}用途`} value={reference.role} onChange={(event) => updateReferenceRole(reference, event.target.value)}>{referenceRoleOptions[reference.reference_type].map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></div>
                  <div className="reference-actions"><button aria-label="上移" disabled={index === 0} onClick={() => moveReference(reference, -1)}><ArrowUp size={14} /></button><button aria-label="下移" disabled={index === group.length - 1} onClick={() => moveReference(reference, 1)}><ArrowDown size={14} /></button><button aria-label="解绑" onClick={() => unbindReference(reference)}><Trash2 size={14} /></button></div>
                </article>
              })}
            </div>
          )}
          <small className="reference-summary">{references.length ? `图片 ${references.filter(r => r.reference_type === 'image').length}/9 · 视频 ${references.filter(r => r.reference_type === 'video').length}/3 · 音频 ${references.filter(r => r.reference_type === 'audio').length}/3` : '当前输入：画面提示词；参考素材不是必填项'}</small>
        </div>
        <div className="settings-grid">
          <label>分辨率<select value={`${draft.width}x${draft.height}`} onChange={(event) => { const [width, height] = event.target.value.split('x').map(Number); setDraft({ ...draft, width, height }); setDryRun(null) }}><option value="608x352">608 × 352</option><option value="768x448">768 × 448</option><option value="1344x768">1344 × 768</option></select></label>
          <label>时长<select value={draft.seconds} onChange={(event) => { setDraft({ ...draft, seconds: Number(event.target.value) }); setDryRun(null) }}>{![5.17, 8].includes(draft.seconds) && <option value={draft.seconds}>{draft.seconds} 秒（当前成片）</option>}<option value={5.17}>5.17 秒</option><option value={8}>8 秒</option></select></label>
          <label>候选数<select value={draft.candidate_count} onChange={(event) => { setDraft({ ...draft, candidate_count: Number(event.target.value) }); setDryRun(null) }}>{draft.candidate_count === 1 && <option value={1}>1 条（历史）</option>}<option value={2}>2 条</option><option value={3}>3 条</option><option value={4}>4 条</option></select></label>
        </div>
        <div className="strategy"><span>定稿策略</span><div><button className={draft.strategy === '保真放大' ? 'active' : ''} onClick={() => setDraft({ ...draft, strategy: '保真放大' })}>保真放大</button><button className={draft.strategy === 'Ref2VA 精修' ? 'active' : ''} onClick={() => setDraft({ ...draft, strategy: 'Ref2VA 精修' })}>Ref2VA 精修</button></div><p>{draft.strategy === '保真放大' ? '不重新生成内容，构图最稳定；不会凭空增加细节。' : '重新扩散以获得新细节；人物和运镜存在漂移风险。'}</p></div>
        <div className="generation-actions"><button className="button primary" disabled={busy} onClick={runDry}>{busy ? <LoaderCircle className="spin" size={17} /> : <WandSparkles size={17} />}Dry-run 检查</button><button className="button secondary" disabled={!dryRun || busy} onClick={() => onPrepareGeneration(draft)}><Sparkles size={16} />提交生成</button></div>
        {dryRun && <section className="dryrun-result"><div><Check size={17} /><strong>{dryRun.mode} 已通过</strong><span>未占用 GPU</span></div><p>{dryRun.resolution} · 请求 {dryRun.requested_seconds} 秒 · {dryRun.candidate_count} 条候选</p><p>{dryRun.references.length ? dryRun.references.map((reference) => `${reference.tag} ${reference.asset_name}`).join(' / ') : '无参考输入，使用文本/首尾帧路线'}</p><details><summary>查看编译后的提示词</summary><pre>{dryRun.compiled_prompt}</pre></details></section>}
        <p className="safety-note"><Check size={14} />dry-run 只构建节点图；“提交生成”才会进入 GPU 确认</p>
      </aside>
      {pickerOpen && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setPickerOpen(false) }}><section className="modal asset-picker"><div className="modal-title"><div><span className="eyebrow">{displayShotId(selectedShot.id)}</span><h2>绑定受管素材</h2></div><button onClick={() => setPickerOpen(false)}><X /></button></div>{availableAssets.length ? <div className="asset-picker-list">{availableAssets.map((asset) => <button key={asset.id} onClick={() => bindAsset(asset)}><AssetPreview asset={asset} compact /><span><strong>{asset.name}</strong><small>{assetMeta(asset)}</small><em>默认用途：{referenceRoleOptions[asset.media_type || 'image'].find(([value]) => value === defaultReferenceRole(asset))?.[1]}</em></span><Plus size={16} /></button>)}</div> : <div className="empty">没有可绑定的受管素材。请先到“角色资产”导入本地文件。</div>}</section></div>}
      {batchConfirmOpen && <div className="modal-backdrop"><div className="modal compact"><div className="modal-title"><div><span className="eyebrow">可恢复生产批次</span><h2>提交 {batchSelection.size} 个镜头？</h2></div><button disabled={batchBusy} onClick={() => setBatchConfirmOpen(false)}><X /></button></div><div className="confirm-summary"><p><strong>{batchSelection.size} 个镜头 · {selectedCandidateCount} 条候选</strong></p><p>平台只接受在“生成计划”中已 dry-run 且人工批准的当前版本；提交时冻结计划哈希，并按分镜顺序单 GPU 调度。</p><p>关闭网页不影响任务；API 重启后会恢复跟踪。暂停只阻止后续提交，不会伪装成已中止当前 ComfyUI 任务。</p></div><div className="modal-actions"><button className="button secondary" disabled={batchBusy} onClick={() => setBatchConfirmOpen(false)}>暂不提交</button><button className="button primary" disabled={batchBusy} onClick={submitBatch}>{batchBusy ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}确认占用 GPU</button></div></div></div>}
    </main>
  )
}

function Overview({ project, health, assets, jobs, acceptance, onOpenStoryboard, onRunAcceptance, onSignoff }: {
  project: Project
  health: Health | null
  assets: Asset[]
  jobs: Job[]
  acceptance: ProductionAcceptance | null
  onOpenStoryboard: () => void
  onRunAcceptance: () => Promise<void>
  onSignoff: (category: 'picture_continuity' | 'sound', decision: 'pass' | 'reject', note: string) => Promise<void>
}) {
  const finished = project.shots.filter((shot) => shot.status === '已定稿').length
  const exportReady = acceptance?.delivery.stages.find((stage) => stage.id === 'export')?.status === 'pass'
  const recordSignoff = async (category: 'picture_continuity' | 'sound', decision: 'pass' | 'reject') => {
    const label = category === 'picture_continuity' ? '画面与连贯性' : '声音'
    const defaultNote = decision === 'pass' ? `已完整审看并确认${label}通过` : `${label}需要修改：`
    const note = window.prompt(`请记录${label}${decision === 'pass' ? '通过依据' : '修改意见'}：`, defaultNote)
    if (note?.trim()) await onSignoff(category, decision, note.trim())
  }
  const renderTrack = (title: string, description: string, ready: boolean, stages: ProductionAcceptance['generation']['stages']) => <article className={`acceptance-track ${ready ? 'ready' : 'blocked'}`}>
    <header><span>{ready ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}</span><div><h3>{title}</h3><p>{description}</p></div><strong>{ready ? '通过' : '待处理'}</strong></header>
    <div>{stages.map((stage) => <section className={`acceptance-stage ${stage.status}`} key={stage.id}><span>{stage.status === 'pass' ? <Check size={13} /> : stage.status === 'warn' ? <AlertTriangle size={13} /> : <X size={13} />}</span><div><strong>{stage.label}</strong><small>{stage.evidence}</small>{stage.action && <em>下一步：{stage.action}</em>}</div></section>)}</div>
  </article>
  return <main className="page overview-page">
    <div className="page-heading"><div><span className="eyebrow">{project.episode} · 制作驾驶舱</span><h1>{project.title}</h1><p>{project.logline}</p></div><button className="button primary" onClick={onOpenStoryboard}>继续制作<ChevronRight size={17} /></button></div>
    <div className="metrics"><article><CircleGauge /><span>镜头进度</span><strong>{finished}/{project.shots.length}</strong><small>已通过定稿</small></article><article><Clock3 /><span>计划时长</span><strong>{project.shots.reduce((sum, shot) => sum + shot.seconds, 0).toFixed(1)}s</strong><small>目标 {project.target_duration}s</small></article><article><Boxes /><span>锁定资产</span><strong>{assets.filter(a => a.locked).length}</strong><small>共 {assets.length} 个资产</small></article><article><Sparkles /><span>生成引擎</span><strong>H3</strong><small>{health?.gpu || '等待连接 GPU'}</small></article></div>
    {acceptance && <section className="acceptance-dashboard">
      <div className="acceptance-heading"><div><span className="eyebrow">生产验收</span><h2>两条独立的就绪轨道</h2><p>“可开始生成”与“可交付成片”分别判断，警告保留历史证据缺口，但不会伪造通过记录。</p></div><div><button className="button secondary" onClick={onRunAcceptance}><ShieldCheck size={15} />冻结当前报告</button>{acceptance.latest_run && <a href="/api/acceptance/report"><Download size={14} />下载 JSON</a>}</div></div>
      <div className="acceptance-tracks">
        {renderTrack('可开始 H3 生产', '用于判断剧本、连续性规则和当前生成输入是否已冻结。', acceptance.generation.ready, acceptance.generation.stages)}
        {renderTrack('可交付横屏成片', '用于判断真实媒体、整片人工确认、装配、导出和归档证据。', acceptance.delivery.ready, acceptance.delivery.stages)}
      </div>
      <div className="acceptance-signoffs"><span>整片人工确认</span><button disabled={!exportReady} onClick={() => recordSignoff('picture_continuity', 'pass')}>确认画面通过</button><button disabled={!exportReady} onClick={() => recordSignoff('sound', 'pass')}>确认声音通过</button><small>{acceptance.latest_run ? `最近报告 ${acceptance.latest_run.report_hash.slice(0, 12)} · ${formatTimestamp(acceptance.latest_run.created_at)}` : '尚未冻结验收报告'}</small></div>
    </section>}
    <section className="overview-grid"><div><h2>镜头状态</h2>{project.shots.map(shot => <button className="overview-shot" onClick={onOpenStoryboard} key={shot.id}><ShotThumbnail shot={shot} /><span><strong>{displayShotId(shot.id)} · {shot.title}</strong><small>{shot.description}</small></span><StatusPill status={shot.status} /></button>)}</div><div><h2>最近活动</h2>{jobs.slice(0, 6).map(job => <div className="activity" key={job.id}><i /><span><strong>{job.title}</strong><small>{job.message}</small></span><time>{job.state}</time></div>)}</div></section>
  </main>
}

function AssetsPage({ assets, onRefresh, setNotice }: { assets: Asset[]; onRefresh: () => Promise<void>; setNotice: (value: string) => void }) {
  const [importOpen, setImportOpen] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [kind, setKind] = useState('角色参考')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!file) return setNotice('请选择要导入的图片、视频或音频文件')
    const form = new FormData()
    form.append('file', file)
    form.append('name', name || file.name.replace(/\.[^.]+$/, ''))
    form.append('kind', kind)
    form.append('description', description)
    setBusy(true)
    try {
      const created = await api<Asset>('/api/assets', { method: 'POST', body: form })
      await onRefresh()
      setImportOpen(false)
      setFile(null)
      setName('')
      setDescription('')
      setNotice(`已导入“${created.name}”，现在可以绑定到镜头`)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '素材导入失败')
    } finally {
      setBusy(false)
    }
  }

  return <main className="page"><div className="page-heading"><div><span className="eyebrow">一致性控制</span><h1>参考素材库</h1><p>受管素材会复制到项目目录并记录校验值；Ref2VA 按每类素材的绑定顺序解释引用标签。</p></div><button className="button secondary" onClick={() => setImportOpen(true)}><Upload size={17} />导入资产</button></div><div className="asset-list">{assets.map(asset => <article key={asset.id}><AssetPreview asset={asset} /><div><span>{asset.kind} · {asset.source === 'managed' ? '受管文件' : '演示占位'}</span><h3>{asset.name}</h3><p>{asset.description || '未填写说明'}</p><small>{asset.source === 'managed' ? assetMeta(asset) : '仅用于界面演示，不能绑定 Ref2VA'}</small></div><button className={asset.bindable ? 'locked' : ''} disabled>{asset.bindable ? <><Check size={15} />可绑定</> : '不可绑定'}</button></article>)}</div>{importOpen && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setImportOpen(false) }}><form className="modal" onSubmit={submit}><div className="modal-title"><div><span className="eyebrow">本地受管目录</span><h2>导入参考素材</h2></div><button type="button" disabled={busy} onClick={() => setImportOpen(false)}><X /></button></div><label className="file-drop"><input type="file" accept="image/jpeg,image/png,image/webp,video/mp4,video/quicktime,video/x-matroska,video/webm,audio/wav,audio/mpeg,audio/flac,audio/mp4,audio/aac,audio/ogg" onChange={(event) => { const selected = event.target.files?.[0] || null; setFile(selected); if (selected && !name) setName(selected.name.replace(/\.[^.]+$/, '')) }} /><Upload size={24} /><strong>{file?.name || '选择图片、视频或音频'}</strong><span>图片 ≤ 50 MB · 视频 ≤ 2 GB · 音频 ≤ 250 MB</span></label><div className="form-grid"><label>素材名称<input required minLength={2} maxLength={80} value={name} onChange={(event) => setName(event.target.value)} /></label><label>素材分类<select value={kind} onChange={(event) => setKind(event.target.value)}><option>角色参考</option><option>场景参考</option><option>画风参考</option><option>道具参考</option><option>动作参考</option><option>声音参考</option></select></label></div><label>备注<textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="说明身份、服装、场景、镜头语言或声音用途" /></label><div className="modal-actions"><button type="button" className="button secondary" disabled={busy} onClick={() => setImportOpen(false)}>取消</button><button className="button primary" disabled={busy || !file || name.length < 2}>{busy ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}导入并探测</button></div></form></div>}</main>
}

function QueuePage({ jobs, onSync, onResolve }: {
  jobs: Job[]
  onSync: (shotId: string) => Promise<void>
  onResolve: (job: Job, action: 'confirm_not_submitted' | 'accept_current_manifest') => Promise<void>
}) {
  const [batches, setBatches] = useState<ProductionBatch[]>([])
  const [selectedBatchId, setSelectedBatchId] = useState('')
  const [selectedBatch, setSelectedBatch] = useState<ProductionBatch | null>(null)
  const [conflicts, setConflicts] = useState<ProductionConflictGroup[]>([])
  const [conflictNotes, setConflictNotes] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')
  const [queueError, setQueueError] = useState('')

  const loadBatches = useCallback(async () => {
    try {
      const list = await api<ProductionBatch[]>('/api/production-batches')
      setBatches(list)
      setSelectedBatchId((current) => list.some((item) => item.id === current) ? current : list[0]?.id || '')
      setQueueError('')
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : '生产批次加载失败')
    }
  }, [])

  const loadDetail = useCallback(async (batchId: string) => {
    if (!batchId) { setSelectedBatch(null); return }
    try {
      const detail = await api<ProductionBatch>(`/api/production-batches/${batchId}`)
      setSelectedBatch(detail)
      setBatches((current) => current.map((item) => item.id === detail.id ? { ...item, ...detail, events: [] } : item))
      setQueueError('')
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : '生产批次详情加载失败')
    }
  }, [])

  const loadConflicts = useCallback(async () => {
    try {
      setConflicts(await api<ProductionConflictGroup[]>('/api/production-conflicts?include_resolved=true'))
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : '历史生产冲突加载失败')
    }
  }, [])

  useEffect(() => { void loadBatches() }, [loadBatches])
  useEffect(() => { void loadConflicts() }, [loadConflicts])
  useEffect(() => { void loadDetail(selectedBatchId) }, [loadDetail, selectedBatchId])
  useEffect(() => {
    const hasActive = batches.some((batch) => ['running', 'paused', 'cancelling'].includes(batch.state))
    if (!hasActive) return
    const timer = window.setInterval(async () => { await loadBatches(); if (selectedBatchId) await loadDetail(selectedBatchId) }, 2500)
    return () => window.clearInterval(timer)
  }, [batches, loadBatches, loadDetail, selectedBatchId])

  const mutateBatch = async (action: 'pause' | 'resume' | 'cancel') => {
    if (!selectedBatch) return
    setBusy(action)
    try {
      const next = await api<ProductionBatch>(`/api/production-batches/${selectedBatch.id}/${action}`, { method: 'POST' })
      setSelectedBatch(next)
      await loadBatches()
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : '批次操作失败')
    } finally { setBusy('') }
  }

  const retryItem = async (itemId: string) => {
    setBusy(itemId)
    try {
      const next = await api<ProductionBatch>(`/api/production-items/${itemId}/retry`, { method: 'POST' })
      setSelectedBatch(next)
      await loadBatches()
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : '镜头重试失败')
    } finally { setBusy('') }
  }

  const resolveConflict = async (group: ProductionConflictGroup) => {
    const note = (conflictNotes[group.shot_id] || '').trim()
    if (!note) { setQueueError('请填写本次冲突审计说明'); return }
    setBusy(`conflict-${group.shot_id}`)
    try {
      await api<ProductionConflictGroup>(`/api/production-conflicts/${group.shot_id}/resolve`, {
        method: 'POST',
        body: JSON.stringify({
          confirm: true,
          expected_revisions: group.expected_revisions,
          resolved_by: '本机操作员',
          note,
        }),
      })
      setConflictNotes((current) => ({ ...current, [group.shot_id]: '' }))
      await Promise.all([loadConflicts(), loadBatches()])
      setQueueError('')
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : '生产所有权冲突解决失败')
      await loadConflicts()
    } finally { setBusy('') }
  }

  const resolveConflictJob = async (job: Job) => {
    setBusy(`job-${job.id}`)
    try {
      await onResolve(job, 'confirm_not_submitted')
      await Promise.all([loadConflicts(), loadBatches()])
    } finally { setBusy('') }
  }

  const batchStateLabels: Record<ProductionBatch['state'], string> = {
    running: '运行中', paused: '已暂停', cancelling: '停止中', completed: '已完成',
    completed_with_errors: '有失败', cancelled: '已取消',
  }
  const itemStateLabels: Record<ProductionBatch['items'][number]['state'], string> = {
    queued: '等待提交', submitting: '提交中', running: '生成中', completed: '待审片', failed: '失败', cancelled: '已取消',
  }

  return <main className="page production-queue-page">
    <div className="page-heading"><div><span className="eyebrow">持久化单 GPU 调度</span><h1>项目队列</h1><p>只消费已批准的生成计划；批次、逐镜快照和恢复事件都保存在本地数据库。</p></div><div className="queue-safety"><ShieldCheck size={17} /><span><strong>重启可恢复</strong><small>提交结果不确定时失败关闭，绝不自动重复扣算力</small></span></div></div>
    {queueError && <div className="queue-error"><AlertTriangle size={16} />{queueError}</div>}
    {conflicts.length > 0 && <section className="ownership-conflicts" aria-label="历史生产所有权冲突">
      <header><div><AlertTriangle size={16} /><span><strong>生产所有权审计</strong><small>升级重复尝试或运行期 attempt/job 冲突不会自动选定所有者；精确证明未提交后才解除镜头门禁。</small></span></div><button onClick={() => void loadConflicts()}><RefreshCw size={14} />刷新证据</button></header>
      <div>{conflicts.map((group) => <article className={group.state} key={group.shot_id}>
        <div className="conflict-summary"><span><b>{displayShotId(group.shot_id)}</b><strong>{group.shot_title}</strong></span><em>{group.state === 'resolved' ? '已解决' : group.can_resolve ? '证据完整' : '证据不足'}</em></div>
        <div className="conflict-attempts">{group.items.map((item) => {
          const target = item.evidence?.target_attempt
          const targetJob = target?.draft_job_id ? jobs.find((job) => job.id === target.draft_job_id) : undefined
          return <span className={item.proof.verified ? 'verified' : 'blocked'} key={item.id}><b>{item.item_title}</b><small>{item.original_state} · R{item.revision}{target?.attempt ? ` · attempt ${target.attempt}` : ''}{target?.draft_job_id ? ` · job #${target.draft_job_id}` : ''}{targetJob ? ` / R${targetJob.reconciliation_revision || 0}` : target?.item_job_revision !== undefined ? ` / R${target.item_job_revision}` : ''}</small><em>{item.proof.verified ? (item.proof.basis === 'queued_never_claimed' ? '从未领取' : '已证明零提交') : item.proof.reason}</em>{group.state === 'unresolved' && targetJob?.state === '待人工对账' && <button type="button" className="danger" disabled={busy === `job-${targetJob.id}`} onClick={() => void resolveConflictJob(targetJob)}>{busy === `job-${targetJob.id}` ? '正在写入审计…' : `确认 job #${targetJob.id} 未提交`}</button>}</span>
        })}</div>
        {group.state === 'unresolved' ? <div className="conflict-resolution"><label>审计说明<input aria-label={`${group.shot_title}冲突审计说明`} value={conflictNotes[group.shot_id] || ''} onChange={(event) => setConflictNotes((current) => ({ ...current, [group.shot_id]: event.target.value }))} placeholder="说明如何确认整组尝试均未提交" /></label><button disabled={!group.can_resolve || !conflictNotes[group.shot_id]?.trim() || Boolean(busy)} onClick={() => void resolveConflict(group)}>{busy === `conflict-${group.shot_id}` ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}确认整组未提交并解除门禁</button></div> : <footer>{group.items[0]?.resolved_by} · {group.items[0]?.resolution_note} · {group.items[0]?.resolved_at ? new Date(group.items[0].resolved_at).toLocaleString('zh-CN', { hour12: false }) : ''}</footer>}
      </article>)}</div>
    </section>}
    <section className="production-queue-grid">
      <aside className="production-batches">
        <header><span>生产批次</span><button onClick={() => void loadBatches()}><RefreshCw size={14} /></button></header>
        {batches.map((batch) => <button className={batch.id === selectedBatchId ? 'selected' : ''} onClick={() => setSelectedBatchId(batch.id)} key={batch.id}>
          <span className={`production-state ${batch.state}`} /><span><strong>{batch.name}</strong><small>{batch.completed_count}/{batch.item_count} 完成 · {batch.config.candidate_total} 条候选</small></span><em>{batchStateLabels[batch.state]}</em>
        </button>)}
        {!batches.length && <div className="production-empty"><ListVideo size={22} /><strong>暂无生产批次</strong><span>先到“生成计划”完成 dry-run 与批准，再从分镜选择镜头提交。</span></div>}
      </aside>
      <section className="production-batch-detail">
        {selectedBatch ? <>
          <header className="production-batch-head"><div><span>{selectedBatch.id}</span><h2>{selectedBatch.name}</h2><p>{selectedBatch.message}</p></div><div className="production-batch-actions">
            {selectedBatch.state === 'running' && <button disabled={Boolean(busy)} onClick={() => void mutateBatch('pause')}><Pause size={14} />暂停后续提交</button>}
            {selectedBatch.state === 'paused' && <button disabled={Boolean(busy)} onClick={() => void mutateBatch('resume')}><Play size={14} />继续调度</button>}
            {!['completed', 'completed_with_errors', 'cancelled'].includes(selectedBatch.state) && <button className="danger" disabled={Boolean(busy)} onClick={() => void mutateBatch('cancel')}><X size={14} />停止批次</button>}
          </div></header>
          <div className="production-progress"><div style={{ width: `${selectedBatch.item_count ? selectedBatch.completed_count / selectedBatch.item_count * 100 : 0}%` }} /><span>{selectedBatch.completed_count} 完成</span><span>{selectedBatch.failed_count} 失败</span><span>{selectedBatch.cancelled_count} 取消</span><strong>{selectedBatch.item_count} 镜头</strong></div>
          <div className="production-items-head"><span>顺序</span><span>镜头与冻结计划</span><span>尝试</span><span>状态</span><span>操作</span></div>
          <div className="production-items">{selectedBatch.items.map((item) => <article key={item.id}>
            <strong>{String(item.ordinal).padStart(2, '0')}</strong><span><b>{item.title}</b><small>{item.plan_snapshot.mode} · {item.plan_snapshot.spec?.resolution} · {item.plan_hash.slice(0, 12)}</small><em>{item.message}</em>{item.error && <code>{item.error}</code>}</span><span>{item.attempts}/{item.max_attempts}</span><i className={item.state}>{itemStateLabels[item.state]}</i><span className="production-item-actions">{item.state === 'running' && <button onClick={() => onSync(item.shot_id)}><RefreshCw size={13} />同步</button>}{item.state === 'failed' && <button disabled={busy === item.id} onClick={() => void retryItem(item.id)}>{busy === item.id ? <LoaderCircle className="spin" size={13} /> : <RefreshCw size={13} />}重试</button>}</span>
          </article>)}</div>
          <section className="production-events"><h3>恢复与调度记录</h3>{selectedBatch.events.slice(0, 12).map((event) => <div className={event.level} key={event.id}><time>{new Date(event.created_at).toLocaleString('zh-CN', { hour12: false })}</time><strong>{event.event}</strong><span>{event.message}</span></div>)}</section>
        </> : <div className="production-detail-empty"><ListVideo size={26} /><strong>选择一个生产批次</strong><span>查看逐镜提交、生成、恢复和失败重试状态。</span></div>}
      </section>
    </section>
    <details className="legacy-job-log"><summary>底层 H3 任务日志 <span>{jobs.length}</span></summary><div className="queue-table"><div className="queue-head"><span>镜头</span><span>任务</span><span>说明</span><span>状态</span><span>时间</span><span>操作</span></div>{jobs.map(job => <div className="queue-row" key={job.id}><strong>{displayShotId(job.shot_id)}</strong><span>{job.kind}<small>job #{job.id} · R{job.reconciliation_revision || 0}</small></span><span><b>{job.message}</b>{job.prompt_ids?.length ? <small>{job.prompt_ids.length} 个 prompt_id</small> : null}</span><StatusPill status={job.state} /><time>{new Date(job.updated_at || job.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time><span className="queue-reconciliation-actions">{job.state === '待人工对账' ? <><button onClick={() => void onResolve(job, 'accept_current_manifest')}><RefreshCw size={14} />接受 job #{job.id} 证据</button><button className="danger" onClick={() => void onResolve(job, 'confirm_not_submitted')}>确认 job #{job.id} 未提交</button></> : <button className="queue-sync" disabled={!job.h3_project || job.kind !== 'draft'} onClick={() => onSync(job.shot_id)}><RefreshCw size={14} />同步</button>}</span></div>)}</div></details>
  </main>
}

const reviewScoreLabels = {
  story_match: '剧情符合', continuity: '跨镜连续', action: '动作表演', visual_quality: '画面质量', audio_quality: '声音质量',
}
const reviewIssueOptions = [
  ['identity_drift', '人物漂移'], ['costume_drift', '服装漂移'], ['location_drift', '场景漂移'],
  ['action_error', '动作错误'], ['camera_error', '运镜错误'], ['artifact', '画面瑕疵'],
  ['pseudo_text', '伪文字'], ['dialogue_error', '对白错误'], ['audio_noise', '声音噪声'],
  ['audio_missing', '缺少音轨'], ['timing_error', '节奏/时长'], ['other', '其他'],
] as const

function ReviewPage({ project, shot, candidates, promotions, reviewWorkspace, onSelectShot, onRefresh, setNotice }: {
  project: Project
  shot: Shot
  candidates: Candidate[]
  promotions: Promotion[]
  reviewWorkspace: ReviewWorkspace | null
  onSelectShot: (shotId: string) => void
  onRefresh: () => Promise<void>
  setNotice: (value: string) => void
}) {
  const [stage, setStage] = useState<'drafts' | 'promotions'>(promotions.length ? 'promotions' : 'drafts')
  const [selectedCandidate, setSelectedCandidate] = useState(candidates.find(c => c.selected)?.id || candidates[0]?.id)
  const [compareCandidate, setCompareCandidate] = useState(candidates.find(c => c.id !== (candidates.find(item => item.selected)?.id || candidates[0]?.id))?.id)
  const [selectedPromotion, setSelectedPromotion] = useState(promotions.find(p => p.selected)?.id || promotions[0]?.id)
  const [playing, setPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [note, setNote] = useState('')
  const [extractingFrame, setExtractingFrame] = useState(false)
  const [savingReview, setSavingReview] = useState(false)
  const [maxWatched, setMaxWatched] = useState(0)
  const [reviewDecision, setReviewDecision] = useState<'pass' | 'needs_changes' | 'reject'>('needs_changes')
  const [reviewScores, setReviewScores] = useState<Record<keyof typeof reviewScoreLabels, number>>({ story_match: 3, continuity: 3, action: 3, visual_quality: 3, audio_quality: 3 })
  const [reviewIssues, setReviewIssues] = useState<Set<string>>(new Set())
  const [audioChecks, setAudioChecks] = useState<{
    dialogue_match: 'pending' | 'pass' | 'fail' | 'not_applicable'
    lip_sync: 'pending' | 'pass' | 'fail' | 'not_applicable'
    ambience: 'pending' | 'pass' | 'fail'
  }>({ dialogue_match: shot.dialogue ? 'pending' : 'not_applicable', lip_sync: shot.dialogue ? 'pending' : 'not_applicable', ambience: 'pending' })
  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    setSelectedCandidate(candidates.find(c => c.selected)?.id || candidates[0]?.id)
    setCompareCandidate((current) => candidates.some((candidate) => candidate.id === current && candidate.id !== selectedCandidate)
      ? current
      : candidates.find((candidate) => candidate.id !== (candidates.find(item => item.selected)?.id || candidates[0]?.id))?.id)
  }, [candidates])
  useEffect(() => {
    setSelectedPromotion(promotions.find(p => p.selected)?.id || promotions[0]?.id)
    if (!promotions.length && stage === 'promotions') setStage('drafts')
  }, [promotions, stage])

  const chosenCandidate = candidates.find(c => c.id === selectedCandidate) || candidates[0]
  const comparisonCandidate = candidates.find(c => c.id === compareCandidate) || candidates.find(c => c.id !== chosenCandidate?.id)
  const chosenReview = reviewWorkspace?.reviews.find((review) => review.candidate_id === chosenCandidate?.id)
  const chosenTrace = reviewWorkspace?.comparison.find((item) => item.candidate_id === chosenCandidate?.id)?.trace
  const chosenPromotion = promotions.find(p => p.id === selectedPromotion) || promotions[0]
  const chosen = stage === 'drafts' ? chosenCandidate : chosenPromotion
  const video = chosen?.video
  const duration = stage === 'drafts'
    ? Number(chosenCandidate?.metadata?.actual_seconds || shot.seconds)
    : Number(chosenPromotion?.actual_seconds || shot.seconds)

  useEffect(() => {
    const review = stage === 'drafts' ? reviewWorkspace?.reviews.find((item) => item.candidate_id === chosen?.id) : undefined
    setNote(stage === 'drafts' ? review?.note || '' : chosen?.note || '')
    setCurrentTime(0)
    setPlaying(false)
    setMaxWatched(review?.watched_seconds || 0)
    setReviewDecision(review?.decision || 'needs_changes')
    setReviewScores({
      story_match: review?.scores.story_match || 3,
      continuity: review?.scores.continuity || 3,
      action: review?.scores.action || 3,
      visual_quality: review?.scores.visual_quality || 3,
      audio_quality: review?.scores.audio_quality || 3,
    })
    setReviewIssues(new Set(review?.issues || []))
    setAudioChecks({
      dialogue_match: review?.audio_checks.dialogue_match || (shot.dialogue ? 'pending' : 'not_applicable'),
      lip_sync: review?.audio_checks.lip_sync || (shot.dialogue ? 'pending' : 'not_applicable'),
      ambience: review?.audio_checks.ambience || 'pending',
    })
  }, [chosen?.id, chosen?.note, stage, reviewWorkspace, shot.dialogue])

  const toggle = () => { if (!videoRef.current) return; if (videoRef.current.paused) videoRef.current.play(); else videoRef.current.pause(); setPlaying(!videoRef.current.paused) }
  const finalizeDraft = async () => {
    if (!chosenCandidate) return
    await api(`/api/shots/${shot.id}/review`, { method: 'POST', body: JSON.stringify({ candidate_id: chosenCandidate.id, note, base_revision: reviewWorkspace?.master_versions[0]?.revision || 0 }) })
    setNotice(`候选 ${chosenCandidate.label} 已选为草稿母版`)
    await onRefresh()
  }
  const rollbackMaster = async (targetRevision: number) => {
    const currentRevision = reviewWorkspace?.master_versions[0]?.revision
    if (!currentRevision || !window.confirm(`确认把草稿母版回滚到 R${targetRevision}？候选与历史不会被删除。`)) return
    try {
      await api(`/api/shots/${shot.id}/candidate-master/rollback`, {
        method: 'POST', body: JSON.stringify({ target_revision: targetRevision, base_revision: currentRevision, note: `人工回滚到 R${targetRevision}`, confirm: true }),
      })
      setNotice(`已追加母版回滚修订；当前来源为历史 R${targetRevision}`)
      await onRefresh()
    } catch (error) { setNotice(error instanceof Error ? error.message : '母版回滚失败') }
  }
  const saveReview = async () => {
    if (!chosenCandidate) return
    setSavingReview(true)
    try {
      const saved = await api<CandidateReview>(`/api/shots/${shot.id}/candidate-reviews`, {
        method: 'POST',
        body: JSON.stringify({
          candidate_id: chosenCandidate.id,
          decision: reviewDecision,
          scores: reviewScores,
          audio_checks: audioChecks,
          issues: [...reviewIssues],
          note,
          watched_seconds: maxWatched,
        }),
      })
      setNotice(saved.can_select ? '审片已通过，可以选择为草稿母版' : reviewDecision === 'reject' ? '已记录拒绝结论与问题证据' : '已记录修改方向，可返回生成计划调整')
      await onRefresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '审片结论保存失败')
    } finally { setSavingReview(false) }
  }
  const extractFrame = async () => {
    if (!chosenCandidate?.video || currentTime <= 0) return
    const frameSeconds = Math.max(0, Math.min(currentTime, duration - 1 / 24))
    setExtractingFrame(true)
    try {
      const asset = await api<Asset>(`/api/candidates/${chosenCandidate.id}/extract-frame`, {
        method: 'POST',
        body: JSON.stringify({
          seconds: frameSeconds,
          name: `${shot.title} · 人物连续性 ${frameSeconds.toFixed(2)}s`,
          kind: '角色参考',
          description: `从 ${shot.id} 草稿候选 ${chosenCandidate.label} 的 ${frameSeconds.toFixed(2)} 秒提取；用于后续镜头的人物与服装连续性。`,
        }),
      })
      setNotice(`已提取“${asset.name}”并加入素材库`)
      await onRefresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '当前帧提取失败')
    } finally {
      setExtractingFrame(false)
    }
  }
  const finalizePromotion = async () => {
    if (!chosenPromotion) return
    await api(`/api/shots/${shot.id}/finalize-promotion`, {
      method: 'POST', body: JSON.stringify({ promotion_id: chosenPromotion.id, note }),
    })
    setNotice(`${promotionName(chosenPromotion)} 已选为最终成片`)
    await onRefresh()
    setSelectedPromotion(chosenPromotion.id)
  }
  const timeLabel = (seconds: number) => `00:${seconds.toFixed(2).padStart(5, '0')}`
  return (
    <main className="review-layout">
      <section className="review-stage">
        <div className="page-heading review-page-heading">
          <div><span className="eyebrow">候选对比与质量门禁</span><h1>审片台</h1><p>{displayShotId(shot.id)} · {shot.title}</p></div>
          <div className="review-tabs" aria-label="审片阶段">
            <button className={stage === 'drafts' ? 'active' : ''} onClick={() => setStage('drafts')}>草稿候选 <span>{candidates.length}</span></button>
            <button className={stage === 'promotions' ? 'active' : ''} disabled={!promotions.length} onClick={() => setStage('promotions')}>成片版本 <span>{promotions.length}</span></button>
          </div>
        </div>
        <nav className="review-shot-nav" aria-label="切换审片镜头">{project.shots.map((item) => <button className={item.id === shot.id ? 'active' : ''} onClick={() => onSelectShot(item.id)} key={item.id}><span>{String(item.ordinal).padStart(2, '0')}</span><strong>{item.title}</strong><small>{item.status}</small></button>)}</nav>
        {stage === 'drafts' && reviewWorkspace?.selected_debt.active && <div className="review-debt"><strong>历史证据债务</strong><span>{reviewWorkspace.selected_debt.message}</span></div>}
        <div className="video-frame">
          {video ? (
            <video key={`${stage}-${chosen?.id}`} ref={videoRef} src={video} poster={stage === 'drafts' ? chosenCandidate?.thumbnail : undefined} preload="auto" onTimeUpdate={(event) => { setCurrentTime(event.currentTarget.currentTime); setMaxWatched((value) => Math.max(value, event.currentTarget.currentTime)) }} onEnded={(event) => { setPlaying(false); setMaxWatched((value) => Math.max(value, event.currentTarget.duration || 0)) }} />
          ) : (
            <div className="generation-placeholder"><LoaderCircle className={chosen?.status === 'running' ? 'spin' : ''} /><strong>{chosen ? '真实视频正在生成' : stage === 'drafts' ? '尚无真实候选' : '尚无成片版本'}</strong><span>{chosen?.prompt_id ? `Prompt ${chosen.prompt_id}` : '请先完成 H3 草稿与定稿提升'}</span></div>
          )}
        </div>
        <div className="video-controls"><button disabled={!video} aria-label={playing ? '暂停' : '播放'} onClick={toggle}>{playing ? <Pause /> : <Play />}</button><span>{timeLabel(currentTime)} / {timeLabel(duration)}</span><div><i style={{ width: `${duration ? Math.min(100, currentTime / duration * 100) : 0}%` }} /></div><Volume2 size={18} /></div>
        {stage === 'drafts' && candidates.length >= 2 && chosenCandidate && comparisonCandidate && <section className="candidate-compare"><header><span><strong>双候选对比</strong><small>同屏观察构图；声音请依次播放，避免混音误判。</small></span><label>对照候选<select value={comparisonCandidate.id} onChange={(event) => setCompareCandidate(event.target.value)}>{candidates.filter(candidate => candidate.id !== chosenCandidate.id).map(candidate => <option value={candidate.id} key={candidate.id}>{candidate.label}</option>)}</select></label></header><div><article><video src={chosenCandidate.video} poster={chosenCandidate.thumbnail} controls preload="metadata" /><strong>{chosenCandidate.label} · 当前检查</strong></article><article><video src={comparisonCandidate.video} poster={comparisonCandidate.thumbnail} controls preload="metadata" /><strong>{comparisonCandidate.label} · 对照</strong></article></div></section>}
        {stage === 'drafts' ? (
          <div className="filmstrip candidate-strip">{candidates.map(candidate => { const review = reviewWorkspace?.reviews.find((item) => item.candidate_id === candidate.id); return <button className={candidate.id === selectedCandidate ? 'active' : ''} key={candidate.id} onClick={() => setSelectedCandidate(candidate.id)}><img src={candidate.thumbnail} /><span>{candidate.label} · {review?.can_select ? '审片通过' : review?.decision === 'reject' ? '已拒绝' : review?.decision === 'needs_changes' ? '需修改' : candidate.status === 'completed' ? '待审片' : '生成中'}</span></button> })}</div>
        ) : (
          <div className="filmstrip promotion-strip">{promotions.map(promotion => <button className={promotion.id === selectedPromotion ? 'active' : ''} key={promotion.id} onClick={() => setSelectedPromotion(promotion.id)}><video src={promotion.video} muted preload="auto" /><span>{promotionName(promotion)}{promotion.selected ? ' · 最终' : ''}</span></button>)}</div>
        )}
      </section>

      <aside className="review-inspector">
        <div className="review-heading"><h2>{stage === 'drafts' ? '草稿候选' : '成片版本'}</h2><span className={`source-badge ${stage === 'drafts' ? chosenCandidate?.source || '' : 'final'}`}>{stage === 'drafts' ? chosenCandidate?.source === 'h3' ? '真实 H3' : '演示数据' : 'H3 成片'}</span></div>
        {stage === 'drafts' ? (
          <>
            <div className="candidate-list">{candidates.length ? candidates.map(candidate => { const review = reviewWorkspace?.reviews.find((item) => item.candidate_id === candidate.id); return <button className={candidate.id === selectedCandidate ? 'active' : ''} key={candidate.id} onClick={() => setSelectedCandidate(candidate.id)}><strong>{candidate.label}</strong><img src={candidate.thumbnail} /><span>Seed: {candidate.seed}<small>{review?.can_select ? '门禁通过' : review?.stale ? '需重新审片' : review?.decision === 'reject' ? '已拒绝' : review?.decision === 'needs_changes' ? '保留修改' : candidate.status === 'completed' ? '待审片' : candidate.status}</small></span>{candidate.id === selectedCandidate && <Check />}</button> }) : <div className="empty">这个镜头还没有候选版本。</div>}</div>
            {chosenCandidate && <section className="structured-review">
              <header><span><strong>结构化审片</strong><small>版本 {chosenReview?.revision || 0} · 已观看 {maxWatched.toFixed(2)} / {duration.toFixed(2)} 秒</small></span>{chosenReview?.stale && <em>文件变化，结论已失效</em>}</header>
              <div className="review-score-grid">{Object.entries(reviewScoreLabels).map(([key, label]) => <label key={key}><span>{label}</span><select value={reviewScores[key as keyof typeof reviewScoreLabels]} onChange={(event) => setReviewScores({ ...reviewScores, [key]: Number(event.target.value) })}>{[1, 2, 3, 4, 5].map((score) => <option value={score} key={score}>{score} 分</option>)}</select></label>)}</div>
              <div className="review-audio-checks"><label><span>对白内容{shot.dialogue ? '' : '（无对白）'}</span><select value={audioChecks.dialogue_match} onChange={(event) => setAudioChecks({ ...audioChecks, dialogue_match: event.target.value as typeof audioChecks.dialogue_match })}><option value="pending">待人工确认</option><option value="pass">内容正确</option><option value="fail">内容错误</option><option value="not_applicable">不适用</option></select></label><label><span>口型 / 时序</span><select value={audioChecks.lip_sync} onChange={(event) => setAudioChecks({ ...audioChecks, lip_sync: event.target.value as typeof audioChecks.lip_sync })}><option value="pending">待人工确认</option><option value="pass">同步可信</option><option value="fail">不同步</option><option value="not_applicable">不适用</option></select></label><label><span>环境声连续性</span><select value={audioChecks.ambience} onChange={(event) => setAudioChecks({ ...audioChecks, ambience: event.target.value as typeof audioChecks.ambience })}><option value="pending">待人工确认</option><option value="pass">连续可用</option><option value="fail">不连续 / 不合适</option></select></label></div>
              <div className="review-decisions"><button className={reviewDecision === 'pass' ? 'pass active' : 'pass'} onClick={() => setReviewDecision('pass')}><CheckCircle2 size={14} />通过</button><button className={reviewDecision === 'needs_changes' ? 'changes active' : 'changes'} onClick={() => setReviewDecision('needs_changes')}><RefreshCw size={14} />保留修改</button><button className={reviewDecision === 'reject' ? 'reject active' : 'reject'} onClick={() => setReviewDecision('reject')}><X size={14} />拒绝</button></div>
              <div className="review-issues">{reviewIssueOptions.map(([code, label]) => <button className={reviewIssues.has(code) ? 'active' : ''} onClick={() => setReviewIssues((current) => { const next = new Set(current); if (next.has(code)) next.delete(code); else next.add(code); return next })} key={code}>{reviewIssues.has(code) && <Check size={11} />}{label}</button>)}</div>
              {chosenReview?.media_probe && <div className={`review-media-qc ${chosenReview.media_probe.ok ? 'passed' : 'failed'}`}><ShieldCheck size={15} /><span><strong>{chosenReview.media_probe.ok ? '媒体与声音技术 QC 通过' : '媒体与声音技术 QC 未通过'}</strong><small>{chosenReview.media_probe.video?.width}×{chosenReview.media_probe.video?.height} · {chosenReview.media_probe.duration_seconds?.toFixed(3)} 秒 · {chosenReview.media_probe.video?.codec || '未知视频'} · {chosenReview.media_probe.audio?.present ? `${chosenReview.media_probe.audio.codec || '音频'} / ${chosenReview.media_probe.audio.channels || '?'} 声道` : '无音轨'}</small>{chosenReview.media_probe.audio_analysis && <small>平均 {chosenReview.media_probe.audio_analysis.mean_volume_db?.toFixed(1) ?? '—'} dB · 峰值 {chosenReview.media_probe.audio_analysis.max_volume_db?.toFixed(1) ?? '—'} dB · 长静音 {(chosenReview.media_probe.audio_analysis.silence_ratio * 100).toFixed(1)}%</small>}</span></div>}
              {(chosenReview?.history?.length || 0) > 1 && <details className="review-revisions"><summary>查看 {chosenReview?.history?.length} 次审片修订</summary>{chosenReview?.history?.map(item => <span key={`${item.candidate_id}-${item.revision}`}>R{item.revision} · {item.decision === 'pass' ? '通过' : item.decision === 'reject' ? '拒绝' : '保留修改'} · {item.note || '无备注'} · {formatTimestamp(item.created_at)}</span>)}</details>}
            </section>}
            {chosenCandidate && <section className={`candidate-trace ${chosenTrace?.evidence_status === 'verified' ? 'verified' : 'debt'}`}><header><strong>{chosenTrace?.evidence_status === 'verified' ? '真实媒体证据' : '历史证据债务'}</strong><span>{chosenTrace?.plan_hash ? `计划 ${chosenTrace.plan_hash.slice(0, 12)}` : '无计划哈希'}</span></header><dl><div><dt>Seed</dt><dd>{chosenTrace?.seed ?? chosenCandidate.seed}</dd></div><div><dt>Prompt / Comfy</dt><dd>{chosenTrace?.prompt_id || '缺失'}</dd></div><div><dt>规格</dt><dd>{chosenTrace?.spec.width || '—'}×{chosenTrace?.spec.height || '—'} · {chosenTrace?.spec.actual_seconds || '—'} 秒</dd></div><div><dt>媒体</dt><dd>{chosenTrace?.media.file_exists ? `${chosenTrace.media.size_bytes || 0} bytes` : chosenTrace?.debt_reason || '文件证据缺失'}</dd></div></dl></section>}
            <div className="frame-extract"><span><ImagePlus size={16} /><strong>从视频反哺素材库</strong><small>{currentTime > 0 ? `当前帧 ${currentTime.toFixed(2)} 秒` : '播放并暂停在人物清晰的画面'}</small></span><button className="button secondary" disabled={!video || currentTime <= 0 || extractingFrame} onClick={extractFrame}>{extractingFrame ? <LoaderCircle className="spin" size={15} /> : <ImagePlus size={15} />}提取当前帧</button></div>
            <label>审片结论与修改方向<textarea rows={4} value={note} onChange={(event) => setNote(event.target.value)} placeholder={reviewDecision === 'pass' ? '可选：记录可复用的优点或后续精修注意事项' : '必填：写明下一轮需要保留和修正的内容'} /></label>
            <div className="review-actions"><button className="button secondary" disabled={!chosenCandidate || chosenCandidate.status !== 'completed' || savingReview} onClick={saveReview}>{savingReview ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}保存审片结论</button><button className="button primary" disabled={!chosenCandidate || Boolean(chosenCandidate.selected) || chosenCandidate.status !== 'completed' || !chosenReview?.can_select || (reviewWorkspace?.summary.comparable_count || 0) < 2} onClick={finalizeDraft}>{chosenCandidate?.selected ? '当前草稿母版' : (reviewWorkspace?.summary.comparable_count || 0) < 2 ? '至少需要 2 条真实候选' : chosenReview?.can_select ? '选为草稿母版' : '先完成通过审片'}</button></div>
            {reviewWorkspace?.master_versions.length ? <details className="master-history"><summary>草稿母版历史 · {reviewWorkspace.master_versions.length} 个不可变修订</summary>{reviewWorkspace.master_versions.map(version => <article key={version.id}><span><strong>R{version.revision} · 候选 {candidates.find(candidate => candidate.id === version.candidate_id)?.label || version.candidate_id}</strong><small>{version.action === 'rollback' ? `回滚自 R${version.rollback_of_revision}` : '人工选择'} · 审片 R{version.review_revision} · {formatTimestamp(version.created_at)}</small></span>{version.current ? <em>当前</em> : <button disabled={!reviewWorkspace.master_versions[0]?.revision} onClick={() => rollbackMaster(version.revision)}>回滚到此版</button>}</article>)}</details> : null}
          </>
        ) : (
          <>
            <div className="promotion-list">{promotions.length ? promotions.map(promotion => <button className={promotion.id === selectedPromotion ? 'active' : ''} key={promotion.id} onClick={() => setSelectedPromotion(promotion.id)}><span className="version-kind">{promotion.strategy === 'ref2va' ? 'R' : 'S'}</span><video src={promotion.video} muted preload="auto" /><span><strong>{promotionName(promotion)}</strong><small>{promotion.width}×{promotion.height} · {(promotion.actual_seconds || 0).toFixed(3)} 秒</small></span>{promotion.selected ? <span className="final-flag"><Check />最终</span> : <ChevronRight />}</button>) : <div className="empty">这个镜头还没有成片版本。</div>}</div>
            {chosenPromotion && <div className="version-summary"><div><span>来源草稿</span><strong>{chosenPromotion.source_candidate || '—'}</strong></div><div><span>处理耗时</span><strong>{formatElapsed(chosenPromotion.elapsed_seconds)}</strong></div><div><span>内容稳定性</span><strong>{chosenPromotion.strategy === 'scale' ? '完全保留' : '重新生成'}</strong></div><p>{chosenPromotion.strategy === 'scale' ? '确定性放大：动作、构图、时长和原音保持不变，但不会产生新细节。' : 'Ref2VA 精修：以草稿视频为画面与声音参考重新扩散，细节更丰富，但存在轻微漂移。'}</p></div>}
            <label>成片备注<textarea rows={4} value={note} onChange={(event) => setNote(event.target.value)} /></label>
            <div className="review-actions"><button className="button secondary" onClick={() => setNotice('所有成片版本均已保留，可随时切换最终版')}>保留全部版本</button><button className="button primary" disabled={!chosenPromotion || chosenPromotion.status !== 'completed' || Boolean(chosenPromotion.selected)} onClick={finalizePromotion}>{chosenPromotion?.selected ? '当前最终成片' : chosenPromotion?.strategy === 'scale' ? '回退为最终成片' : '设为最终成片'}</button></div>
          </>
        )}
      </aside>
    </main>
  )
}

function promotionName(promotion: Promotion) {
  return promotion.strategy === 'ref2va' ? 'Ref2VA 精修版' : '保真放大版'
}

function formatElapsed(seconds?: number) {
  if (seconds === undefined || seconds === null) return '—'
  if (seconds < 60) return `${seconds.toFixed(0)} 秒`
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`
}

function formatTimestamp(value?: string) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(value))
}

function TimelinePage({ shots, roughCut, preflight, exportRuns, deliveryWorkspace, onExport, onRunAction, onRefresh, setNotice }: {
  shots: Shot[]
  roughCut: RoughCut
  preflight: ExportPreflight | null
  exportRuns: ExportRun[]
  deliveryWorkspace: DeliveryWorkspace | null
  onExport: () => Promise<void>
  onRunAction: (runId: string, action: 'cancel' | 'retry' | 'activate') => Promise<void>
  onRefresh: () => Promise<void>
  setNotice: (message: string) => void
}) {
  const [playing, setPlaying] = useState(false)
  const [expandedRunId, setExpandedRunId] = useState<string | null>(exportRuns[0]?.id || null)
  const [assemblyItems, setAssemblyItems] = useState<DeliveryPlanItem[]>([])
  const [savingAssembly, setSavingAssembly] = useState(false)
  useEffect(() => {
    setAssemblyItems(deliveryWorkspace?.plan.items || [])
  }, [deliveryWorkspace?.plan.id, deliveryWorkspace?.plan.revision, deliveryWorkspace?.plan.plan_hash])
  const orderedShots = useMemo(() => {
    if (!assemblyItems.length) return shots
    const byId = new Map(shots.map((shot) => [shot.id, shot]))
    return assemblyItems.map((item) => byId.get(item.shot_id)).filter((shot): shot is Shot => Boolean(shot))
  }, [assemblyItems, shots])
  const assemblyDirty = useMemo(() => {
    const saved = deliveryWorkspace?.plan.items || []
    const compact = (items: DeliveryPlanItem[]) => items.map((item, index) => ({
      shot_id: item.shot_id,
      ordinal: index + 1,
      subtitle_enabled: item.subtitle_enabled,
      subtitle_start_seconds: item.subtitle_start_seconds || 0,
      transition: 'cut',
      in_point_seconds: item.in_point_seconds || 0,
      out_point_seconds: item.out_point_seconds || item.seconds,
      dialogue_mode: item.dialogue_mode || 'original',
    }))
    return JSON.stringify(compact(assemblyItems)) !== JSON.stringify(compact(saved))
  }, [assemblyItems, deliveryWorkspace])
  const assemblySourcesReady = assemblyItems.length > 0 && assemblyItems.every(
    (item) => item.source_snapshot?.source_status === 'ready' && Boolean(item.source_snapshot?.media?.checksum_sha256),
  )
  const updateAssemblyItem = (shotId: string, patch: Partial<DeliveryPlanItem>) => {
    setAssemblyItems((items) => items.map((item) => item.shot_id === shotId ? { ...item, ...patch } : item))
  }
  const moveAssemblyItem = (index: number, direction: -1 | 1) => {
    setAssemblyItems((items) => {
      const nextIndex = index + direction
      if (nextIndex < 0 || nextIndex >= items.length) return items
      const next = [...items]
      const [item] = next.splice(index, 1)
      next.splice(nextIndex, 0, item)
      return next.map((entry, itemIndex) => ({ ...entry, ordinal: itemIndex + 1 }))
    })
  }
  const saveAssembly = async () => {
    if (!deliveryWorkspace) throw new Error('装配计划尚未载入')
    return api<DeliveryWorkspace>('/api/delivery-plan', {
      method: 'PUT',
      body: JSON.stringify({
        base_revision: deliveryWorkspace.plan.revision,
        items: assemblyItems.map((item) => ({
          shot_id: item.shot_id,
          subtitle_enabled: Boolean(item.dialogue && item.subtitle_enabled),
          subtitle_start_seconds: item.dialogue && item.subtitle_enabled ? Number(item.subtitle_start_seconds || 0) : null,
          transition: 'cut',
          in_point_seconds: Number(item.in_point_seconds || 0),
          out_point_seconds: Number(item.out_point_seconds || item.seconds),
          dialogue_mode: item.dialogue_mode || 'original',
        })),
      }),
    })
  }
  const persistAssembly = async () => {
    setSavingAssembly(true)
    try {
      await saveAssembly()
      await onRefresh()
      setNotice('装配草稿已保存，并记录为一个可追溯修订')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '装配计划保存失败')
    } finally {
      setSavingAssembly(false)
    }
  }
  const lockAssembly = async () => {
    if (!deliveryWorkspace) return
    setSavingAssembly(true)
    try {
      let current = deliveryWorkspace
      if (deliveryWorkspace.plan.revision === 0 || assemblyDirty) current = await saveAssembly()
      await api<DeliveryWorkspace>('/api/delivery-plan/lock', {
        method: 'POST', body: JSON.stringify({ base_revision: current.plan.revision }),
      })
      await onRefresh()
      setNotice('装配计划已锁定；导出将冻结当前顺序、字幕设置和计划哈希')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '装配计划锁定失败')
    } finally {
      setSavingAssembly(false)
    }
  }
  const reopenAssembly = async () => {
    if (!deliveryWorkspace) return
    setSavingAssembly(true)
    try {
      await api<DeliveryWorkspace>('/api/delivery-plan/reopen', {
        method: 'POST', body: JSON.stringify({ base_revision: deliveryWorkspace.plan.revision }),
      })
      await onRefresh()
      setNotice('已从锁定版本开启新修订；上一版仍保留在历史中')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '新修订创建失败')
    } finally {
      setSavingAssembly(false)
    }
  }
  const activeRun = exportRuns.find((run) => activeExportStates.has(run.state))
  const latestRun = activeRun || exportRuns[0]
  const exporting = Boolean(activeRun)
  const runAction = async (run: ExportRun, action: 'cancel' | 'retry' | 'activate') => {
    if (action === 'cancel' && !window.confirm('取消后会终止当前合成并清理未完成产物，确定继续吗？')) return
    await onRunAction(run.id, action)
  }
  return <main className={`page timeline-page ${playing ? 'is-playing' : ''}`}>
    <div className="page-heading">
      <div><span className="eyebrow">结构预演</span><h1>成片时间线</h1><p>平台按已选草稿或最终成片合成，不会覆盖原始候选视频。</p></div>
      <button className="button secondary" onClick={() => setPlaying(!playing)}>{playing ? <Pause size={17} /> : <Play size={17} />}{playing ? '暂停预演' : '预演节奏'}</button>
    </div>
    <section className="assembly-plan">
      <div className="assembly-heading">
        <div><span className="eyebrow">交付装配计划</span><h2>锁定顺序、入出点与对白策略</h2><p>每个装配项冻结小节、镜头、候选和母版修订；只引用真实媒体，不复制、不覆盖候选视频。</p></div>
        <div className="assembly-meta">
          <StatusPill status={deliveryWorkspace?.plan.status === 'locked' ? '已锁定' : '草稿'} />
          <span>修订 {deliveryWorkspace?.plan.revision || 0}</span>
          <code title={deliveryWorkspace?.plan.plan_hash}>{deliveryWorkspace?.plan.plan_hash?.slice(0, 12) || '尚未保存'}</code>
        </div>
      </div>
      <div className="assembly-list">
        {assemblyItems.map((item, index) => {
          const source = preflight?.sources.find((entry) => entry.shot_id === item.shot_id)
          const locked = deliveryWorkspace?.plan.status === 'locked'
          return <article className="assembly-row" key={item.shot_id}>
            <span className="assembly-index">{String(index + 1).padStart(2, '0')}</span>
            <div className="assembly-move"><button disabled={locked || index === 0} onClick={() => moveAssemblyItem(index, -1)} title="上移"><ArrowUp size={14} /></button><button disabled={locked || index === assemblyItems.length - 1} onClick={() => moveAssemblyItem(index, 1)} title="下移"><ArrowDown size={14} /></button></div>
            <div className="assembly-copy"><strong>{item.title}</strong><small>{item.scene_code} · {item.seconds.toFixed(2)} 秒 · {source ? `${source.width}×${source.height} ${source.source_type === 'promotion' ? '成片' : '候选'}` : '尚无可导出来源'}</small></div>
            <div className={`assembly-source ${item.source_snapshot?.source_status || 'missing'}`}><strong>{item.source_snapshot?.source_status === 'ready' ? '来源已冻结' : '来源待处理'}</strong><small>{item.section_id ? `小节 ${item.section_id}` : '历史手工分镜'} → {item.shot_id} → {item.candidate_id || '未选候选'}{item.source_snapshot?.master_revision ? ` · 母版 R${item.source_snapshot.master_revision}` : ''}</small></div>
            <label className="clip-point"><span>入点</span><input type="number" min="0" max={Math.max(0, (item.out_point_seconds || item.seconds) - 0.05)} step="0.05" disabled={locked} value={item.in_point_seconds || 0} onChange={(event) => updateAssemblyItem(item.shot_id, { in_point_seconds: Number(event.target.value) })} /></label>
            <label className="clip-point"><span>出点</span><input type="number" min="0.05" max={item.seconds} step="0.05" disabled={locked} value={item.out_point_seconds || item.seconds} onChange={(event) => updateAssemblyItem(item.shot_id, { out_point_seconds: Number(event.target.value) })} /></label>
            <label className="dialogue-mode"><span>对白</span><select disabled={locked} value={item.dialogue_mode || 'original'} onChange={(event) => updateAssemblyItem(item.shot_id, { dialogue_mode: event.target.value as DeliveryPlanItem['dialogue_mode'] })}><option value="original">保留原声</option><option value="mute">静音对白</option></select></label>
            <label className="subtitle-switch"><input type="checkbox" disabled={locked || !item.dialogue} checked={Boolean(item.dialogue && item.subtitle_enabled)} onChange={(event) => updateAssemblyItem(item.shot_id, { subtitle_enabled: event.target.checked })} /><span>{item.dialogue ? '显示对白字幕' : '无对白'}</span></label>
            <label className="subtitle-start"><span>字幕入点</span><input type="number" min="0" max={Math.max(0, item.seconds - 0.05)} step="0.05" disabled={locked || !item.subtitle_enabled} value={item.subtitle_start_seconds || 0} onChange={(event) => updateAssemblyItem(item.shot_id, { subtitle_start_seconds: Number(event.target.value) })} /></label>
            <span className="transition-label">硬切</span>
          </article>
        })}
      </div>
      <div className="assembly-actions">
        <span>{deliveryWorkspace?.summary.item_count || assemblyItems.length} 镜头 · {deliveryWorkspace?.summary.subtitle_count || assemblyItems.filter((item) => item.subtitle_enabled).length} 条字幕 · 约 {(deliveryWorkspace?.summary.planned_seconds || assemblyItems.reduce((sum, item) => sum + item.seconds, 0)).toFixed(2)} 秒</span>
        {deliveryWorkspace?.plan.status === 'locked'
          ? <button className="button secondary" disabled={savingAssembly} onClick={reopenAssembly}><RefreshCw size={15} />开启新修订</button>
          : <><button className="button secondary" disabled={savingAssembly || (!assemblyDirty && deliveryWorkspace?.plan.revision !== 0)} onClick={persistAssembly}><Save size={15} />保存草稿</button><button className="button primary" title={assemblySourcesReady ? '冻结当前装配凭证' : '所有镜头都需先通过结构化审片并选择有完整证据的母版'} disabled={savingAssembly || !assemblySourcesReady} onClick={lockAssembly}><ShieldCheck size={15} />锁定交付计划</button></>}
      </div>
      {deliveryWorkspace?.versions.length ? <details className="assembly-history"><summary>查看 {deliveryWorkspace.versions.length} 个历史修订</summary>{deliveryWorkspace.versions.map((version) => <span key={version.id}>R{version.revision} · {version.status} · {version.plan_hash.slice(0, 12)} · {formatTimestamp(version.created_at)}</span>)}</details> : null}
    </section>
    <section className={`export-gate ${preflight?.ready ? 'ready' : 'blocked'}`}>
      <div className="export-gate-copy">
        <span className="eyebrow">生产导出门禁</span>
        <h2>{preflight?.ready ? '当前项目可以进入横屏合成' : '仍有镜头不能进入生产导出'}</h2>
        <p>{preflight ? `${preflight.ready_shot_count}/${preflight.shot_count} 个镜头已有真实且已选的本地版本 · 预计 ${preflight.duration_seconds.toFixed(2)} 秒` : '正在检查镜头来源…'}</p>
        <small>来源策略：只使用人工已选版本；mock、缺失文件、未完成任务和无音轨视频都会阻断导出。</small>
      </div>
        {preflight?.delivery_plan && <code className="delivery-plan-proof">装配 R{preflight.delivery_plan.revision} · {preflight.delivery_plan.plan_hash.slice(0, 12)}</code>}
      <div className="export-gate-action">
        {latestRun && <div className="export-run-state"><StatusPill status={latestRun.state} /><span>{latestRun.message}</span>{latestRun.error && <small title={latestRun.error}>{latestRun.error}</small>}</div>}
        <button className="button primary" disabled={!preflight?.ready || exporting} onClick={onExport}>{exporting ? <LoaderCircle className="spin" size={17} /> : <Film size={17} />}{exporting ? '后台导出中' : '导出横屏成片'}</button>
      </div>
      {preflight?.issues.length ? <div className="export-issues">{preflight.issues.map(issue => <span key={issue.shot_id}><strong>{displayShotId(issue.shot_id)}</strong>{issue.message}</span>)}</div> : null}
    </section>
    {roughCut.available && roughCut.video && <section className="roughcut-delivery"><div><span className="eyebrow">当前可交付版本</span><h2>{roughCut.name}</h2><p>{roughCut.width}×{roughCut.height} · {(roughCut.duration_seconds || 0).toFixed(2)} 秒 · {roughCut.shot_count} 镜头 · 含音轨与可开关字幕</p>{roughCut.sha256 && <code title={roughCut.sha256}>视频 SHA-256 · {roughCut.sha256}</code>}<small>{roughCut.quality_note}</small><nav>{roughCut.subtitles && <a href={roughCut.subtitles}>下载 SRT 字幕</a>}{roughCut.sources && <a href={roughCut.sources}>查看来源清单</a>}{roughCut.manifest && <a href={roughCut.manifest}>查看生产清单</a>}</nav></div><video controls preload="metadata" src={roughCut.video}>{roughCut.captions && <track kind="subtitles" src={roughCut.captions} srcLang="zh" label="中文" default />}</video></section>}
    <section className="export-history">
      <div className="export-history-heading"><div><span className="eyebrow">任务与版本</span><h2>平台导出历史</h2></div><small>服务重启可恢复 · 失败可重试 · 已完成版本可回切</small></div>
      <div className="export-run-list">
        {exportRuns.length ? exportRuns.map((run) => {
          const expanded = expandedRunId === run.id
          return <article className={`export-run-row ${run.is_current ? 'current' : ''}`} key={run.id}>
            <button className="export-run-main" onClick={() => setExpandedRunId(expanded ? null : run.id)} aria-expanded={expanded}>
              <span className="export-run-index">{run.is_current ? <Check size={15} /> : <Clock3 size={15} />}</span>
              <span><strong>{run.is_current ? '当前交付版本' : run.state === '已完成' ? '历史成片版本' : '导出任务'}</strong><small>{run.output_name}</small></span>
              <span><StatusPill status={run.state} /><small>{run.message}</small></span>
              <span><strong>{run.width}×{run.height}</strong><small>尝试 {run.attempt} · 恢复 {run.recovery_count} 次</small></span>
              <span><strong>{formatTimestamp(run.completed_at || run.updated_at)}</strong><small>{typeof run.outputs.duration_seconds === 'number' ? `${run.outputs.duration_seconds.toFixed(2)} 秒` : '等待产物'}</small></span>
              <ChevronRight className={expanded ? 'expanded' : ''} size={17} />
            </button>
            {expanded && <div className="export-run-detail">
              <div className="export-event-list">{run.events?.length ? run.events.map((event) => <div className={`export-event ${event.level}`} key={event.id}><time>{formatTimestamp(event.created_at)}</time><i /><span>{event.message}</span></div>) : <p>尚无任务事件。</p>}</div>
              <div className="export-run-actions">
                {run.can_cancel && <button className="button secondary danger" onClick={() => runAction(run, 'cancel')}><X size={14} />取消任务</button>}
                {run.can_retry && <button className="button secondary" onClick={() => runAction(run, 'retry')}><RefreshCw size={14} />按原输入重试</button>}
                {run.can_activate && <button className="button secondary" onClick={() => runAction(run, 'activate')}><Check size={14} />设为当前版本</button>}
                {run.state === '已完成' && <a href={`/api/export-runs/${run.id}/files/video`}>下载成片</a>}
                {run.state === '已完成' && <a href={`/api/export-runs/${run.id}/files/manifest`}>生产清单</a>}
                <a href={`/api/export-runs/${run.id}/logs`} target="_blank" rel="noreferrer">完整日志</a>
              </div>
              {run.error && <pre className="export-error">{run.error}</pre>}
            </div>}
          </article>
        }) : <div className="empty">还没有平台导出任务。</div>}
      </div>
    </section>
    <div className="timeline-ruler">{Array.from({ length: 9 }, (_, i) => <span key={i}>{i * 5}s</span>)}</div><div className="timeline-playhead" />
    <div className="timeline-track"><div className="track-label"><Film />V1</div><div className="clips">{orderedShots.map((shot, index) => <button key={shot.id} style={{ width: `${Math.max(110, shot.seconds * 29)}px` }}><ShotThumbnail shot={shot} /><span>{index + 1}. {shot.title}</span><small>{shot.seconds}s</small></button>)}</div></div>
    <div className="timeline-track audio"><div className="track-label"><Volume2 />A1</div><div className="audio-wave">对白、环境声与连续雨声底 · 已完成基础响度平衡</div></div>
  </main>
}

function NewProjectModal({ onClose, onCreated }: { onClose: () => void; onCreated: (project: Project) => void }) {
  const [form, setForm] = useState({ title: '', episode: 'EP01', logline: '', target_duration: '45', shots: '' })
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    try {
      const lines = form.shots.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
      const shots = lines.map((line, index) => {
        const parts = line.split(/[|｜]/).map((part) => part.trim())
        if (parts.length < 2 || !parts[0] || !parts[1]) throw new Error(`第 ${index + 1} 行至少需要“标题｜剧情动作”`)
        return {
          title: parts[0],
          description: parts[1],
          dialogue: parts[2] || '',
          prompt: parts[3] || `${parts[1]}, cinematic realism, coherent character and location, one continuous shot, no subtitles or logos`,
        }
      })
      const created = await api<Project>('/api/projects', {
        method: 'POST',
        body: JSON.stringify({ ...form, target_duration: Number(form.target_duration), shots }),
      })
      onCreated(created)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '项目创建失败')
    }
  }
  return <div className="modal-backdrop"><form className="modal project-modal" onSubmit={submit}>
    <div className="modal-title"><div><span className="eyebrow">新短剧工作区</span><h2>从创作规划开始新项目</h2></div><button type="button" onClick={onClose}><X /></button></div>
    <div className="form-grid project-fields"><label>项目名称<input required minLength={2} value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder="例如：电梯停在十三层" /></label><label>集号<input required value={form.episode} onChange={(event) => setForm({ ...form, episode: event.target.value })} /></label><label>目标时长（秒）<input required type="number" min="5" max="3600" value={form.target_duration} onChange={(event) => setForm({ ...form, target_duration: event.target.value })} /></label></div>
    <label>已有故事想法 <small>可空，创建后在“创作规划”完善主题和剧情提案</small><textarea rows={3} value={form.logline} onChange={(event) => setForm({ ...form, logline: event.target.value })} placeholder="一句话想法，或者暂时留空。" /></label>
    <label>已有初始分镜 <small>可空；每行：标题｜剧情动作｜对白（可空）｜H3 提示词（可空）</small><textarea className="shot-import" rows={6} value={form.shots} onChange={(event) => setForm({ ...form, shots: event.target.value })} placeholder={'如果已有分镜，可以在这里批量导入；没有就直接创建空项目。\n电梯来电｜凌晨，林夏独自走入电梯，楼层灯闪烁'} /></label>
    <p className="import-note">空项目会先进入“创作规划”：简报 → 剧情提案 → 角色 → 章节与小节。这里不会调用 Agent 或提交 GPU。</p>
    {error && <p className="form-error">{error}</p>}
    <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>取消</button><button className="button primary"><FolderPlus size={16} />创建工作区</button></div>
  </form></div>
}

function NewShotModal({ onClose, onCreated }: { onClose: () => void; onCreated: (shot: Shot) => void }) {
  const [form, setForm] = useState({ title: '', description: '', dialogue: '', prompt: '' })
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => { event.preventDefault(); try { const shot = await api<Shot>('/api/shots', { method: 'POST', body: JSON.stringify(form) }); onCreated(shot) } catch (e) { setError(e instanceof Error ? e.message : '创建失败') } }
  return <div className="modal-backdrop"><form className="modal" onSubmit={submit}><div className="modal-title"><div><span className="eyebrow">追加分镜</span><h2>新建镜头</h2></div><button type="button" onClick={onClose}><X /></button></div><label>镜头标题<input required minLength={2} value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="例如：电话里的倒计时" /></label><label>剧情动作<textarea required rows={3} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></label><label>对白<input value={form.dialogue} onChange={e => setForm({ ...form, dialogue: e.target.value })} /></label><label>H3 画面提示词<textarea required rows={5} value={form.prompt} onChange={e => setForm({ ...form, prompt: e.target.value })} /></label>{error && <p className="form-error">{error}</p>}<div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>取消</button><button className="button primary">加入分镜表</button></div></form></div>
}

function ConfirmModal({ shot, onClose, onConfirm }: { shot: Shot; onClose: () => void; onConfirm: () => void }) {
  return <div className="modal-backdrop"><div className="modal compact"><div className="modal-title"><div><span className="eyebrow">GPU 任务确认</span><h2>提交 {shot.candidate_count} 条候选？</h2></div><button onClick={onClose}><X /></button></div><div className="confirm-summary"><p><strong>{displayShotId(shot.id)}</strong> · {shot.title}</p><p>{shot.width}×{shot.height} · {shot.seconds} 秒 · 20 steps</p><p>节点图已通过 dry-run。继续后将向本机 ComfyUI 提交真实生成任务并占用显卡。</p></div><div className="modal-actions"><button className="button secondary" onClick={onClose}>暂不提交</button><button className="button primary" onClick={onConfirm}>确认占用 GPU</button></div></div></div>
}

export default App

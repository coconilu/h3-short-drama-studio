import { FormEvent, useEffect, useMemo, useState } from 'react'
import {
  Archive,
  BookMarked,
  Box,
  Check,
  ChevronRight,
  Film,
  Image,
  LoaderCircle,
  LockKeyhole,
  MapPinned,
  Mic2,
  Plus,
  Save,
  ShieldCheck,
  Sparkles,
  Unlink,
  UserRound,
  X,
} from 'lucide-react'
import type { Asset, BibleEntry, BibleEntryType, BibleVersion, BibleWorkspace } from './types'

const typeMeta: Record<BibleEntryType, { label: string; hint: string; icon: typeof UserRound }> = {
  character: { label: '角色', hint: '身份、外形、服装与表演边界', icon: UserRound },
  location: { label: '场景', hint: '空间结构、陈设、光线与天气', icon: MapPinned },
  prop: { label: '道具', hint: '造型、材质、状态与使用规则', icon: Box },
  style: { label: '视听风格', hint: '全局画风、摄影、色彩与质感', icon: Film },
  voice: { label: '声音', hint: '音色、语速、情绪与环境声', icon: Mic2 },
}

const api = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  const headers = new Headers(options?.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...options, headers })
  if (!response.ok) {
    const text = await response.text()
    try { throw new Error(JSON.parse(text).detail || text) } catch (error) {
      if (error instanceof SyntaxError) throw new Error(text || `请求失败：${response.status}`)
      throw error
    }
  }
  return response.json()
}

const shortShotId = (id: string) => {
  const match = id.match(/-(S\d+)-(\d{3})$/)
  return match ? `${match[1]}-${match[2]}` : id
}

function MediaThumb({ asset }: { asset: Asset }) {
  if (asset.media_type === 'video') return <video src={asset.preview} muted preload="metadata" />
  if (asset.media_type === 'audio') return <span className="bible-audio-thumb"><Mic2 size={17} /></span>
  return <img src={asset.preview} alt="" />
}

export function ProductionBible({ projectId, assets, setNotice }: {
  projectId: string
  assets: Asset[]
  setNotice: (message: string) => void
}) {
  const [workspace, setWorkspace] = useState<BibleWorkspace | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [filter, setFilter] = useState<BibleEntryType | 'all'>('all')
  const [draft, setDraft] = useState<BibleEntry | null>(null)
  const [busy, setBusy] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [createType, setCreateType] = useState<BibleEntryType>('character')
  const [createName, setCreateName] = useState('')
  const [archiveConfirm, setArchiveConfirm] = useState(false)
  const [versions, setVersions] = useState<BibleVersion[] | null>(null)
  const [assetChoice, setAssetChoice] = useState('')

  const load = async () => {
    const data = await api<BibleWorkspace>('/api/bible')
    setWorkspace(data)
    setSelectedId((current) => data.entries.some((entry) => entry.id === current) ? current : data.entries[0]?.id || '')
  }

  useEffect(() => { load().catch((error) => setNotice(error.message)) }, [projectId])

  const selected = useMemo(() => workspace?.entries.find((entry) => entry.id === selectedId) || null, [workspace, selectedId])
  useEffect(() => { setDraft(selected); setArchiveConfirm(false); setAssetChoice('') }, [selected])
  const visibleEntries = workspace?.entries.filter((entry) => filter === 'all' || entry.entry_type === filter) || []
  const bindableAssets = assets.filter((asset) => asset.bindable && !selected?.assets.some((linked) => linked.id === asset.id))

  const mutate = async (operation: () => Promise<BibleWorkspace>, success: string) => {
    setBusy(true)
    try {
      const next = await operation()
      setWorkspace(next)
      setNotice(success)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '生产圣经操作失败')
    } finally { setBusy(false) }
  }

  const create = async (event: FormEvent) => {
    event.preventDefault()
    if (!createName.trim()) return
    await mutate(
      () => api('/api/bible/entries', { method: 'POST', body: JSON.stringify({ entry_type: createType, name: createName.trim(), apply_globally: createType === 'style' }) }),
      `已创建${typeMeta[createType].label}条目“${createName.trim()}”`,
    )
    setCreateOpen(false)
    setCreateName('')
  }

  const save = async () => {
    if (!draft) return
    await mutate(
      () => api(`/api/bible/entries/${draft.id}`, {
        method: 'PATCH', body: JSON.stringify({
          base_revision: draft.revision,
          name: draft.name,
          summary: draft.summary,
          canonical_description: draft.canonical_description,
          prompt_fragment: draft.prompt_fragment,
          negative_prompt: draft.negative_prompt,
          continuity_rules: draft.continuity_rules,
          apply_globally: draft.apply_globally,
        }),
      }),
      `“${draft.name}”已保存为新修订`,
    )
  }

  const linkAsset = async () => {
    if (!selected || !assetChoice) return
    const asset = assets.find((item) => item.id === assetChoice)
    await mutate(
      () => api(`/api/bible/entries/${selected.id}/assets/link`, { method: 'POST', body: JSON.stringify({ base_revision: selected.revision, asset_id: assetChoice, role: 'continuity-reference' }) }),
      `已绑定真实素材“${asset?.name || assetChoice}”`,
    )
  }

  const toggleShot = async (shotId: string, linked: boolean) => {
    if (!selected) return
    await mutate(
      () => api(`/api/bible/entries/${selected.id}/shots/${linked ? 'unlink' : 'link'}`, { method: 'POST', body: JSON.stringify({ base_revision: selected.revision, shot_id: shotId, role: 'continuity', note: '' }) }),
      linked ? '已解除镜头连续性绑定' : '已绑定到镜头；条目已回到草稿状态等待复核',
    )
  }

  if (!workspace) return <div className="script-loading"><LoaderCircle className="spin" />正在读取生产圣经…</div>

  return <main className="bible-page">
    <header className="bible-heading">
      <div><span className="eyebrow">{workspace.project.episode} · PRE-PRODUCTION</span><h1>生产圣经</h1><p>把可复用的角色、场景、道具、风格和声音定义成后续生成可读取的单一事实源。</p></div>
      <div className="bible-summary"><span><strong>{workspace.summary.locked_count}</strong>/{workspace.summary.entry_count} 已锁定</span><span><strong>{workspace.summary.covered_shot_count}</strong>/{workspace.summary.shot_count} 镜头有输入</span><button className="button primary" onClick={() => setCreateOpen(true)}><Plus size={16} />新建条目</button></div>
    </header>

    <section className="bible-workspace">
      <aside className="bible-types">
        <button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}><BookMarked /><span><strong>全部</strong><small>{workspace.entries.length} 项设定</small></span></button>
        {(Object.entries(typeMeta) as Array<[BibleEntryType, typeof typeMeta.character]>).map(([type, meta]) => <button className={filter === type ? 'active' : ''} onClick={() => setFilter(type)} key={type}><meta.icon /><span><strong>{meta.label}</strong><small>{workspace.entries.filter((entry) => entry.entry_type === type).length} 项</small></span></button>)}
      </aside>

      <section className="bible-list">
        <div className="bible-list-head"><span>{filter === 'all' ? '全部设定' : typeMeta[filter].label}</span><small>{visibleEntries.length} 项</small></div>
        {visibleEntries.map((entry) => <button className={selectedId === entry.id ? 'active' : ''} key={entry.id} onClick={() => setSelectedId(entry.id)}><span className={`bible-status ${entry.status}`}><i />{entry.status === 'locked' ? '已锁定' : '草稿'}</span><strong>{entry.name}</strong><p>{entry.summary || typeMeta[entry.entry_type].hint}</p><footer><span>R{entry.revision}</span><span>{entry.assets.length} 素材</span><span>{entry.apply_globally ? '全局' : `${entry.shots.length} 镜头`}</span><ChevronRight size={14} /></footer></button>)}
        {!visibleEntries.length && <div className="bible-empty"><BookMarked size={24} /><strong>还没有这类设定</strong><span>新建的条目默认是草稿，不会直接进入 H3 提示词。</span></div>}
      </section>

      {draft ? <section className="bible-editor">
        <div className="bible-editor-head"><div><span>{typeMeta[draft.entry_type].label} · R{draft.revision}</span><strong>{draft.name}</strong></div><div><button className="icon-action" title="查看版本" onClick={async () => setVersions(await api(`/api/bible/entries/${draft.id}/versions`))}><BookMarked size={17} /></button><button className="button secondary" disabled={busy} onClick={save}><Save size={15} />保存修订</button><button className="button primary" disabled={busy || draft.status === 'locked'} onClick={() => mutate(() => api(`/api/bible/entries/${draft.id}/lock`, { method: 'POST', body: JSON.stringify({ base_revision: draft.revision }) }), `“${draft.name}”已锁定，可供后续提示词编译`)}><LockKeyhole size={15} />{draft.status === 'locked' ? '已锁定' : '锁定版本'}</button></div></div>
        <div className="bible-form">
          <label>条目名称<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>一句话识别点<input value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} placeholder="让团队一眼确认这是同一个对象" /></label>
          <label className="wide">标准设定<textarea rows={4} value={draft.canonical_description} onChange={(event) => setDraft({ ...draft, canonical_description: event.target.value })} placeholder="稳定不变的身份、外形、空间、材质或声音事实；不要写本镜头动作" /></label>
          <label className="wide prompt-field">H3 提示词片段<textarea rows={4} value={draft.prompt_fragment} onChange={(event) => setDraft({ ...draft, prompt_fragment: event.target.value })} placeholder="后续会由提示词编译器按镜头组合；建议使用清晰、可观察的英文描述" /></label>
          <label>负面约束<textarea rows={3} value={draft.negative_prompt} onChange={(event) => setDraft({ ...draft, negative_prompt: event.target.value })} placeholder="不能出现的变体、服装、材质、声音等" /></label>
          <label>连续性规则<textarea rows={3} value={draft.continuity_rules} onChange={(event) => setDraft({ ...draft, continuity_rules: event.target.value })} placeholder="跨镜头必须保持的状态，以及允许变化的条件" /></label>
          <label className="bible-global"><input type="checkbox" checked={draft.apply_globally} onChange={(event) => setDraft({ ...draft, apply_globally: event.target.checked })} /><span><strong>应用到全片</strong><small>适合统一摄影风格、全片声音基调；角色与场景通常按镜头绑定。</small></span></label>
        </div>

        <section className="bible-bindings"><header><div><Image size={16} /><span><strong>真实参考素材</strong><small>仅受管素材可进入生产圣经；绑定变化会生成新修订并回到草稿。</small></span></div><div><select aria-label="选择圣经参考素材" value={assetChoice} onChange={(event) => setAssetChoice(event.target.value)}><option value="">选择受管素材</option>{bindableAssets.map((asset) => <option key={asset.id} value={asset.id}>{asset.name}</option>)}</select><button className="button secondary" disabled={!assetChoice || busy} onClick={linkAsset}>绑定</button></div></header><div className="bible-media-list">{selected?.assets.map((asset) => <article key={asset.id}><MediaThumb asset={asset} /><span><strong>{asset.name}</strong><small>{asset.kind} · {asset.media_type}</small></span><button title="解除素材绑定" disabled={busy} onClick={() => mutate(() => api(`/api/bible/entries/${selected.id}/assets/unlink`, { method: 'POST', body: JSON.stringify({ base_revision: selected.revision, asset_id: asset.id, role: asset.bible_role }) }), '已解除真实素材绑定')}><Unlink size={14} /></button></article>)}{!selected?.assets.length && <p>当前条目是文本设定；可以锁定，但不会为 Ref2VA 提供参考文件。</p>}</div></section>

        <section className="bible-shot-links"><header><div><ShieldCheck size={16} /><span><strong>镜头连续性覆盖</strong><small>{draft.apply_globally ? '此条目已全片生效，无需逐镜重复绑定。' : '明确哪些镜头需要这个对象，P12 才能按镜头编译。'}</small></span></div></header><div>{workspace.shots.map((shot) => { const linked = Boolean(selected?.shots.some((item) => item.shot_id === shot.id)); return <button key={shot.id} disabled={busy || draft.apply_globally} className={linked || draft.apply_globally ? 'linked' : ''} onClick={() => toggleShot(shot.id, linked)}><span>{linked || draft.apply_globally ? <Check size={13} /> : null}</span><strong>{shortShotId(shot.id)}</strong><small>{shot.title}</small></button> })}</div></section>

        <footer className="bible-editor-footer"><span><ShieldCheck size={14} />每次保存、绑定和锁定都有不可变快照；旧页面提交会因修订号冲突而被拒绝。</span>{archiveConfirm ? <div><small>归档会停止后续编译，但保留版本记录。</small><button className="button secondary" onClick={() => setArchiveConfirm(false)}>取消</button><button className="button danger-outline" disabled={busy} onClick={() => mutate(() => api(`/api/bible/entries/${draft.id}/archive`, { method: 'POST', body: JSON.stringify({ base_revision: draft.revision }) }), `“${draft.name}”已归档`)}>确认归档</button></div> : <button className="text-action danger-text" onClick={() => setArchiveConfirm(true)}><Archive size={14} />归档条目</button>}</footer>
      </section> : <section className="bible-editor bible-no-selection"><BookMarked size={32} /><strong>选择或新建一个生产设定</strong><span>锁定的条目将在 P12 成为逐镜 H3 提示词的结构化输入。</span></section>}
    </section>

    <section className="bible-coverage"><header><div><span className="eyebrow">CONTINUITY COVERAGE</span><h2>逐镜覆盖矩阵</h2></div><p>这不是“越多越好”的分数；它只暴露哪些镜头尚未得到任何可复用设定，或仍引用草稿。</p></header><div className="coverage-table"><div className="coverage-head"><span>镜头</span><span>有效条目</span><span>锁定情况</span><span>处理建议</span></div>{workspace.shots.map((shot) => <article key={shot.id}><strong>{shortShotId(shot.id)} · {shot.title}</strong><div>{shot.entries.map((entry) => <span className={`${entry.entry_type} ${entry.status}`} key={entry.id}>{typeMeta[entry.entry_type].label} · {entry.name}</span>)}{!shot.entries.length && <small>暂无</small>}</div><span>{shot.locked_count}/{shot.entries.length}</span><em className={shot.attention === '连续性输入已锁定' ? 'ready' : ''}>{shot.attention}</em></article>)}</div></section>

    {createOpen && <div className="modal-backdrop"><form className="modal compact bible-create" onSubmit={create}><div className="modal-title"><div><span className="eyebrow">NEW BIBLE ENTRY</span><h2>建立生产事实源</h2></div><button type="button" onClick={() => setCreateOpen(false)}><X /></button></div><label>类型<select value={createType} onChange={(event) => setCreateType(event.target.value as BibleEntryType)}>{(Object.entries(typeMeta) as Array<[BibleEntryType, typeof typeMeta.character]>).map(([type, meta]) => <option value={type} key={type}>{meta.label} · {meta.hint}</option>)}</select></label><label>名称<input autoFocus value={createName} onChange={(event) => setCreateName(event.target.value)} placeholder="例如：林夏 / 雨夜便利店 / 红色雨衣" /></label><p>创建后仍是草稿；填写标准设定与 H3 片段并人工锁定，才会进入后续编译。</p><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setCreateOpen(false)}>取消</button><button className="button primary" disabled={busy || !createName.trim()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Plus size={15} />}创建草稿</button></div></form></div>}

    {versions && <div className="modal-backdrop"><section className="modal bible-versions"><div className="modal-title"><div><span className="eyebrow">IMMUTABLE HISTORY</span><h2>{draft?.name} · 修订记录</h2></div><button onClick={() => setVersions(null)}><X /></button></div><div>{versions.map((version) => <article key={version.id}><span className={version.status}>{version.status === 'locked' ? <LockKeyhole size={13} /> : <Sparkles size={13} />}R{version.revision}</span><strong>{version.source.replace('human:', '')}</strong><p>{version.snapshot.summary || version.snapshot.canonical_description || '空白草稿'}</p><time>{new Date(version.created_at).toLocaleString('zh-CN')}</time></article>)}</div></section></div>}
  </main>
}

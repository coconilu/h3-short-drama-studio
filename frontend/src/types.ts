export type Health = {
  api: string
  api_pid?: number
  api_started_at?: string
  api_uptime_seconds?: number
  database_path?: string
  comfyui: 'online' | 'offline'
  comfyui_url: string
  gpu?: string
  h3_adapter: boolean
  h3_project_root?: string
  export_worker?: 'online' | 'offline'
  export_worker_id?: string
  export_queue?: { queued: number; running: number }
  supervisor?: SupervisorStatus
  error?: string
}

export type SupervisorService = {
  state: 'starting' | 'online' | 'external' | 'restarting' | 'stopped'
  pid?: number
  managed: boolean
  external?: boolean
  port: number
  restarts: number
  started_at?: string
  last_exit_code?: number
  log_path: string
}

export type SupervisorStatus = {
  state: 'online' | 'stale' | 'not_running'
  managed: boolean
  message: string
  supervisor_pid?: number
  started_at?: string
  updated_at?: string
  project_root?: string
  status_path: string
  services: Record<string, SupervisorService>
}

export type Shot = {
  id: string
  ordinal: number
  scene_code: string
  title: string
  description: string
  dialogue: string
  sound: string
  prompt: string
  status: string
  width: number
  height: number
  seconds: number
  candidate_count: number
  strategy: '保真放大' | 'Ref2VA 精修'
  thumbnail: string
  video?: string
  subtitle_enabled: number
  subtitle_start_seconds?: number
  final_output?: Promotion
  source_mapping?: {
    shot_id: string
    section_id: string
    section_title: string
    last_synced_revision: number
    current_section_revision: number
  }
}

export type Promotion = {
  id: string
  shot_id: string
  external_id: string
  source_candidate?: string
  strategy: 'scale' | 'ref2va'
  status: 'queued' | 'running' | 'completed' | 'error'
  width: number
  height: number
  actual_seconds?: number
  elapsed_seconds?: number
  prompt_id?: string
  video?: string
  selected: number
  note: string
  created_at?: string
  selected_at?: string
  metadata?: Record<string, unknown>
}

export type Project = {
  id: string
  title: string
  episode: string
  logline: string
  target_duration: number
  shots: Shot[]
}

export type CreativeBrief = {
  project_id: string
  theme: string
  genre: string
  tone: string
  audience: string
  target_duration: number
  constraints: string
  status: 'draft' | 'approved'
  revision: number
  version_count: number
  created_at: string
  updated_at: string
}

export type CreativeProposal = {
  id: string
  project_id: string
  ordinal: number
  title: string
  synopsis: string
  core_conflict: string
  ending: string
  status: 'draft' | 'finalized'
  revision: number
  version_count: number
  created_at: string
  updated_at: string
}

export type CreativeCharacter = {
  id: string
  project_id: string
  ordinal: number
  name: string
  identity: string
  goal: string
  obstacle: string
  personality: string
  appearance: string
  voice: string
  relationships: string
  reference_notes: string
  status: 'draft' | 'approved'
  revision: number
  version_count: number
  created_at: string
  updated_at: string
}

export type CreativeSection = {
  id: string
  project_id: string
  chapter_id: string
  ordinal: number
  title: string
  summary: string
  content: string
  scene: string
  action: string
  dialogue: string
  sound: string
  visual: string
  pacing_goal: string
  planned_seconds: number
  status: 'draft' | 'approved'
  review_note: string
  approved_at?: string
  revision: number
  version_count: number
  created_at: string
  updated_at: string
}

export type CreativeChapter = {
  id: string
  project_id: string
  ordinal: number
  title: string
  summary: string
  pacing_goal: string
  planned_seconds: number
  status: 'draft' | 'approved'
  revision: number
  version_count: number
  created_at: string
  updated_at: string
  sections: CreativeSection[]
}

export type CreativePlanningWorkspace = {
  project: { id: string; title: string; episode: string }
  brief: CreativeBrief
  proposals: CreativeProposal[]
  characters: CreativeCharacter[]
  chapters: CreativeChapter[]
  summary: {
    proposal_count: number
    character_count: number
    chapter_count: number
    section_count: number
    finalized_proposal_id?: string
    ready: boolean
  }
  next_actions: Array<{ id: string; label: string; complete: boolean; action: string }>
}

export type CreativeStoryboardAction =
  | 'create' | 'update' | 'delete' | 'reorder' | 'unchanged' | 'protected' | 'preserve'

export type CreativeStoryboardFieldDiff = {
  field: 'title' | 'description' | 'dialogue' | 'sound' | 'seconds' | 'ordinal'
  label: string
  before: string | number | null
  after: string | number | null
  changed: boolean
  decision: 'create' | 'update' | 'delete' | 'protected' | 'keep'
}

export type CreativeStoryboardRow = {
  action: CreativeStoryboardAction
  section: null | {
    id: string
    chapter_id?: string
    chapter_title?: string
    title?: string
    status: 'draft' | 'approved' | 'archived'
    revision?: number
  }
  shot_id?: string
  mapping_id?: string
  current?: Record<string, string | number | null>
  proposed?: Record<string, string | number | null>
  field_diffs: CreativeStoryboardFieldDiff[]
  protected_reasons: string[]
}

export type CreativeStoryboardPreview = {
  project_id: string
  plan_hash: string
  rows: CreativeStoryboardRow[]
  blockers: string[]
  can_apply: boolean
  summary: Record<CreativeStoryboardAction, number>
  section_revisions: Record<string, number>
  safety: {
    preview_has_side_effects: false
    explicit_confirmation_required: true
    historical_manual_shots_preserved: true
  }
}

export type CreativeRevisionHistory = {
  entity_type: 'brief' | 'proposal' | 'character' | 'chapter' | 'section'
  entity_id: string
  revisions: Array<{
    id: string
    revision: number
    source: string
    snapshot: Record<string, unknown>
    created_at: string
  }>
}

export type CreativeArchiveEntry = {
  id: string
  entity_type: 'proposal' | 'character' | 'chapter' | 'section'
  title: string
  status: 'archived'
  revision: number
  updated_at: string
  archived_at: string
  source: string
  history_url: string
}

export type CreativeArchive = {
  project: { id: string; title: string; episode: string }
  entries: CreativeArchiveEntry[]
  summary: {
    total: number
    by_type: Record<CreativeArchiveEntry['entity_type'], number>
  }
}

export type LocalAgentProvider = {
  id: 'codex' | 'kimi'
  adapter: 'codex' | 'kimi'
  label: string
  executable_path?: string
  enabled: boolean
  timeout_seconds: number
  model: string
  capabilities: Array<{ scope: CreativeAgentScope; operations: CreativeAgentOperation[] }>
  probe_state: 'unknown' | 'verified' | 'unverified' | 'unavailable'
  installed: boolean
  callable: boolean
  auth_state: 'unknown' | 'verified' | 'unverified' | 'unavailable'
  model_state: 'unknown' | 'verified' | 'unverified' | 'unavailable'
  callable_state: 'unknown' | 'verified' | 'unverified' | 'unavailable'
  version?: string
  last_error?: string
  last_probe_at?: string
  action_hint: string
}

export type CreativeAgentScope = 'plot' | 'outline' | 'chapter' | 'section' | 'body'
export type CreativeAgentOperation = 'generate' | 'expand' | 'compress' | 'rewrite' | 'proofread'

export type CreativeAgentRun = {
  id: string
  project_id: string
  provider_id: 'codex' | 'kimi'
  scope: CreativeAgentScope
  operation: CreativeAgentOperation
  target_id?: string
  parent_id?: string
  instruction: string
  state: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'applied' | 'rejected'
  message: string
  input_summary: string
  input_hash: string
  base_payload: Record<string, unknown>
  base_revisions: Array<{ entity_type: string; id: string; revision: number }>
  proposed_payload: { proposal?: Record<string, unknown> }
  diff: Array<{ path: string; before: string; after: string; unified: string }>
  raw_output: string
  log: string
  error?: string
  command_info: { adapter?: string; transport?: string; security_profile?: string; executable?: string; arguments?: string[] }
  provider_version?: string
  provider_adapter: 'codex' | 'kimi'
  executable_path: string
  executable_fingerprint: string
  timeout_seconds: number
  model: string
  retry_of?: string
  attempt: number
  cancel_requested: boolean
  recoverable: boolean
  confirmed_by?: string
  created_at: string
  updated_at: string
  started_at?: string
  completed_at?: string
  applied_at?: string
}

export type ProjectSummary = {
  id: string
  title: string
  episode: string
  logline: string
  target_duration: number
  created_at: string
  shot_count: number
  final_count: number
  active: boolean
  archived?: boolean
  archived_at?: string
}

export type WorkbenchProject = ProjectSummary & {
  planned_seconds: number
  job_count: number
  active_job_count: number
  candidate_count: number
  thumbnail?: string
  updated_at: string
  phase: '筹备中' | '制作中' | '生成中' | '已完成' | '已归档'
  category: 'planning' | 'production' | 'completed' | 'archived'
  progress: number
}

export type WorkbenchActivity = {
  id: string
  item_type: 'generation' | 'export'
  kind: string
  state: string
  message: string
  created_at: string
  updated_at: string
  shot_id?: string
  title: string
  project_id: string
  project_title: string
}

export type StorageStatus = {
  runtime_root: string
  asset_root: string
  export_root: string
  managed_bytes: number
  disk_total_bytes: number
  disk_free_bytes: number
}

export type Workbench = {
  summary: {
    project_count: number
    production_count: number
    archived_count: number
    pending_generation: number
    pending_review: number
  }
  projects: WorkbenchProject[]
  activities: WorkbenchActivity[]
  queue: WorkbenchActivity[]
  storage: StorageStatus
}

export type ProjectArchive = {
  id: string
  project_id: string
  revision: number
  state: 'ready' | 'invalid'
  size_bytes: number
  checksum_sha256: string
  created_at: string
  verified_at?: string
  row_counts: Record<string, number>
  media_count: number
  omitted_count: number
  download_url: string
}

export type AcceptanceStage = {
  id: string
  label: string
  status: 'pass' | 'warn' | 'block'
  evidence: string
  action: string
}

export type ProductionAcceptance = {
  project: { id: string; title: string; episode: string }
  status: 'deliverable' | 'production_ready' | 'blocked'
  generation: { ready: boolean; stages: AcceptanceStage[] }
  delivery: {
    ready: boolean
    stages: AcceptanceStage[]
    signoffs: Record<string, { id: string; category: string; revision: number; decision: 'pass' | 'reject'; note: string; source: string; created_at: string }>
  }
  latest_run?: { id: string; status: string; report_hash: string; created_at: string }
  generated_at: string
}

export type WorkspaceSettings = {
  sidebar_collapsed: boolean
  default_landing_page: 'projects' | 'activity' | 'global-queue'
  density: 'comfortable' | 'compact'
  comfyui_url: string
  default_export_width: number
  default_export_height: number
  polish_audio: boolean
  runtime: StorageStatus & {
    comfyui_root: string
    h3_project_root: string
    h3_script: string
    h3_adapter: boolean
  }
}

export type BibleEntryType = 'character' | 'location' | 'prop' | 'style' | 'voice'

export type BibleAsset = Asset & {
  bible_role: string
  bible_ordinal: number
}

export type BibleShotLink = {
  shot_id: string
  role: string
  note: string
  ordinal: number
  title: string
  scene_code: string
}

export type BibleEntry = {
  id: string
  project_id: string
  entry_type: BibleEntryType
  name: string
  summary: string
  canonical_description: string
  prompt_fragment: string
  negative_prompt: string
  continuity_rules: string
  apply_globally: boolean
  status: 'draft' | 'locked'
  revision: number
  archived: boolean
  created_at: string
  updated_at: string
  source_type?: 'manual' | 'creative_character'
  source_id?: string
  source_revision?: number
  assets: BibleAsset[]
  shots: BibleShotLink[]
  version_count: number
}

export type BibleShotCoverage = {
  id: string
  ordinal: number
  scene_code: string
  title: string
  status: string
  entries: Array<{ id: string; name: string; entry_type: BibleEntryType; status: 'draft' | 'locked' }>
  locked_count: number
  attention: string
}

export type BibleWorkspace = {
  project: { id: string; title: string; episode: string }
  entries: BibleEntry[]
  shots: BibleShotCoverage[]
  summary: {
    entry_count: number
    locked_count: number
    global_count: number
    covered_shot_count: number
    shot_count: number
  }
}

export type BibleVersion = {
  id: string
  entry_id: string
  revision: number
  status: 'draft' | 'locked'
  source: string
  snapshot: BibleEntry
  created_at: string
}

export type PromptPlanStatus = 'preview' | 'validated' | 'approved'

export type PromptPlanReference = {
  id: string
  asset_id: string
  asset_name: string
  media_type: 'image' | 'video' | 'audio'
  role: string
  tag: string
  audio_tag?: string
  source: 'shot' | 'bible'
  source_label: string
  checksum_sha256?: string
}

export type PromptPlan = {
  shot: Pick<Shot, 'id' | 'ordinal' | 'scene_code' | 'title' | 'description' | 'dialogue' | 'sound' | 'prompt' | 'width' | 'height' | 'seconds' | 'candidate_count' | 'strategy'>
  plan_hash: string
  ready: boolean
  status: PromptPlanStatus
  validated_at?: string
  approved_at?: string
  stale: boolean
  stale_reasons: string[]
  stale_plan_hash?: string
  superseded_at?: string
  mode: 'FL2VA' | 'REF2VA'
  sections: Array<{ id: string; label: string; content: string }>
  compiled_prompt: string
  references: PromptPlanReference[]
  reference_counts: { image: number; video: number; audio: number }
  bible: Array<{
    id: string
    entry_type: BibleEntryType
    name: string
    revision: number
    apply_globally: boolean
    asset_ids: string[]
    source_type?: 'manual' | 'creative_character'
    source_id?: string
    source_revision?: number
  }>
  storyboard_source?: {
    section_id: string
    section_title: string
    last_synced_revision: number
    current_section_revision: number
  }
  blocking: string[]
  warnings: string[]
  spec: {
    resolution: string
    requested_seconds: number
    adapter_seconds: number
    frames: number
    actual_seconds: number
    candidate_count: number
    steps: number
    fps: number
  }
  message?: string
  gpu_submitted?: boolean
}

export type BatchGenerationResult = {
  ok: boolean
  requested_count: number
  passed_count?: number
  submitted_count?: number
  failed_count: number
  gpu_submitted?: boolean
  results: Array<{
    shot_id: string
    title: string
    ok: boolean
    mode?: string
    state?: string
    resolution?: string
    candidate_count?: number
    prompt_ids?: string[]
    message: string
  }>
}

export type RoughCut = {
  available: boolean
  name?: string
  video?: string
  subtitles?: string
  captions?: string
  sources?: string
  manifest?: string
  width?: number
  height?: number
  duration_seconds?: number
  has_audio?: boolean
  shot_count?: number
  size_bytes?: number
  updated_at?: string
  quality_note?: string
  run?: ExportRun
}

export type ExportSource = {
  shot_id: string
  ordinal: number
  title: string
  source_type: 'candidate' | 'promotion'
  source_id: string
  source_detail?: string
  duration_seconds: number
  width: number
  height: number
  has_audio: boolean
}

export type ExportPreflight = {
  ready: boolean
  project_id: string
  project_title: string
  shot_count: number
  ready_shot_count: number
  duration_seconds: number
  source_policy: 'selected_only'
  delivery_plan?: { id: string; revision: number; plan_hash: string; status: 'locked' } | null
  sources: ExportSource[]
  issues: Array<{ shot_id: string; title: string; message: string }>
}

export type DeliveryPlanItem = {
  plan_id?: string
  shot_id: string
  ordinal: number
  subtitle_enabled: boolean
  subtitle_start_seconds?: number
  transition: 'cut'
  title: string
  scene_code: string
  dialogue: string
  seconds: number
  shot_status: string
}

export type DeliveryWorkspace = {
  project: { id: string; title: string; episode: string }
  plan: {
    id?: string
    project_id: string
    status: 'draft' | 'locked'
    revision: number
    plan_hash: string
    created_at?: string
    updated_at?: string
    locked_at?: string
    items: DeliveryPlanItem[]
  }
  versions: Array<{ id: string; revision: number; status: 'draft' | 'locked'; plan_hash: string; source: string; created_at: string }>
  summary: { item_count: number; subtitle_count: number; planned_seconds: number }
}

export type ExportRun = {
  id: string
  project_id: string
  state: '排队中' | '恢复排队' | '导出中' | '取消中' | '已完成' | '失败' | '已取消'
  message: string
  output_name: string
  width: number
  height: number
  polish_audio: number
  config: Record<string, unknown>
  outputs: Record<string, unknown>
  created_at: string
  updated_at: string
  started_at?: string
  completed_at?: string
  error?: string
  attempt: number
  parent_run_id?: string
  recovery_count: number
  cancel_requested: boolean
  is_current: boolean
  worker_id?: string
  log_available: boolean
  can_cancel: boolean
  can_retry: boolean
  can_activate: boolean
  events?: ExportEvent[]
}

export type ExportEvent = {
  id: number
  level: 'info' | 'warning' | 'error'
  event: string
  message: string
  created_at: string
}

export type Asset = {
  id: string
  kind: string
  name: string
  description: string
  preview: string
  locked: number
  source: 'mock' | 'managed'
  source_path?: string
  content_url?: string
  mime_type?: string
  media_type?: 'image' | 'video' | 'audio'
  size_bytes?: number
  checksum_sha256?: string
  width?: number
  height?: number
  duration_seconds?: number
  has_audio?: number
  created_at?: string
  bindable: boolean
}

export type ShotReference = {
  id: string
  shot_id: string
  asset_id: string
  reference_type: 'image' | 'video' | 'audio'
  ordinal: number
  role: string
  tag: string
  audio_tag?: string
  asset: Asset
}

export type DryRunResult = {
  ok: boolean
  state: string
  message: string
  h3_project: string
  mode: 'FL2VA' | 'REF2VA'
  reference_counts: Record<'image' | 'video' | 'audio', number>
  references: Array<{
    reference_id: string
    asset_id: string
    asset_name: string
    media_type: 'image' | 'video' | 'audio'
    role: string
    tag: string
    audio_tag?: string
  }>
  compiled_prompt: string
  resolution: string
  requested_seconds: number
  adapter_seconds: number
  candidate_count: number
  gpu_submitted: false
}

export type Candidate = {
  id: string
  shot_id: string
  label: string
  seed: string
  created_at: string
  thumbnail: string
  video?: string
  selected: number
  scores: Record<string, number>
  note: string
  status: 'queueing' | 'queued' | 'running' | 'completed' | 'error'
  source: 'mock' | 'h3'
  external_id?: string
  prompt_id?: string
  output_file?: string
  elapsed_seconds?: number
  metadata?: Record<string, unknown>
}

export type CandidateReview = {
  id?: string
  candidate_id: string
  status: 'pending' | 'reviewed' | 'stale'
  revision: number
  decision: 'pass' | 'needs_changes' | 'reject' | null
  scores: Partial<Record<'story_match' | 'continuity' | 'action' | 'visual_quality' | 'audio_quality', number>>
  audio_checks: {
    dialogue_match?: 'pending' | 'pass' | 'fail' | 'not_applicable'
    lip_sync?: 'pending' | 'pass' | 'fail' | 'not_applicable'
    ambience?: 'pending' | 'pass' | 'fail'
  }
  issues: string[]
  note: string
  watched_seconds: number
  media_probe?: {
    ok: boolean
    duration_seconds?: number
    size_bytes?: number
    bit_rate?: number
    video?: { codec?: string; width?: number; height?: number; frame_rate?: string }
    audio?: { present: boolean; codec?: string; channels?: number; sample_rate?: number }
    audio_analysis?: {
      ok: boolean
      mean_volume_db?: number
      max_volume_db?: number
      silence_seconds: number
      silence_ratio: number
      issues: string[]
    }
    issues?: string[]
  }
  stale: boolean
  can_select: boolean
  created_at?: string
}

export type ReviewWorkspace = {
  shot_id: string
  reviews: CandidateReview[]
  summary: {
    candidate_count: number
    passed_count: number
    needs_changes_count: number
    rejected_count: number
    pending_count: number
  }
}

export type Job = {
  id: number
  shot_id: string
  title: string
  kind: string
  state: string
  message: string
  created_at: string
  updated_at?: string
  completed_at?: string
  h3_project?: string
  prompt_ids: string[]
  retry_safe?: number
  reconciliation_snapshot?: string
}

export type ProductionBatchItem = {
  id: string
  batch_id: string
  shot_id: string
  ordinal: number
  title: string
  state: 'queued' | 'submitting' | 'running' | 'completed' | 'failed' | 'cancelled'
  plan_hash: string
  plan_snapshot: PromptPlan
  attempts: number
  max_attempts: number
  h3_project?: string
  prompt_ids: string[]
  message: string
  error?: string
  created_at: string
  updated_at: string
  started_at?: string
  completed_at?: string
}

export type ProductionBatchEvent = {
  id: number
  batch_id: string
  item_id?: string
  event: string
  level: 'info' | 'warning' | 'error'
  message: string
  created_at: string
}

export type ProductionBatch = {
  id: string
  project_id: string
  name: string
  state: 'running' | 'paused' | 'cancelling' | 'completed' | 'completed_with_errors' | 'cancelled'
  item_count: number
  submitted_count: number
  completed_count: number
  failed_count: number
  cancelled_count: number
  config: { concurrency: number; candidate_total: number; snapshot_policy: string }
  message: string
  created_at: string
  updated_at: string
  started_at?: string
  completed_at?: string
  items: ProductionBatchItem[]
  events: ProductionBatchEvent[]
}

export type ScriptAgentProvider = {
  id: 'codex' | 'kimi'
  label: string
  available: boolean
  version?: string
  executable?: string
}

export type ScriptScene = {
  id: string
  document_id: string
  parent_id: string
  section_type: 'scene'
  ordinal: number
  code: string
  title: string
  summary: string
  goal: string
  conflict: string
  turning_point: string
  hook: string
  content: string
  planned_seconds: number
  tension: number
  status: 'draft' | 'approved'
  updated_at: string
}

export type ScriptAct = Omit<ScriptScene, 'section_type' | 'parent_id'> & {
  section_type: 'act'
  parent_id: null
  scenes: ScriptScene[]
}

export type ScriptDocument = {
  id: string
  project_id: string
  title: string
  summary: string
  version: number
  status: 'draft' | 'approved'
  total_seconds: number
  created_at: string
  updated_at: string
}

export type ScriptAgentRun = {
  id: string
  document_id: string
  scope: 'episode' | 'act' | 'scene'
  target_id?: string
  provider: 'codex' | 'kimi'
  instruction: string
  state: 'queued' | 'running' | 'completed' | 'failed' | 'applied' | 'rejected'
  message: string
  base_payload: Record<string, unknown>
  proposed_payload: Record<string, unknown>
  raw_output: string
  error?: string
  created_at: string
  updated_at: string
  completed_at?: string
  applied_at?: string
}

export type ScriptVersion = {
  id: string
  version: number
  status: 'draft' | 'approved'
  source: string
  created_at: string
}

export type StoryboardSyncAction = 'create' | 'update' | 'unchanged' | 'protected' | 'preserve'
export type StoryboardSyncFieldDecision = 'create' | 'update' | 'keep' | 'protected'

export type StoryboardSyncFieldDiff = {
  field: 'ordinal' | 'scene_code' | 'title' | 'description' | 'dialogue' | 'seconds' | 'prompt'
  label: string
  before: string | number | null
  after: string | number | null
  changed: boolean
  decision: StoryboardSyncFieldDecision
  note: string
}

export type StoryboardSyncShotFields = {
  ordinal: number
  scene_code: string
  title: string
  description: string
  dialogue: string
  seconds: number
  prompt: string
  status?: string
}

export type StoryboardSyncRow = {
  action: StoryboardSyncAction
  shot_id?: string
  scene_id?: string
  scene_code?: string
  scene_title?: string
  act_code?: string
  act_title?: string
  target_ordinal?: number
  match_reason: 'linked' | 'title' | 'ordinal' | 'new' | 'unmatched'
  current?: StoryboardSyncShotFields
  proposed?: StoryboardSyncShotFields
  changes: string[]
  field_diffs: StoryboardSyncFieldDiff[]
  protected_reasons: string[]
  dependencies: Record<string, number>
}

export type StoryboardSyncSummary = Record<StoryboardSyncAction, number>

export type StoryboardSyncPreview = {
  document_id: string
  project_id: string
  script_version: number
  plan_hash: string
  summary: StoryboardSyncSummary
  rows: StoryboardSyncRow[]
  can_apply: boolean
  safety: {
    existing_prompts_preserved: boolean
    production_shots_protected: boolean
    unmatched_shots_preserved: boolean
  }
  review_mode?: 'preview' | 'history'
  applied_at?: string
}

export type StoryboardSyncRecord = {
  id: string
  document_id: string
  project_id: string
  script_version: number
  state: 'applied' | 'partial'
  plan_hash: string
  summary: StoryboardSyncSummary
  plan: StoryboardSyncRow[]
  created_at: string
  applied_at: string
}

export type ScriptWorkspace = {
  document: ScriptDocument
  acts: ScriptAct[]
  providers: ScriptAgentProvider[]
  agent_runs: ScriptAgentRun[]
  versions: ScriptVersion[]
  latest_storyboard_sync?: StoryboardSyncRecord
}

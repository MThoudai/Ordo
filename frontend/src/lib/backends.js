import { pollEvaluation } from './evaluationJobs.js'
const DEFAULT_BACKENDS = {
  python: {
    id: 'python',
    label: 'Python',
    baseUrl: runtimeConfig().pythonApiUrl || import.meta.env?.VITE_PYTHON_API_URL || '/api',
    port: '18000'
  }
}

export function createInitialSettings() {
  const saved = readSettings()
  const settings = {
    backend: 'python',
    apiToken: '',
    userId: saved.userId || createUserId(),
    conversationId: saved.conversationId || '',
    endpoints: {
      python: saved.endpoints?.python || DEFAULT_BACKENDS.python.baseUrl
    }
  }
  saveSettings(settings)
  return settings
}

export function createUserId() {
  const random = globalThis.crypto?.randomUUID?.().replaceAll('-', '')
    || `${Math.random().toString(16).slice(2)}${Date.now().toString(16)}`
  return `p_${random.slice(0, 4)}`
}

export function saveSettings(settings) {
  localStorage.setItem('ordo.frontend.settings', JSON.stringify({ ...settings, apiToken: undefined }))
}

export function backendMeta(type, settings) {
  const meta = DEFAULT_BACKENDS[type] || DEFAULT_BACKENDS.python
  return {
    ...meta,
    baseUrl: normalizeBaseUrl(settings.endpoints[type] || meta.baseUrl)
  }
}

export async function requestHealth(type, settings) {
  return requestApi(type, settings, '/health')
}

export async function requestMonitor(type, settings) {
  return requestApi(type, settings, '/monitor')
}

export async function requestSkills(type, settings) {
  return requestApi(type, settings, '/skills')
}

export async function reloadSkills(type, settings) {
  return requestApi(type, settings, '/skills/reload', { method: 'POST' })
}

export async function requestKnowledgeStats(type, settings) {
  return requestApi(type, settings, '/knowledge/stats')
}

export async function runEvaluation(type, settings, body = null, progressOptions = {}) {
  const initial = await requestApi(type, settings, '/eval/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  })
  if (!initial?.job_id) return initial
  return pollEvaluation(initial, id => requestApi(type, settings, `/eval/jobs/${encodeURIComponent(id)}`), progressOptions)
}

export async function requestSearch(type, settings, query, topK = 5, rewrite = false) {
  const params = new URLSearchParams({ query, top_k: String(topK), rewrite: String(rewrite) })
  return requestApi(type, settings, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(type, settings, message, clientRequestId = createRequestId()) {
  const meta = backendMeta(type, settings)
  const payload = { ...buildChatPayload(type, settings, message), client_request_id: clientRequestId }
  const raw = await requestApi(type, settings, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  return normalizeChatResponse(type, raw)
}

export async function requestToolTrace(type, settings, requestId) {
  if (!requestId) return null
  const raw = await requestApi(type, settings, `/trace/tool/${encodeURIComponent(requestId)}`)
  return normalizeToolTraceResponse(raw)
}

export async function addKnowledge(type, settings, documents) {
  return requestApi(type, settings, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents })
  })
}

export async function uploadKnowledge(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestApi(type, settings, '/knowledge/upload', {
    method: 'POST',
    body: form
  })
}

function buildChatPayload(type, settings, message) {
  return {
    message,
    user_id: settings.userId || 'anonymous',
    conv_id: settings.conversationId || undefined
  }
}

function normalizeChatResponse(type, raw) {
  return {
    backend: type,
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    requestId: raw.request_id || raw.requestId || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || raw.intentGroup || 'other',
    agentType: raw.agent_type || raw.agentType || '',
    agentTypes: raw.agent_types || raw.agentTypes || [],
    primaryAgent: raw.primary_agent || raw.primaryAgent || '',
    supportingAgents: raw.supporting_agents || raw.supportingAgents || [],
    routingReason: raw.routing_reason || raw.routingReason || '',
    routingConfidence: Number(raw.routing_confidence ?? raw.routingConfidence ?? 0),
    entities: raw.entities || {},
    intentConfidence: Number(raw.intent_confidence ?? raw.intentConfidence ?? 0),
    intentSourceScores: raw.intent_source_scores || raw.intentSourceScores || {},
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? raw.latencyMs ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used ?? raw.knowledgeUsed),
    status: raw.status || 'success',
    retryable: Boolean(raw.retryable),
    errorCode: raw.error_code || '',
    verified: raw.verified,
    grounded: raw.grounded,
    raw
  }
}

function normalizeToolTraceResponse(raw) {
  const trace = raw?.trace || {}
  const requestId = raw?.request_id || raw?.requestId || trace.request_id || ''
  return {
    requestId,
    found: Boolean(raw?.found),
    trace: {
      ...trace,
      requestId,
      toolsUsed: trace.tools_used || trace.toolsUsed || [],
      toolCalls: trace.tool_calls || trace.toolCalls || []
    },
    raw
  }
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    const error = new Error(`${response.status} ${response.statusText}: ${detail}`)
    error.status = response.status
    error.data = data
    throw error
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem('ordo.frontend.settings') || '{}')
    return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {}
  } catch {
    return {}
  }
}

function runtimeConfig() {
  if (typeof window === 'undefined') return {}
  return window.__ORDO_CONFIG__ || {}
}

export function createRequestId() {
  return globalThis.crypto?.randomUUID?.() || `ordo-request-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function requestApi(type, settings, path, options = {}) {
  const headers = { ...options.headers }
  if (settings.apiToken) headers.Authorization = `Bearer ${settings.apiToken}`
  return requestJson(backendMeta(type, settings).baseUrl, path, { ...options, headers })
}

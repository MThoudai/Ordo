export const SUPPORT_ROLES = {
  general: '通用支持', collaboration: '协作支持', technical: '技术支持', escalation: '沟通摘要'
}

export const STARTER_PROMPTS = [
  { label: '了解助手', question: 'Ordo 可以帮助我解决哪些问题？' },
  { label: '创建项目', question: '我是 member，如何创建一个私有项目？' },
  { label: '成员权限', question: 'guest 可以编辑项目文档吗？' },
  { label: 'API 排错', question: 'API 返回 403 时，应该检查哪些条件？' },
  { label: '保存故障', question: '文档提示保存失败，我还没有刷新页面，应该先做什么？' }
]

export const SINGLE_TURN_CASES = [
  { question: '这个平台能做什么？', intent: 'product_overview' },
  { question: '如何创建一个私有项目？', intent: 'onboarding' },
  { question: 'guest 可以编辑项目文档吗？', intent: 'enterprise_mgmt' },
  { question: 'API 返回 403 时，应该检查哪些条件？', intent: 'api_error' }
]

export function buildEvaluationPayload() {
  const run = `frontend-eval-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`}`
  return {
    intent_cases: SINGLE_TURN_CASES.map(item => ({ message: item.question, expected_intent: item.intent })),
    dialog_cases: SINGLE_TURN_CASES.map((item, index) => ({
      question: item.question, user_id: `${run}-${index}`, conv_id: `${run}-${index}`
    }))
  }
}

export function agentLabel(type) { return SUPPORT_ROLES[type] || type || '待识别' }

export function intentLabel(intent) {
  return ({ technical_issue: '平台技术故障', product_overview: '平台概述与入门咨询', feature_inquiry: '功能说明', onboarding: '上手指导', enterprise_mgmt: '成员与项目权限',
    permission_request: '访问申请咨询', api_integration: 'API 接入', api_error: 'API 排错',
    api_quota: 'API 额度', ticket_query: '工单咨询', human_handoff: '人工支持请求',
    greeting: '问候', feedback: '反馈', other: '其他咨询' })[intent] || intent || '待识别'
}

export function toolStatus(call) {
  if (call.success === false || call.result_success === false) return '调用失败'
  if (call.result_success === true) return '结果已返回'
  return call.success === true ? '调用完成' : '状态未知'
}

export function evidenceItems(trace) {
  const seen = new Set()
  return (trace?.toolCalls || [])
    .filter(call => call.success !== false && call.result_success !== false)
    .flatMap(call => call.evidence || []).filter(item => {
    const key = item.chunk_id || `${item.source}:${item.version}:${item.document_id}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export function sourceItems(trace) {
  const seen = new Set()
  return evidenceItems(trace).filter(item => {
    const key = `${item.source}:${item.version}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export function scorePercent(value) {
  const score = Number(value)
  return Number.isFinite(score) ? Math.min(100, Math.max(0, score * 100)) : 0
}

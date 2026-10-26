<template>
  <main :class="['app-shell', `app-shell-${activeView}`]">
    <header class="topbar">
      <a class="brand" href="#" aria-label="Ordo 首页" @click.prevent="activeView = 'chat'">
        <span class="brand-mark">O</span><span class="brand-name">Ordo</span>
      </a>
      <nav class="view-nav" aria-label="工作区">
        <button :class="{ active: activeView === 'chat' }" @click="activeView = 'chat'">对话</button>
        <button :class="{ active: activeView === 'knowledge' }" @click="activeView = 'knowledge'">知识库</button>
        <button :class="{ active: activeView === 'evaluation' }" @click="activeView = 'evaluation'">评测</button>
      </nav>
      <div class="topbar-tools">
        <span class="environment-pill"><i :class="healthOk ? 'online' : 'offline'"></i>{{ healthLabel }}</span>
        <span class="avatar-button" title="浏览器会话用户">U</span>
      </div>
    </header>
    <div v-if="toast" class="toast" role="status">{{ toast }}</div>

    <section v-if="activeView === 'chat'" class="page page-chat">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">团队协作 · 智能支持</span>
          <h1>Ordo 智能支持中心</h1>
          <p>帮助理解协作平台功能、成员权限与 API 使用问题。</p>
        </div>
        <div class="heading-actions">
          <span class="session-label">{{ settings.conversationId ? '对话进行中' : '新对话' }}</span>
          <button class="quiet-button" :disabled="busy" @click="clearConversation">新建对话</button>
        </div>
      </div>
      <div class="chat-layout">
        <section class="chat-stage" aria-label="支持对话">
          <div class="stage-bar">
            <div class="stage-context"><span class="context-dot"></span><span>功能上手 · 成员权限 · API 支持</span></div>
            <span>{{ messages.length }} 条消息</span>
          </div>
          <div class="messages" ref="messageList" role="log" aria-live="polite" :aria-busy="chatBusy">
            <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
              <div class="message-meta">
                <span>{{ item.role === 'user' ? '你' : 'Ordo' }}</span>
                <small v-if="item.meta">{{ item.meta }}</small>
              </div>
              <div v-if="item.role === 'assistant' && !item.isError" class="markdown-body" v-html="renderMarkdown(item.content)"></div>
              <p v-else>{{ item.content }}</p>
              <details v-if="sourceItems(item.trace).length" class="message-sources">
                <summary>检索资料来源 · {{ sourceItems(item.trace).length }} 份</summary>
                <ul><li v-for="source in sourceItems(item.trace)" :key="source.source + source.version">{{ sourceName(source.source) }} <small>{{ source.version }}</small></li></ul>
              </details>
              <small v-if="item.traceState === 'loading'" class="trace-note">检索与处理详情加载中…</small>
              <small v-else-if="item.traceState === 'unavailable'" class="trace-note">处理详情暂不可用，回答已保留。</small>
            </article>
            <div v-if="chatBusy" class="reply-pending" role="status">正在理解问题并整理回答…</div>
            <div v-if="messages.length === 0" class="empty-state">
              <div class="empty-symbol">✦</div>
              <h2>从一个协作问题开始</h2>
              <p>了解功能、梳理权限，或一起分析 API 报错。</p>
              <div class="starter-prompts">
                <button v-for="item in STARTER_PROMPTS" :key="item.label" @click="usePrompt(item.question)">{{ item.label }}</button>
              </div>
              <p class="capability-note">提供规则解释与操作指导；实时业务查询、代操作及人工接通暂不支持。</p>
            </div>
          </div>
          <form class="composer" @submit.prevent="sendMessage">
            <textarea v-model="draft" rows="3" aria-label="输入问题" placeholder="描述你想了解的功能或遇到的问题，可附脱敏提示…"
              @keydown.meta.enter.prevent="sendMessage" @keydown.ctrl.enter.prevent="sendMessage"></textarea>
            <div class="composer-bottom"><span>⌘ / Ctrl + Enter 发送</span><button v-if="pendingRetry" type="button" :disabled="busy" @click="sendMessage(true)">重试上次请求</button><button type="submit" :disabled="busy || !draft.trim()">{{ chatBusy ? '正在回答' : '发送' }}</button></div>
          </form>
        </section>

        <aside class="chat-sidebar">
          <div class="chat-sidebar-scroll">
            <section class="side-card trace-card">
              <div class="card-heading"><div><span class="kicker">处理概览</span><h2>最近一次回答</h2></div><span class="trace-status" :class="{ 'has-data': lastResponse }"></span></div>
              <div v-if="lastResponse" class="trace-body">
                <div class="latency"><span>响应耗时</span><strong>{{ lastResponse.latencyMs }}<small> ms</small></strong></div>
                <dl class="detail-list">
                  <div><dt>主要支持角色</dt><dd>{{ agentLabel(lastResponse.primaryAgent || lastResponse.agentType) }}</dd></div>
                  <div><dt>辅助支持角色</dt><dd>{{ lastResponse.supportingAgents.map(agentLabel).join('、') }}</dd></div>
                  <div><dt>问题类型</dt><dd>{{ intentLabel(lastResponse.intent) }}</dd></div>
                  <div><dt>路由匹配分</dt><dd>{{ formatScore(lastResponse.routingConfidence) }}</dd></div>
                  <div><dt>资料检索</dt><dd>{{ retrievalLabel }}</dd></div>
                  <div><dt>沟通摘要流程</dt><dd>{{ lastResponse.escalated ? '已进入（未接通人工）' : '未进入' }}</dd></div>
                </dl>
              </div>
              <p v-else class="side-empty">发送问题后，这里会显示支持角色、资料检索与耗时。</p>
              <details v-if="lastResponse" class="advanced-details">
                <summary>查看处理详情</summary>
                <p class="muted small-copy">角色仅参与解释与指导，工具调用完成不代表业务操作已办理。</p>
                <dl class="detail-list"><div><dt>角色标识</dt><dd>{{ lastResponse.primaryAgent }}</dd></div><div><dt>请求标识</dt><dd>{{ lastResponse.requestId }}</dd></div></dl>
                <p v-if="lastResponse.routingReason" class="routing-reason">{{ lastResponse.routingReason }}</p>
                <p v-if="traceLoading" class="side-empty">正在获取处理详情…</p>
                <p v-else-if="!lastTrace?.found" class="side-empty">没有取得本次处理详情。</p>
                <div v-else class="trace-calls">
                  <details v-for="(call, index) in lastTrace.trace.toolCalls" :key="call.tool_use_id || index">
                    <summary><strong>{{ call.tool_name }}</strong><span>{{ toolStatus(call) }}</span></summary>
                    <p class="small-copy">{{ call.latency_ms || 0 }} ms</p>
                    <pre>{{ formatJson({ input: call.input, error: call.error || undefined, diagnostics: call.diagnostics }) }}</pre>
                  </details>
                  <p v-if="!lastTrace.trace.toolCalls.length" class="side-empty">本次没有记录到工具调用。</p>
                </div>
              </details>
            </section>
            <section class="side-card monitor-card">
              <div class="card-heading"><div><span class="kicker">运行概览</span><h2>问答服务</h2></div><button class="link-button" @click="loadMonitor">刷新</button></div>
              <div class="mini-stats">
                <div><strong>{{ monitorLoaded ? totalRequests : '—' }}</strong><span>处理次数</span></div>
                <div><strong>{{ monitorLoaded ? agentCount : '—' }}</strong><span>问答角色</span></div>
                <div><strong>{{ monitorLoaded ? activeAlerts.length : '—' }}</strong><span>运行告警</span></div>
              </div>
              <p class="small-copy muted">三类问答角色 · 独立的确定性沟通摘要流程</p>
              <p v-if="!monitorLoaded" class="side-empty">运行状态暂未取得。</p>
              <div v-else-if="activeAlerts.length"><p v-for="alert in activeAlerts" :key="alert.metric" class="alert-note">{{ alert.message || alert.detail || alert.title || '运行指标异常' }}</p></div>
              <p v-else class="healthy-note">当前没有活跃运行告警。</p>
            </section>
            <details class="side-card connection-card">
              <summary class="fold-heading">会话与连接设置</summary>
              <p class="small-copy muted">用户 ID 用于区分用户及其会话，保存在当前浏览器，不代表平台账号或成员权限。新建对话不会删除服务端历史。</p>
              <label><span>用户 ID</span><input v-model="userIdDraft" :disabled="busy" placeholder="例如 u1001、test01" /></label>
              <button class="quiet-button" :disabled="busy" @click="applyUserId">应用用户 ID 并新建对话</button>
              <label><span>当前会话 ID（自动生成）</span><input :value="settings.conversationId || '发送后生成'" readonly /></label>
              <details class="advanced-connection-settings">
                <summary class="fold-heading">高级设置</summary>
                <label><span>接口访问 Token（可选）</span><input v-model="settings.apiToken" type="password" :disabled="busy" autocomplete="off" /></label>
                <p class="small-copy muted">仅在后端开启接口鉴权时填写，未开启则留空。此凭证用于访问助手接口，不是模型 API Key，也不代表平台成员权限。仅保存在本页内存，刷新后清空。</p>
                <p class="small-copy muted">服务地址：{{ currentBackend.baseUrl }}</p>
                <a class="settings-docs-link" :href="docsUrl" target="_blank" rel="noreferrer">后端接口文档</a>
              </details>
              <div class="side-actions"><button @click="checkHealth">检查服务</button><button class="quiet-button" @click="refreshConsole">刷新运行状态</button></div>
              <p class="small-copy muted">检查服务可用性；刷新运行状态会同时更新知识库、角色规范和运行统计。两项操作均不调用模型或清空对话。</p>
            </details>
          </div>
        </aside>
      </div>
    </section>

    <section v-else-if="activeView === 'knowledge'" class="page page-knowledge">
      <div class="page-heading">
        <div class="heading-copy"><span class="kicker">产品资料</span><h1>知识库</h1><p>查阅功能操作、成员权限和 API 规范，核对回答依据。</p><small class="collection-label">当前集合：{{ knowledgeCollection || '读取中' }}</small></div>
        <div class="count-display"><strong>{{ knowledgeCount }}</strong><span>知识片段</span></div>
      </div>
      <div class="knowledge-layout single-column">
        <section class="workspace-card search-workspace">
          <div class="card-heading"><div><span class="kicker">资料检索</span><h2>查找相关说明</h2></div></div>
          <div class="search-line"><input v-model="searchQuery" placeholder="例如：guest 可以编辑项目文档吗？" aria-label="知识检索问题" @keydown.enter="searchKnowledge" /><button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">{{ searchBusy ? '检索中' : '搜索' }}</button></div>
          <label class="checkbox-line"><input v-model="enhancedSearch" type="checkbox" :disabled="searchBusy" /><span>增强检索（调用模型进行改写与重排）</span></label>
          <p class="small-copy muted">默认直接检索，不调用模型。相似度只用于排序，不代表答案正确率。</p>
          <p v-if="searchError" class="inline-error" role="alert">{{ searchError }}</p>
          <div v-if="searchResults.length" class="result-list">
            <article v-for="(item, index) in searchResults" :key="item.chunk_id || index" class="result-item">
              <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
              <div><div class="result-title"><strong>{{ item.title || '未命名文档' }}</strong><small>相似度 {{ formatScore(item.score) }}</small></div>
                <p class="source-meta">{{ item.source || '来源未标注' }} · {{ item.version || '版本未标注' }}</p><p>{{ item.content }}</p>
              </div>
            </article>
          </div>
          <div v-else class="workspace-empty">{{ searchBusy ? '正在检索…' : searchCompleted ? '本次没有返回知识片段，可以换一种表达重试。' : '输入协作平台相关问题开始搜索。' }}</div>
        </section>
      </div>
      <section class="workspace-card skills-workspace">
        <div class="card-heading"><div><span class="kicker">回答规范</span><h2>角色规范</h2></div></div>
        <p class="small-copy muted">规范约束各角色的回答方式，不代表已连接业务查询或操作工具。</p>
        <div class="skill-table">
          <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item"><span class="skill-dot"></span><strong>{{ skill.name }}</strong><span>{{ skill.description }}</span><small>{{ skill.content_chars || 0 }} 字符</small></div>
          <div v-if="!skillsData.skills.length" class="workspace-empty">角色规范暂未取得。</div>
        </div>
      </section>
      <details class="workspace-card maintenance-panel">
        <summary class="fold-heading">知识维护</summary>
        <p class="small-copy muted">默认从仓库维护资料。这里可手动添加补充知识；不会自动更新仓库文件或清单版本。</p>
        <label><span>标题</span><input v-model="docTitle" placeholder="填写补充资料标题" /></label>
        <label><span>内容</span><textarea v-model="docContent" rows="7" placeholder="填写已核对的操作说明或排查资料"></textarea></label>
        <div class="side-actions"><button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">添加补充文档</button><label class="upload-button" :class="{ disabled: busy }">上传文件<input type="file" accept=".txt,.md,.json" :disabled="busy" @change="handleUpload" /></label><button class="quiet-button" :disabled="busy" @click="reloadSkillSet">重新加载角色规范</button></div>
      </details>
    </section>

    <section v-else class="page page-evaluation">
      <div class="page-heading"><div class="heading-copy"><span class="kicker">问答质量</span><h1>单轮问答评测</h1><p>检查 SaaS 场景的意图识别与回答质量。</p></div><button @click="runEvaluation" :disabled="busy">{{ evalBusy ? '评测运行中…' : '运行单轮评测' }}</button></div>
      <section class="workspace-card">
        <h2>本次范围</h2><p class="small-copy">4 条意图样例与 4 条独立单轮问答，覆盖功能说明、项目上手、成员权限和 API 排错。运行将调用模型生成回答和评分，并更新后端评测基线。</p>
        <ul class="evaluation-cases"><li v-for="item in SINGLE_TURN_CASES" :key="item.intent">{{ item.question }}</li></ul>
        <p class="small-copy muted">本入口不运行多轮、跨领域组合或故障注入。模型评分仅供参考，不等于人工业务验收结论。</p>
      </section>
      <p v-if="evalError" class="inline-error" role="alert">{{ evalError }}</p>
      <div v-if="evalData" class="evaluation-content">
        <p class="small-copy">评测状态：{{ evalData.status || 'complete' }} · {{ evalData.regression_note || '仅同一套题集及评分配置可比较' }}</p>
        <div class="evaluation-summary">
          <div class="score-hero"><span>模型评测通过率</span><strong>{{ evalData.valid_total === 0 ? '无有效评分' : formatPercent(evalData.pass_rate) }}</strong><small>{{ evalData.passed }} / {{ evalData.valid_total ?? evalData.total }} 个有效条目；评分失败 {{ evalData.evaluation_errors || 0 }} 条</small></div>
          <div><span>通过条目</span><strong>{{ evalData.passed }}</strong></div><div><span>总条目</span><strong>{{ evalData.total }}</strong></div><div><span>与后端基线相比的退化项</span><strong>{{ evalData.regressions?.length || 0 }}</strong></div>
        </div>
        <p class="small-copy muted">意图识别汇总为一个条目，其余为逐题回答评分。基线若来自不同测试范围，退化项不能直接用于比较优劣。</p>
        <div class="evaluation-layout">
          <section class="workspace-card"><div class="card-heading"><h2>平均评分（0～1）</h2></div><div class="score-list"><div v-for="(value, key) in evalData.avg_scores" :key="key"><span>{{ scoreLabel(key) }}</span><i><b :style="{ width: scorePercent(value) + '%' }"></b></i><strong>{{ formatScore(value) }}</strong></div></div></section>
          <section class="workspace-card"><div class="card-heading"><h2>评测建议</h2></div><div v-if="evalData.recommendations?.length" class="recommendations"><p v-for="(item, index) in evalData.recommendations" :key="index">{{ item }}</p></div><p v-else class="side-empty">本次没有额外建议。</p></section>
        </div>
        <section class="workspace-card evaluation-results"><h2>逐项结果</h2>
          <details v-for="item in evalData.results || []" :key="item.test_id" class="evaluation-result"><summary><strong>{{ evaluationTitle(item) }}</strong><span>{{ item.passed ? '达到评分阈值' : '未达到评分阈值' }}</span></summary><p>{{ item.detail }}</p><pre>{{ formatJson({ scores: item.scores, detail: item.metadata }) }}</pre></details>
        </section>
      </div>
      <div v-else class="evaluation-empty"><div class="empty-symbol">◎</div><h2>{{ evalBusy ? '正在评测' : '还没有评测结果' }}</h2><p>{{ evalBusy ? '模型正在生成并评估回答，请稍候。' : '确认上方范围后，可手动运行一次评测。' }}</p><p v-if="evalBusy && evalProgress">{{ evalProgress.status }} · {{ evalProgress.completed || 0 }} / {{ evalProgress.total || '—' }}</p></div>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import {
  addKnowledge, backendMeta, createInitialSettings, createUserId, createRequestId, reloadSkills, requestChat, requestHealth,
  requestKnowledgeStats, requestMonitor, requestSearch, requestToolTrace, requestSkills,
  runEvaluation as requestEvaluation, saveSettings, uploadKnowledge
} from './lib/backends'
import { createRequestState } from './lib/requestState'
import { STARTER_PROMPTS, SINGLE_TURN_CASES, SUPPORT_ROLES, agentLabel, intentLabel, toolStatus, evidenceItems, sourceItems, scorePercent, buildEvaluationPayload } from './lib/presentation'

const settings = reactive(createInitialSettings())
const requestState = createRequestState(settings)
const pendingRetry = ref(null)
const evalProgress = ref(null)
const activeView = ref('chat')
const messages = ref([])
const draft = ref('')
const userIdDraft = ref(settings.userId)
const chatBusy = ref(false)
const searchBusy = ref(false)
const maintenanceBusy = ref(false)
const evalBusy = ref(false)
const busy = computed(() => chatBusy.value || searchBusy.value || maintenanceBusy.value || evalBusy.value)
const healthOk = ref(false)
const healthLabel = ref('连接检查中')
const knowledgeCount = ref('—')
const knowledgeCollection = ref('')
const searchQuery = ref('guest 可以编辑项目文档吗？')
const enhancedSearch = ref(false)
const searchResults = ref([])
const searchCompleted = ref(false)
const searchError = ref('')
const docTitle = ref('')
const docContent = ref('')
const messageList = ref(null)
const monitorData = ref({ agent_stats: {}, active_alerts: [] })
const monitorLoaded = ref(false)
const skillsData = ref({ skills: [], errors: [] })
const lastResponse = ref(null)
const lastTrace = ref(null)
const traceLoading = ref(false)
const evalData = ref(null)
const evalError = ref('')
const toast = ref('')
let toastTimer
let messageSequence = 0
let sessionVersion = 0
const markdown = new MarkdownIt({ html: false, linkify: false, breaks: true })
const currentBackend = computed(() => backendMeta(settings.backend, settings))
const docsUrl = computed(() => currentBackend.value.baseUrl + '/docs')
const activeAlerts = computed(() => monitorData.value.active_alerts || [])
const agentCount = computed(() => new Set(Object.keys(monitorData.value.agent_stats || {}).map(key => key.replace(/_\d+$/, '')).filter(role => role !== 'escalation' && SUPPORT_ROLES[role])).size)
const totalRequests = computed(() => Object.values(monitorData.value.agent_stats || {}).reduce((sum, item) => sum + Number(item.total || 0), 0))
const retrievalLabel = computed(() => {
  const count = evidenceItems(lastTrace.value?.trace).length
  if (count) return '返回 ' + count + ' 个片段'
  return lastResponse.value?.knowledgeUsed ? '已请求检索' : '未请求检索'
})
onMounted(refreshConsole)
onBeforeUnmount(() => { sessionVersion += 1; requestState.invalidate(); clearTimeout(toastTimer) })
watch(() => settings.apiToken, () => { requestState.invalidate(); sessionVersion += 1; messages.value = []; settings.conversationId = ''; pendingRetry.value = null; lastTrace.value = null; lastResponse.value = null; evalData.value = null })
function persist() { saveSettings(settings) }
function snapshotSettings() { return { ...settings, endpoints: { ...settings.endpoints } } }
function renderMarkdown(text) { return markdown.render(String(text || '')) }
function sourceName(source) { return source?.split('/').pop() || '资料来源未标注' }
function formatScore(value) { return value != null && Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '—' }
function formatPercent(value) { return scorePercent(value).toFixed(1) + '%' }
function formatJson(value) { return JSON.stringify(value ?? {}, null, 2) }
function scoreLabel(key) { return ({ accuracy: '准确性 / 意图准确率', macro_f1: 'Macro-F1', relevance: '相关性', completeness: '完整性', helpfulness: '有用性', overall: '综合评分' })[key] || key }
function evaluationTitle(item) {
  if (item.test_id === 'intent_recognition') return '意图识别汇总'
  const match = /^dialog_(\d+)$/.exec(item.test_id || '')
  return match ? SINGLE_TURN_CASES[Number(match[1])]?.question || item.test_id : item.test_id
}
async function scrollMessages() { await nextTick(); messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' }) }
async function refreshConsole() { await Promise.allSettled([checkHealth(), loadStats(), loadMonitor(), loadSkills()]) }
async function checkHealth() {
  const current = requestState.begin('health')
  try { const data = await requestHealth(current.backend, current.settings); if (!current.isCurrent()) return; healthOk.value = data.status === 'ok'; healthLabel.value = healthOk.value ? '服务可用' : '服务未就绪' }
  catch { if (current.isCurrent()) { healthOk.value = false; healthLabel.value = '连接不可用' } }
}
async function loadStats() {
  const current = requestState.begin('stats')
  try { const data = await requestKnowledgeStats(current.backend, current.settings); if (!current.isCurrent()) return; knowledgeCount.value = data.total_chunks ?? '—'; knowledgeCollection.value = data.collection || '' }
  catch { if (current.isCurrent()) { knowledgeCount.value = '—'; knowledgeCollection.value = '暂未取得' } }
}
async function loadMonitor() {
  const current = requestState.begin('monitor')
  try { const data = await requestMonitor(current.backend, current.settings); if (!current.isCurrent()) return; monitorData.value = data; monitorLoaded.value = true }
  catch { if (current.isCurrent()) monitorLoaded.value = false }
}
async function loadSkills() {
  const current = requestState.begin('skills')
  try { const data = await requestSkills(current.backend, current.settings); if (current.isCurrent()) skillsData.value = data }
  catch { if (current.isCurrent()) skillsData.value = { skills: [], errors: [] } }
}
async function reloadSkillSet() {
  if (busy.value) return
  maintenanceBusy.value = true
  try { skillsData.value = await reloadSkills(settings.backend, settings); showToast('角色规范已重新加载') }
  catch { showToast('角色规范加载失败，请检查连接') }
  finally { maintenanceBusy.value = false }
}
async function sendMessage(retry = false) {
  const previous = retry === true ? pendingRetry.value : null
  const content = previous?.message || draft.value.trim()
  if (!content || busy.value) return
  const version = sessionVersion
  const requestSettings = snapshotSettings()
  const clientRequestId = previous?.clientRequestId || createRequestId()
  pendingRetry.value = { message: content, clientRequestId }
  if (!previous) messages.value.push({ id: createMessageId(), role: 'user', content })
  draft.value = ''
  chatBusy.value = true
  void scrollMessages()
  try {
    const response = await requestChat(requestSettings.backend, requestSettings, content, clientRequestId)
    if (version !== sessionVersion) return
    if (response.conversationId) { settings.conversationId = response.conversationId; persist() }
    lastResponse.value = response
    pendingRetry.value = null
    lastTrace.value = null
    const id = createMessageId()
    messages.value.push({ id, role: 'assistant', content: response.response || '本次没有取得回答，请重试。',
      meta: [agentLabel(response.primaryAgent || response.agentType), response.status === 'partial' ? '部分完成' : '', response.escalated ? '未接通人工' : ''].filter(Boolean).join(' · '),
      traceState: 'loading', trace: null })
    traceLoading.value = true
    void populateTrace(response.requestId, id, version, requestSettings)
    void loadMonitor()
  } catch (error) {
    if (error.data?.retryable === false) pendingRetry.value = null
    if (version === sessionVersion) messages.value.push({ id: createMessageId(), role: 'assistant', content: '暂时没有取得回答，请稍后重试。可以保留刚才的问题继续讨论。', meta: '请求未完成', isError: true })
  } finally { chatBusy.value = false; void scrollMessages() }
}
async function populateTrace(requestId, messageId, version, requestSettings) {
  let result = null
  try { result = requestId ? await requestToolTrace(requestSettings.backend, requestSettings, requestId) : null } catch { /* Answer stays visible if diagnostics fail. */ }
  if (version !== sessionVersion) return
  const message = messages.value.find(item => item.id === messageId)
  if (message) { message.trace = result?.found ? result.trace : null; message.traceState = result?.found ? 'ready' : 'unavailable' }
  if (lastResponse.value?.requestId === requestId) { lastTrace.value = result; traceLoading.value = false }
}
function usePrompt(prompt) { draft.value = prompt; nextTick(() => document.querySelector('.composer textarea')?.focus()) }
function clearConversation() {
  if (busy.value) return
  sessionVersion += 1
  requestState.invalidate()
  pendingRetry.value = null
  messages.value = []
  draft.value = ''
  lastResponse.value = null
  lastTrace.value = null
  traceLoading.value = false
  settings.conversationId = ''
  persist()
}
function applyUserId() {
  if (busy.value) return
  const nextUser = userIdDraft.value.trim() || createUserId()
  if (nextUser === settings.userId) { userIdDraft.value = nextUser; return }
  clearConversation()
  settings.userId = nextUser
  userIdDraft.value = nextUser
  persist()
  showToast('已应用新标识，并开始新对话')
}
async function searchKnowledge() {
  if (busy.value || !searchQuery.value.trim()) return
  searchBusy.value = true
  searchError.value = ''
  searchResults.value = []
  searchCompleted.value = false
  try {
    const data = await requestSearch(settings.backend, settings, searchQuery.value.trim(), 5, enhancedSearch.value)
    if (data.success === false) throw new Error('retrieval unavailable')
    searchResults.value = data.results || []
    searchCompleted.value = true
  } catch { searchError.value = '本次检索未完成，请检查连接或稍后重试。'; searchCompleted.value = false }
  finally { searchBusy.value = false }
}
async function submitKnowledge() {
  if (busy.value || !docTitle.value.trim() || !docContent.value.trim()) return
  maintenanceBusy.value = true
  try { await addKnowledge(settings.backend, settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }]); docTitle.value = ''; docContent.value = ''; await loadStats(); showToast('补充文档已添加') }
  catch { showToast('文档添加失败，请检查连接与内容') }
  finally { maintenanceBusy.value = false }
}
async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || busy.value) return
  maintenanceBusy.value = true
  try { await uploadKnowledge(settings.backend, settings, file); await loadStats(); showToast(file.name + ' 已导入') }
  catch { showToast('文件导入失败，请检查格式与连接') }
  finally { maintenanceBusy.value = false }
}
async function runEvaluation() {
  if (busy.value) return
  evalBusy.value = true
  evalError.value = ''
  evalData.value = null
  const current = requestState.begin('evaluation')
  try {
    const result = await requestEvaluation(current.backend, current.settings, buildEvaluationPayload(), {
      isCurrent: current.isCurrent, onProgress: job => { if (current.isCurrent()) evalProgress.value = job }
    })
    if (current.isCurrent() && result) { evalData.value = result; showToast('单轮评测完成') }
  }
  catch { if (current.isCurrent()) evalError.value = '评测未完成，请检查连接或模型服务。' }
  finally { if (current.isCurrent()) evalBusy.value = false }
}
function createMessageId() { return 'message-' + Date.now() + '-' + (++messageSequence) }
function showToast(message) { toast.value = message; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.value = '' }, 3000) }
</script>

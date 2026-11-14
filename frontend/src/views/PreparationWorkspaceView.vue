<script setup>
import { ref, computed, reactive, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import Header from '@/components/Header.vue'
import {
  analyzeWork,
  analyzeWorks,
  generatePlan,
  getCompetitionAwards,
  getCompetitionDetail,
  getInspirationStatus,
  getInspirations,
  requestInspirations,
  requestMoreIdeas
} from '@/services/api'
import { isLoggedIn } from '@/services/auth'
import { openAuthModal } from '@/composables/useAuthModal'
import { renderMarkdown } from '@/services/markdown'
import { clearPlanCache, getPlanCache, setPlanCache } from '@/services/planCache'
import { loadFavoriteIds, saveFavoriteIds } from '@/services/favorites'

const route = useRoute()
// 当前竞赛 ID（路由参数），供 AI 接口使用
const routedId = computed(() => route.params.id)

// ---- 顶部竞赛信息（按路由真实 id 拉取详情，不随 Tab 切换重复请求）----
const competition = reactive({
  title: '',
  level: '',
  category: '',
  contestTime: '',
  image: '',
  // 是否有可提炼的项目作品。纯算法/知识类赛事（蓝桥杯、ACM-ICPC）为 false：
  // 历年公示只有获奖名单、没有项目作品，灵感分析没有备赛价值。
  hasProjectAwards: true
})
const compLoading = ref(true)
const compError = ref('')

// 比赛时间统一走 utils/timeFormat（日期轨优先，缺失降级月份轨）
import { formatContest } from '../utils/timeFormat'

// 封面图：只接受 http(s) 绝对地址，其余回落占位图
function resolveImage(item) {
  const raw = item.image ?? item.cover ?? item.coverUrl ?? item.image_url ?? ''
  const url = typeof raw === 'string' ? raw.trim() : ''
  return /^https?:\/\//i.test(url) ? url : ''
}

function applyDetail(d) {
  if (!d) return
  competition.title = d.title || d.name || ''
  competition.level = d.level || d.grade || ''
  competition.category = d.category || d.type || ''
  competition.image = resolveImage(d)
  // 只有明确的 false 才判定为「纯算法/知识类」；字段缺失一律按有作品处理
  competition.hasProjectAwards = d.has_project_awards === false ? false : true
  // 详情回来后同步首句问候口径（只在还没聊过、仅剩开场白时替换）
  if (Array.isArray(messages.value) && messages.value.length === 1
      && messages.value[0].role === 'assistant') {
    messages.value[0] = greetingMessage()
  }
  // 算法竞技赛事没有「灵感工作台」Tab：若当前停在它上面，退回第一个 Tab
  if (competition.hasProjectAwards === false && activeTab.value === 'idea') {
    activeTab.value = 'ai'
  }
  // 兼容老字段（完整日期），没有再退回 *月份* 拼接
  const startT = d.contestStart || d.startTime || d.contestTime || d.registrationStart || ''
  const endT = d.contestEnd || d.endTime || d.registrationEnd || ''
  competition.contestTime = startT && endT
    ? startT + ' 至 ' + endT
    : (startT || endT || formatContest(d))
}

async function loadCompetition() {
  const id = routedId.value
  if (id === undefined || id === null || id === '') {
    compLoading.value = false
    compError.value = '缺少竞赛 ID'
    return
  }
  compLoading.value = true
  compError.value = ''
  try {
    const d = await getCompetitionDetail(id)
    // 请求返回时路由可能已切到别的竞赛：丢弃过期响应，避免旧标题/图片覆盖新赛事
    if (routedId.value !== id) return
    applyDetail(d && d.data ? d.data : d)
    if (!competition.title) compError.value = '未获取到竞赛信息'
  } catch (e) {
    if (routedId.value !== id) return
    compError.value = (e && e.message) || '竞赛信息加载失败'
  } finally {
    // 仅当仍是当前竞赛时才复位 loading，避免清掉新赛事的请求态
    if (routedId.value === id) compLoading.value = false
  }
}

// 路由切换到另一场竞赛时，重新拉取顶部信息
watch(routedId, () => {
  // 上一场赛事的生成进度与定时器必须作废，避免串台
  stopInspirePolling()
  inspireState.value = 'idle'
  inspireError.value = ''
  awardsCount.value = 0
  loadCompetition()
  loadWorks()
  loadInspirations()
  // 竞赛变了，Tab1 的计划/对话属于上一场赛事，必须清空，再尝试回填新竞赛的缓存
  resetPlanState()
  restorePlanState()
  // 竞赛变了，之前打开的作品弹窗不再属于当前赛事
  closeWork()
  // 共性报告属于竞赛级数据，切走后不再展示上一场赛事的分析
  resetCommonAnalysis()
})

// ---- Tab \u5207\u6362\uff08\u54cd\u5e94\u5f0f\u53d8\u91cf\uff09----
const tabs = [
  { key: 'ai', label: 'AI\u5907\u8d5b' },
  { key: 'works', label: '\u5f80\u5e74\u83b7\u5956\u4f5c\u54c1' },
  { key: 'idea', label: '\u7075\u611f\u5de5\u4f5c\u53f0' }
]
const activeTab = ref('ai')

// 纯算法/知识类赛事（蓝桥杯、ACM-ICPC）只公示获奖名单、没有项目作品：
// 「往年获奖作品」名单对刷题备赛毫无价值（用户要的是刷题路线与真题解析），
// 「灵感工作台」也不做项目分析。因此只保留「AI备赛」一个 Tab，其余全部隐藏。
const visibleTabs = computed(() => (
  algorithmOnly.value ? tabs.filter((tab) => tab.key === 'ai') : tabs
))

/* =========================================================
   Tab1：AI 备赛计划（真实接口：渐进式多轮对话）
   ---------------------------------------------------------
   后端 /api/ai/plan 三种返回：
     status = 'ask'    -> 反问，追加到对话气泡，等待用户作答
     status = 'plan'   -> 首次生成完整计划，渲染为 Markdown 卡片
     status = 'refine' -> 在原计划上增量修改，原地替换计划正文
   ========================================================= */

// 对话气泡（仅展示追问 / 提示，不含计划正文）
const GREETING =
  '你好，我是你的专属备赛教练。为了给你量身定制计划，' +
  '请告诉我你的备赛目标、基础情况以及每天能投入的时间。'

// 纯算法/知识类赛事（蓝桥杯、ACM-ICPC）：教练产出的是刷题路线图，不是作品赛计划
const ALGO_GREETING =
  '你好，我是你的算法竞技教练。告诉我你的目标奖项、比赛语言（C++/Java/Python）、' +
  '当前刷题量与平台、薄弱知识点、每天能投入的时间，我来排一份可直接执行的刷题路线图。'

function greetingMessage() {
  const algo = competition.hasProjectAwards === false
  return { role: 'assistant', content: algo ? ALGO_GREETING : GREETING }
}

const messages = ref([greetingMessage()])

// 当前完整计划（Markdown 正文）；非空即视为「已有计划」，后续请求走 refine
const plan = ref('')
const hasPlan = computed(() => plan.value.trim().length > 0)
const planHtml = computed(() => (hasPlan.value ? renderMarkdown(plan.value) : ''))

// 交互状态机：'chat' = 对话收集信息阶段；'plan' = 文档阅读 + 补充修改阶段。
// 只有后端明确返回 status === 'plan' / 'refine' 时才切到 'plan'；
// status === 'ask' 时强制回到 'chat'，把 AI 的反问展示在对话区。
const viewMode = ref('chat')
const isShowingPlan = computed(() => viewMode.value === 'plan' && hasPlan.value)

// 计划态下对话记录默认折叠（聊天区让位给文档卡片）
const chatOpen = ref(false)

// 全屏（沉浸）阅读
const isReaderOpen = ref(false)

// 输入框
const draft = ref('')

// loading / 错误
// generating：首轮生成 / 重新生成计划的全局请求态
// isSending ：用户发送一条消息后的请求态（与 generating 独立，避免互相覆盖）
const generating = ref(false)
const isSending = ref(false)
const isBusy = computed(() => isSending.value || generating.value)
const errorMsg = ref('')

// 多轮对话历史：每轮 {"user": "...", "assistant": "..."}，拼接后作为 user_input
const history = ref([])

const planCard = ref(null)
const transcriptEl = ref(null)

// 本地缓存状态：最近一次写入时间 / 写入失败提示
const planSavedAt = ref('')
const cacheWarning = ref('')

// 从 localStorage 回填期间禁止回写，避免把「上次保存时间」刷成「刚刚」
let hydrating = false

// 恢复时的安全上限：异常脏数据（超大字符串）会拖垮 Markdown 渲染，
// 超过上限直接当作无效缓存丢弃，保证页面能正常打开。
const MAX_RESTORE_PLAN_CHARS = 200000
const MAX_RESTORE_MESSAGE_CHARS = 20000

/** 把时间戳格式化成「x 分钟前」；任何异常都降级为空串，绝不能抛出 */
function formatSavedAt(savedAt) {
  try {
    const ts = Number(savedAt)
    if (!Number.isFinite(ts) || ts <= 0) return ''
    const diff = Date.now() - ts
    if (diff < 0 || diff < 60000) return '刚刚'
    const min = Math.floor(diff / 60000)
    if (min < 60) return `${min} 分钟前`
    const hour = Math.floor(min / 60)
    if (hour < 24) return `${hour} 小时前`
    const day = Math.floor(hour / 24)
    if (day < 30) return `${day} 天前`
    return new Date(ts).toLocaleDateString()
  } catch (e) {
    return ''
  }
}

// 把 history + 本轮输入拼成后端要求的「完整背景」
function buildUserInput(latest = '') {
  const turns = history.value
    .map((h, i) => `第${i + 1}轮\n用户：${h.user}\n助手：${h.assistant}`)
    .join('\n\n')
  if (!latest) return turns
  return turns ? `${turns}\n\n本轮用户：${latest}` : `本轮用户：${latest}`
}

/** 切竞赛时重置 Tab1：清空计划正文、对话记录与错误，避免上个赛事的上下文串台 */
function resetPlanState() {
  plan.value = ''
  history.value = []
  messages.value = [greetingMessage()]
  draft.value = ''
  errorMsg.value = ''
  generating.value = false
  isSending.value = false
  viewMode.value = 'chat'
  chatOpen.value = false
  isReaderOpen.value = false
  planSavedAt.value = ''
  cacheWarning.value = ''
}

/* ---- 本地持久化：刷新 / 下次进入仍能恢复计划与对话 ---- */

/**
 * 把当前计划与对话写入 localStorage。
 * 空状态（还没生成过计划）不写，避免覆盖已有缓存。
 */
function persistPlanState() {
  // watcher 里抛异常会中断 Vue 的调度队列，导致整个页面失去响应，因此这里必须兜底
  try {
    if (hydrating) return
    const cid = routedId.value
    if (!cid) return
    if (!plan.value.trim() && messages.value.length <= 1 && history.value.length === 0) return

    const ok = setPlanCache(cid, {
      plan: plan.value,
      messages: messages.value,
      history: history.value
    })
    if (ok) {
      planSavedAt.value = formatSavedAt(Date.now())
      cacheWarning.value = ''
    } else {
      cacheWarning.value = '本地缓存写入失败，刷新后可能丢失'
    }
  } catch (e) {
    // 存储异常只影响缓存，绝不能让主流程（发送 / 渲染）崩溃
    cacheWarning.value = '本地缓存写入失败，刷新后可能丢失'
  }
}

// 计划、对话、轮次任一变化都落盘（deep 以覆盖 push / 替换两种写法）。
// flush: 'post' 让写盘发生在 DOM 更新之后，避免与渲染互相阻塞。
watch([plan, messages, history], persistPlanState, { deep: true, flush: 'post' })

/** 把缓存里的消息收敛成合法气泡，任何脏结构都不会进入渲染层 */
function sanitizeCachedMessages(raw) {
  if (!Array.isArray(raw)) return []
  const clean = []
  for (const m of raw) {
    if (!m || (m.role !== 'user' && m.role !== 'assistant')) continue
    if (typeof m.content !== 'string') continue
    const content = m.content.length > MAX_RESTORE_MESSAGE_CHARS
      ? m.content.slice(0, MAX_RESTORE_MESSAGE_CHARS)
      : m.content
    if (!content.trim()) continue
    clean.push({ role: m.role, content })
  }
  return clean
}

/** 把缓存里的轮次收敛成合法结构 */
function sanitizeCachedHistory(raw) {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((h) => h && typeof h.user === 'string' && typeof h.assistant === 'string')
    .map((h) => ({
      user: h.user.length > MAX_RESTORE_MESSAGE_CHARS ? h.user.slice(0, MAX_RESTORE_MESSAGE_CHARS) : h.user,
      assistant: h.assistant.length > MAX_RESTORE_MESSAGE_CHARS
        ? h.assistant.slice(0, MAX_RESTORE_MESSAGE_CHARS)
        : h.assistant
    }))
}

/** 挂载 / 切竞赛后，尝试用本地缓存恢复计划与对话；脏数据一律静默降级 */
function restorePlanState() {
  const cid = routedId.value
  if (!cid) return

  hydrating = true
  try {
    const cached = getPlanCache(cid)
    if (cached && typeof cached === 'object') {
      const cachedPlan = typeof cached.plan === 'string' ? cached.plan : ''
      const safePlan = cachedPlan.length > MAX_RESTORE_PLAN_CHARS ? '' : cachedPlan
      const cachedMessages = sanitizeCachedMessages(cached.messages)
      const cachedHistory = sanitizeCachedHistory(cached.history)

      plan.value = safePlan
      messages.value = cachedMessages.length ? cachedMessages : [greetingMessage()]
      history.value = cachedHistory
      // 有完整计划 → 直接进入文档态；否则停留在对话态
      viewMode.value = safePlan.trim() ? 'plan' : 'chat'
      chatOpen.value = false
      planSavedAt.value = formatSavedAt(cached.savedAt)
      cacheWarning.value = ''
    }
  } catch (e) {
    // 脏缓存导致解析 / 赋值失败：回到干净的初始态，页面照常可用
    plan.value = ''
    messages.value = [greetingMessage()]
    history.value = []
    viewMode.value = 'chat'
    planSavedAt.value = ''
    cacheWarning.value = ''
  } finally {
    // 等本轮 watcher 冲刷结束后再恢复回写
    nextTick(() => {
      hydrating = false
    })
  }
}

/** 从对话态回到已有计划文档（ask 之后计划仍在，只是视图被切到对话） */
function showPlan() {
  if (!hasPlan.value) return
  viewMode.value = 'plan'
  chatOpen.value = false
  nextTick(() => {
    try {
      planCard.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    } catch (e) { /* 滚动失败不影响功能 */ }
  })
}

// 统一请求：拿后端返回后按 status 分流渲染
async function requestPlan(latestInput = '') {
  errorMsg.value = ''
  // 请求前快照竞赛 id：响应回来时若已切到别的竞赛，直接丢弃，避免数据污染
  const cid = routedId.value
  if (!cid) throw new Error('竞赛 ID 缺失，请返回列表重新进入')
  const res = await generatePlan({
    competitionId: cid,
    userInput: buildUserInput(latestInput),
    // 只要本地已有计划（无论当前视图在对话还是文档）就带上 current_plan 走增量修改
    currentPlan: hasPlan.value ? plan.value : null
  })

  // 竞赛已切换 → 本次结果作废，不写 plan / messages / history
  if (routedId.value !== cid) return null

  const status = typeof res?.status === 'string' ? res.status : ''
  const reply = (res?.reply || '').trim()

  if (!reply) throw new Error('AI 未返回内容，请稍后重试')

  // 状态机：只有 ask 才留在对话；只有 plan / refine 才更新并展示文档
  if (status === 'ask') {
    // 反问：只进对话气泡，不产生 / 不改动计划，并把视图切回对话区
    messages.value.push({ role: 'assistant', content: reply })
    history.value.push({ user: latestInput, assistant: reply })
    viewMode.value = 'chat'
    chatOpen.value = true
    return 'ask'
  }

  if (status === 'plan' || status === 'refine') {
    // plan / refine：正文即完整计划，原地替换文档卡片
    const isRefine = status === 'refine'
    plan.value = reply
    history.value.push({
      user: latestInput,
      assistant: isRefine ? '（已根据你的补充更新完整计划）' : '（已生成完整备赛计划）'
    })
    messages.value.push({
      role: 'assistant',
      content: isRefine ? '已按你的补充更新计划。' : '计划已生成，可在下方直接补充信息继续调整。'
    })
    // 计划出来的瞬间切到文档视图并收起聊天区
    viewMode.value = 'plan'
    chatOpen.value = false
    return status
  }

  // 未知 status：不擅自渲染文档，按对话处理，避免误把非计划内容当计划展示
  messages.value.push({ role: 'assistant', content: reply })
  history.value.push({ user: latestInput, assistant: reply })
  viewMode.value = 'chat'
  chatOpen.value = true
  return 'ask'
}

// 滚动到指定区域：滚动失败（元素未挂载 / 浏览器限制）绝不影响后续流程
async function scrollToResult(status) {
  try {
    await nextTick()
    if (status === 'ask') {
      transcriptEl.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    } else {
      planCard.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  } catch (e) { /* 忽略滚动异常 */ }
}

// 「生成专属备赛计划」：首轮
async function startPlan() {
  if (isBusy.value) return
  const cid = routedId.value
  generating.value = true
  try {
    const status = await requestPlan('')
    if (routedId.value === cid && status) {
      // 反问 → 看对话区；计划生成成功 → 看文档卡片
      await scrollToResult(status)
    }
  } catch (e) {
    // 已切走则不回写错误，防止把旧竞赛的失败提示带到新竞赛
    if (routedId.value !== cid) return
    errorMsg.value = e?.message || '生成失败，请稍后重试'
  } finally {
    // 仅当仍是同一竞赛时才复位 loading，避免覆盖新竞赛的请求态
    if (routedId.value === cid) generating.value = false
  }
}

// 计划态「重新生成」：丢弃当前计划与对话，从零开始
async function restartPlan() {
  if (isBusy.value) return
  if (typeof window !== 'undefined' && !window.confirm('重新生成会清空当前计划与对话记录，确定继续吗？')) {
    return
  }
  const cid = routedId.value
  if (cid) clearPlanCache(cid)
  resetPlanState()
  await startPlan()
}

// 发送：首次对话与计划态补充信息共用同一入口
async function send() {
  if (isBusy.value) return
  const text = draft.value.trim()
  if (!text) return

  const cid = routedId.value
  // ---- 乐观更新：点击即上屏 + 立即清空输入框，给用户即时反馈 ----
  messages.value.push({ role: 'user', content: text })
  draft.value = ''
  errorMsg.value = ''
  isSending.value = true

  try {
    const status = await requestPlan(text)
    if (routedId.value === cid && status) {
      // 追问 → 看对话；计划更新 → 看文档卡片
      await scrollToResult(status)
    }
  } catch (e) {
    if (routedId.value !== cid) return
    errorMsg.value = e?.message || '更新失败，请稍后重试'
  } finally {
    // 必须无条件复位：任何分支（成功 / 失败 / 切竞赛）都不能让输入框永久锁死
    isSending.value = false
  }
}

// 中文输入法回车确认候选词时不应触发发送
function onEnter(e) {
  if (e?.isComposing || e?.keyCode === 229) return
  e?.preventDefault?.()
  send()
}

/* ---- 全屏阅读：打开时锁定背景滚动，关闭 / 卸载时恢复 ---- */
watch(isReaderOpen, (open) => {
  if (typeof document === 'undefined') return
  document.body.style.overflow = open ? 'hidden' : ''
})

/* =========================================================
   Tab2\uff1a\u5f80\u5e74\u83b7\u5956\u4f5c\u54c1 + \u8be6\u60c5 Modal
   ========================================================= */
const selectedWork = ref(null)     // Modal \u5f53\u524d\u4f5c\u54c1
const workParsing = ref(false)     // Modal \u5185 AI \u89e3\u6790 loading
const parsedWork = ref(null)       // \u5df2\u89e3\u6790\u7684\u4f5c\u54c1 id

// ---- 获奖作品列表：真实接口 GET /api/competitions/{id}/awards ----
const works = ref([])
const worksLoading = ref(true)
const worksError = ref('')
// 「加载更多」分页：每页 20 条，offset 由已加载条数推出来
const WORKS_PAGE_SIZE = 20
const worksTotal = ref(0)
const worksLoadingMore = ref(false)
const worksHasMore = computed(() => works.value.length < worksTotal.value)

/**
 * 后端 /awards 返回 snake_case（award_grade / origin_url），
 * 而卡片与详情弹窗沿用 camelCase；这里统一转一次，
 * 避免两套命名各自兜底、出现「奖项标签空白」这类问题。
 */
function normalizeWork(item) {
  if (!item || typeof item !== 'object') return item
  return {
    ...item,
    awardGrade: item.awardGrade || item.award_grade || '',
    originUrl: item.originUrl || item.origin_url || '',
  }
}

// 卡片底部元信息标签：灰底 + 细描边，替代原来的图片占位区
const META_TAG_CLASS =
  'inline-flex max-w-full items-center whitespace-nowrap rounded-md bg-slate-100/80 px-2 py-1 ' +
  'text-[11px] font-semibold text-slate-600 ring-1 ring-slate-200/60 ' +
  'dark:bg-slate-700/50 dark:text-slate-300 dark:ring-slate-600/40'

/**
 * 拉取当前竞赛的获奖作品。
 * 只在挂载 / 路由竞赛 ID 变化时调用，Tab 切换不重复请求。
 */
async function loadWorks() {
  const cid = routedId.value
  if (!cid) {
    works.value = []
    worksLoading.value = false
    worksError.value = '竞赛 ID 无效，无法加载获奖作品'
    return
  }

  worksLoading.value = true
  worksError.value = ''
  try {
    const res = await getCompetitionAwards(cid, { limit: WORKS_PAGE_SIZE, offset: 0 })
    // 切走赛事后旧响应不得写回，否则会串成上一场竞赛的获奖列表
    if (routedId.value !== cid) return
    works.value = (res.items || []).map(normalizeWork)
    worksTotal.value = Math.max(Number(res.total) || 0, works.value.length)
  } catch (e) {
    if (routedId.value !== cid) return
    works.value = []
    worksTotal.value = 0
    worksError.value = e?.message || '获奖作品加载失败，请稍后重试'
  } finally {
    // 仅当前赛事才复位，避免误清新赛事的 loading
    if (routedId.value === cid) worksLoading.value = false
  }
}

/**
 * 加载更多：按 offset = 已加载条数 追加下一页。
 * 失败只提示、不打断已有列表；同一时间只允许一个请求在飞。
 */
async function loadMoreWorks() {
  const cid = routedId.value
  if (!cid || worksLoadingMore.value || !worksHasMore.value) return

  worksLoadingMore.value = true
  worksError.value = ''
  try {
    const res = await getCompetitionAwards(cid, {
      limit: WORKS_PAGE_SIZE,
      offset: works.value.length,
    })
    if (routedId.value !== cid) return
    const seen = new Set(works.value.map((w) => w?.id))
    const fresh = (res.items || []).map(normalizeWork).filter((w) => w && !seen.has(w.id))
    works.value = works.value.concat(fresh)
    worksTotal.value = Math.max(Number(res.total) || 0, works.value.length)
  } catch (e) {
    if (routedId.value !== cid) return
    worksError.value = e?.message || '加载更多失败，请稍后重试'
  } finally {
    if (routedId.value === cid) worksLoadingMore.value = false
  }
}

/* ---- 共性分析：竞赛级报告（一键分析全部获奖作品的共性） ---- */
const commonText = ref('')          // 共性分析正文（markdown）
const commonLoading = ref(false)    // 请求中
const commonError = ref('')
const hasCommon = computed(() => !!commonText.value)
const commonHtml = computed(() => (commonText.value ? renderMarkdown(commonText.value) : ''))

/**
 * 触发共性分析。
 * 缓存完全由后端负责：前端每次点击都正常发起请求，
 * 后端命中缓存会立即返回，未命中则等待生成，前端无需区分。
 */
async function runCommonAnalysis() {
  if (commonLoading.value) return

  const cid = routedId.value
  if (!cid) {
    commonError.value = '竞赛 ID 缺失，请返回列表重新进入'
    return
  }

  commonLoading.value = true
  commonError.value = ''

  try {
    // analyzeWorks 内部已用 normalizeCompetitionId 校验并转成数字
    const res = await analyzeWorks(cid)
    // 请求期间已切到别的竞赛 → 丢弃本次结果，避免串台
    if (routedId.value !== cid) return
    const analysis = res?.analysis || ''
    if (!analysis) {
      commonError.value = '未获取到分析内容，请稍后重试'
    } else {
      commonText.value = analysis
    }
  } catch (e) {
    if (routedId.value !== cid) return
    commonError.value = e?.message || 'AI 分析失败，请稍后重试'
  } finally {
    // 仅当仍是同一竞赛时才复位 loading，防止覆盖新竞赛的请求态
    if (routedId.value === cid) commonLoading.value = false
  }
}

function resetCommonAnalysis() {
  commonText.value = ''
  commonError.value = ''
}

function openWork(work) {
  if (!work) return
  // 切换作品时清空上一篇的解析结果与错误，避免残留内容串台
  selectedWork.value = work
  workParsing.value = false
  parsedWork.value = null
  parsedText.value = ''
  parseError.value = ''
  originalTip.value = ''
}

function closeWork() {
  selectedWork.value = null
  workParsing.value = false
  parsedWork.value = null
  parsedText.value = ''
  parseError.value = ''
  originalTip.value = ''
}

// 该作品是否已收录原文链接（后端接入爬虫后由 originUrl 提供）
const hasOriginal = computed(() => Boolean(selectedWork.value?.originUrl))

// 技术栈标签配色池：按索引轮换，视觉分层
const TECH_TAG_STYLES = [
  'bg-blue-50 text-blue-700 ring-blue-200',
  'bg-violet-50 text-violet-700 ring-violet-200',
  'bg-emerald-50 text-emerald-700 ring-emerald-200',
  'bg-amber-50 text-amber-700 ring-amber-200',
  'bg-rose-50 text-rose-700 ring-rose-200',
  'bg-cyan-50 text-cyan-700 ring-cyan-200',
]
function techTagClass(index) {
  return TECH_TAG_STYLES[index % TECH_TAG_STYLES.length]
}

// 级别 / 奖项等级中英文映射：后端可能返回英文枚举，统一转成中文展示
const LEVEL_LABELS = {
  national: '国家级',
  provincial: '省级',
  city: '市级',
  school: '校级',
  international: '国际级',
}

const AWARD_GRADE_LABELS = {
  'first prize': '一等奖',
  first: '一等奖',
  firstprize: '一等奖',
  'second prize': '二等奖',
  second: '二等奖',
  secondprize: '二等奖',
  'third prize': '三等奖',
  third: '三等奖',
  thirdprize: '三等奖',
  gold: '金奖',
  silver: '银奖',
  bronze: '铜奖',
  'grand prize': '特等奖',
  grand: '特等奖',
  excellence: '优秀奖',
  excellent: '优秀奖',
  'honorable mention': '优胜奖',
  honorable: '优胜奖',
}

// 通用翻译：先精确匹配，再忽略大小写 / 空格 / 连字符匹配，未命中则原样返回
function translate(raw, dict) {
  if (typeof raw !== 'string') return raw
  const text = raw.trim()
  if (!text) return raw
  const key = text.toLowerCase()
  if (dict[key]) return dict[key]
  const compact = key.replace(/[\s_-]+/g, '')
  for (const k of Object.keys(dict)) {
    if (k.replace(/[\s_-]+/g, '') === compact) return dict[k]
  }
  return raw
}

function zhLevel(raw) {
  return translate(raw, LEVEL_LABELS)
}

function zhAwardGrade(raw) {
  return translate(raw, AWARD_GRADE_LABELS)
}


// 「查看完整原文」：暂无数据时给出温和提示，有链接时新窗口打开
function openOriginal() {
  const url = selectedWork.value?.originUrl
  if (!url) {
    originalTip.value = '作品原文暂未收录，待接入爬虫后可查看完整原文与论文。'
    return
  }
  window.open(url, '_blank', 'noopener,noreferrer')
}


// Modal 内「AI 解析」：调用后端 /api/ai/analyze-work
const parsedText = ref('')
const parseError = ref('')
const originalTip = ref('')      // 原文入口的提示文案（暂无链接时展示）
const parsedHtml = computed(() => (parsedText.value ? renderMarkdown(parsedText.value) : ''))


async function parseWork() {
  if (workParsing.value || !selectedWork.value) return

  // 统一走 routedId 计算属性，确保拿到的是当前路由的真实竞赛 ID
  const cid = routedId.value
  if (!cid) {
    parseError.value = '竞赛 ID 缺失，请返回列表重新进入'
    return
  }

  workParsing.value = true
  parseError.value = ''
  parsedWork.value = null
  parsedText.value = ''

  // 请求前快照（竞赛 + 作品），避免 await 期间用户切换导致结果串台
  const workId = selectedWork.value.id
  // 回调是否仍属当前上下文：竞赛未切走 且 弹窗仍是同一个作品
  const isStillCurrent = () => routedId.value === cid && selectedWork.value?.id === workId
  try {
    // 缓存由后端负责：每次点击都正常请求，命中则立即返回，未命中则等待生成
    // analyzeWork 内部已做竞赛 ID 校验，指向单作品接口
    const res = await analyzeWork(workId, { competitionId: cid })
    const analysis = res?.analysis || ''
    if (!analysis) {
      // 连错误提示也要守卫，否则会把别处的弹窗写上错误
      if (isStillCurrent()) parseError.value = '未获取到解析内容，请稍后重试'
    } else {
      // 只有仍在同一竞赛 + 同一作品时才回填 UI
      if (isStillCurrent()) {
        parsedText.value = analysis
        parsedWork.value = workId
      }
    }
  } catch (e) {
    if (isStillCurrent()) {
      parseError.value = e?.message || 'AI 解析失败，请稍后重试'
    }
  } finally {
    // 仅当竞赛未切走时才复位 loading，避免清掉新竞赛的解析中状态
    if (routedId.value === cid) workParsing.value = false
  }
}


/* =========================================================
   Tab3\uff1a\u7075\u611f\u5de5\u4f5c\u53f0 + \u6211\u7684\u6536\u85cf\uff08localStorage\uff09
   ========================================================= */
const INSIGHT_TAGS = {
  experience: '获奖经验',
  trend: '选题趋势',
  gap: '空白机会'
}

// 不同洞察类型的标签配色：经验=蓝、趋势=绿、空白=橙，便于一眼区分
const INSIGHT_TAG_STYLES = {
  experience: 'bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30',
  trend: 'bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-500/15 dark:text-emerald-300 dark:ring-emerald-500/30',
  gap: 'bg-orange-50 text-orange-700 ring-orange-200 dark:bg-orange-500/15 dark:text-orange-300 dark:ring-orange-500/30'
}

function insightTagClass(kind) {
  return INSIGHT_TAG_STYLES[kind] || 'bg-slate-100 text-slate-600 ring-slate-200 dark:bg-slate-700/60 dark:text-slate-300 dark:ring-slate-600'
}

// 收藏 key / 读写统一由 services/favorites.js 管理（含脏数据校验）
const favIds = ref(loadFavoriteIds())
const inspirations = ref([])
const inspirationLoading = ref(false)
const inspirationError = ref('')

// 当前是否处于「我的收藏」视图（Tab3 内条件渲染）
const showFavorites = ref(false)

const favoriteList = computed(() => inspirations.value.filter((it) => favIds.value.includes(it.key)))

// 当前视图下可见的卡片：全部灵感 / 我的收藏
const visibleItems = computed(() => (showFavorites.value ? favoriteList.value : inspirations.value))
// 两大板块：赛事洞察（经验 / 趋势 / 空白）与 选题灵感（idea）互相隔离渲染
const visibleInsights = computed(() => visibleItems.value.filter((it) => it.source === 'insight'))
const visibleIdeas = computed(() => visibleItems.value.filter((it) => it.source === 'idea'))

function applyFavoriteFlags() {
  inspirations.value.forEach((it) => {
    it.favorite = favIds.value.includes(it.key)
  })
}

/* 兜底清洗：模型偶尔仍把 id 写进正文（老缓存里尤其多）。
   形如 `[id:1782]`、`id 12、16、24`、`（1782、2350、2918）` 一律移除，
   避免「满屏 ID」再次出现。后端已清洗，这里只为历史数据兜底。 */
const ID_CITE_RE = /[（(\[【]?\s*(?:作品|参考|样本)?\s*id\s*[:：]?\s*\d+(?:\s*[、,，/]\s*\d+)*\s*[)）\]】]?/gi
const NUM_LIST_RE = /[（(\[【]\s*\d{2,6}(?:\s*[、,，/]\s*\d{2,6}){1,}\s*[)）\]】]/g

function stripIdClutter(text) {
  if (!text) return ''
  return String(text)
    .replace(ID_CITE_RE, '')
    .replace(NUM_LIST_RE, '')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/** 引用 id 只保留少量代表性样本；超长数字数组直接丢弃（视为无意义）。 */
function cleanRefs(list) {
  if (!Array.isArray(list)) return []
  const nums = list.map((v) => Number(v)).filter((n) => Number.isFinite(n))
  if (nums.length > 6) return []
  return nums.slice(0, 3)
}

/** 后端返回的 insights / ideas -> 页面统一卡片结构（key 跨表唯一） */
function normalizeInspirationItems(payload) {
  const insights = (payload?.insights || []).map((r) => {
    const tag = INSIGHT_TAGS[r.kind] || '赛事洞察'
    const md = stripIdClutter(r.content_md)
    return {
      key: `insight-${r.id}`,
      id: r.id,
      source: 'insight',
      kind: r.kind || '',
      tag,
      title: tag,
      text: '',
      html: md ? renderMarkdown(md) : '',
      refs: cleanRefs(r.evidence),
      favorite: false
    }
  })

  const ideas = (payload?.ideas || []).map(normalizeIdea).filter(Boolean)

  return [...ideas, ...insights]
}

/** 单条 idea（后端行 / 「换一批」接口返回项）-> 页面卡片结构 */
function normalizeIdea(r) {
  if (!r || r.id === undefined || r.id === null) return null
  return {
    key: `idea-${r.id}`,
    id: r.id,
    source: 'idea',
    tag: '选题灵感',
    title: r.title || '（无标题）',
    text: stripIdClutter(r.pitch || ''),
    html: '',
    detail: stripIdClutter(r.detail_md || ''),
    detailHtml: r.detail_md ? renderMarkdown(stripIdClutter(r.detail_md)) : '',
    novelty: stripIdClutter(r.novelty || ''),
    risk: stripIdClutter(r.risk || ''),
    refs: cleanRefs(r.ref_awards),
    favorite: false
  }
}

/**
 * 按当前路由竞赛 ID 拉取灵感数据。
 * 接口强制要求 competition_id，切赛事时会丢弃过期响应，避免串台。
 */
async function loadInspirations() {
  const cid = routedId.value
  if (!cid) {
    inspirations.value = []
    inspirationLoading.value = false
    inspirationError.value = '竞赛 ID 缺失，无法加载灵感数据'
    return
  }

  inspirationLoading.value = true
  inspirationError.value = ''
  try {
    const data = await getInspirations(cid)
    if (routedId.value !== cid) return
    inspirations.value = normalizeInspirationItems(data)
    awardsCount.value = Number(data?.work_count) || 0
    applyFavoriteFlags()
    // 没有任何灵感时，顺带恢复「正在生成」进度（刷新页面不丢）
    // algorithmOnly 的赛事后端永远返回空列表，不能再去探测状态，
    // 否则 loadInspirations ⇄ syncInspirationState 会互相触发成死循环。
    if (!inspirations.value.length && inspireState.value === 'idle'
        && !algorithmOnly.value && !inspirationSynced) {
      inspirationSynced = true
      syncInspirationState()
    }
  } catch (e) {
    if (routedId.value !== cid) return
    inspirations.value = []
    inspirationError.value = e?.message || '灵感数据加载失败'
  } finally {
    if (routedId.value === cid) inspirationLoading.value = false
  }
}

function persist() {
  saveFavoriteIds(favIds.value)
}

// 刷新灵感：重新请求后端（数据由离线 analyzer.py 生成并落库）
function refreshInspiration() {
  // 手动刷新：允许再探测一次生成状态（重置一次性守卫）
  inspirationSynced = false
  loadInspirations()
}

/* =========================================================
   ✨ 换一批新灵感：把「已看过的列表」传给后端，追加而非覆盖
   ========================================================= */
const moreIdeasLoading = ref(false)
const moreIdeasError = ref('')
const moreIdeasTip = ref('')

async function loadMoreIdeas() {
  if (moreIdeasLoading.value) return

  const cid = routedId.value
  if (!cid) {
    moreIdeasError.value = '竞赛 ID 缺失，请返回列表重新进入'
    return
  }
  if (!isLoggedIn()) {
    openAuthModal('login')
    return
  }

  // 当前已展示的 idea：id 与标题一起给后端，供它拼「必须避开」清单
  const shown = inspirations.value.filter((it) => it.source === 'idea')
  moreIdeasLoading.value = true
  moreIdeasError.value = ''
  moreIdeasTip.value = ''

  try {
    const res = await requestMoreIdeas(cid, {
      count: 4,
      excludeIds: shown.map((it) => it.id),
      excludeTitles: shown.map((it) => it.title)
    })
    if (routedId.value !== cid) return

    const known = new Set(inspirations.value.map((it) => it.key))
    const fresh = (res.ideas || [])
      .map(normalizeIdea)
      .filter((it) => it && !known.has(it.key))

    if (!fresh.length) {
      moreIdeasError.value = '这次没有生成出新选题，再点一次试试'
      return
    }
    // 追加到列表尾部：点得越多，灵感库越厚（不覆盖已有内容）
    inspirations.value = [...inspirations.value, ...fresh]
    applyFavoriteFlags()
    moreIdeasTip.value = `已新增 ${fresh.length} 条，当前共 ${shown.length + fresh.length} 条`
  } catch (e) {
    if (routedId.value !== cid) return
    if (e?.status === 401) {
      openAuthModal('login')
      moreIdeasError.value = '请先登录后再生成灵感'
    } else if (e?.status === 429) {
      moreIdeasError.value = '今日 AI 额度已用完，明天再来试试'
    } else if (e?.status === 409) {
      moreIdeasError.value = '该赛事暂无获奖作品，无法生成选题灵感'
    } else if (e?.status === 400) {
      moreIdeasError.value = e?.message || '该赛事不支持生成选题灵感'
    } else {
      moreIdeasError.value = e?.message || '生成新灵感失败，请稍后重试'
    }
  } finally {
    if (routedId.value === cid) moreIdeasLoading.value = false
  }
}

/* =========================================================
   灵感生成（awards 是唯一素材来源，先看有没有获奖作品）
   ========================================================= */
const awardsCount = ref(0)        // 该赛事获奖作品数：为 0 时按钮置灰
// 纯算法/知识类赛事（蓝桥杯、ACM-ICPC…）：获奖公示只有名单、没有项目作品，
// 生成「选题灵感/技术趋势」既无备赛价值又烧钱，入口整体隐藏（2026-11-14）
const algorithmOnly = computed(() => competition.hasProjectAwards === false)
const inspireState = ref('idle')  // idle | queued | running | ready | failed
const inspireError = ref('')
// 状态探测只做一次：防止 loadInspirations ⇄ syncInspirationState 无限互相触发
let inspirationSynced = false
let inspireTimer = null
let inspirePollCount = 0

const INSPIRE_POLL_FAST_MS = 4000   // 前 20 次 4s
const INSPIRE_POLL_SLOW_MS = 9000   // 之后放慢，避免长任务空转
const INSPIRE_POLL_FAST_LIMIT = 20
const INSPIRE_POLL_MAX = 45         // 约 5 分钟上限

const inspireBusy = computed(
  () => inspireState.value === 'queued' || inspireState.value === 'running'
)
const canGenerateInspiration = computed(
  () => awardsCount.value > 0 && !inspireBusy.value && !algorithmOnly.value
)
const hasInspiration = computed(() => inspirations.value.length > 0)

function stopInspirePolling() {
  if (inspireTimer) {
    clearTimeout(inspireTimer)
    inspireTimer = null
  }
}

function scheduleInspirePoll() {
  stopInspirePolling()
  const delay =
    inspirePollCount < INSPIRE_POLL_FAST_LIMIT
      ? INSPIRE_POLL_FAST_MS
      : INSPIRE_POLL_SLOW_MS
  inspireTimer = setTimeout(pollInspiration, delay)
}

async function pollInspiration() {
  const cid = routedId.value
  if (!cid) return
  inspirePollCount += 1
  try {
    const state = await getInspirationStatus(cid)
    if (routedId.value !== cid) return
    awardsCount.value = state.awards_count || awardsCount.value
    inspireState.value = state.status

    if (state.status === 'ready') {
      stopInspirePolling()
      await loadInspirations()
      return
    }
    if (state.status === 'failed') {
      stopInspirePolling()
      inspireError.value = 'AI 这次没能提炼出灵感，请稍后重试'
      return
    }
    if (inspirePollCount >= INSPIRE_POLL_MAX) {
      stopInspirePolling()
      inspireState.value = 'idle'
      inspireError.value = '生成时间较长，可稍后刷新页面查看结果'
      return
    }
    scheduleInspirePoll()
  } catch (e) {
    stopInspirePolling()
    inspireState.value = 'idle'
    if (e?.status === 401) {
      inspireError.value = '登录状态已失效，请重新登录后再生成'
    } else {
      inspireError.value = e?.message || '查询生成状态失败'
    }
  }
}

/**
 * 进入工作台时恢复「正在生成」状态：
 * 刷新页面后不应丢失进度条，也不能重复入队（后端幂等）。
 */
async function syncInspirationState() {
  const cid = routedId.value
  if (!cid || !isLoggedIn()) return
  try {
    const state = await getInspirationStatus(cid)
    if (routedId.value !== cid) return
    awardsCount.value = state.awards_count || awardsCount.value
    if (state.status === 'queued' || state.status === 'running') {
      inspireState.value = state.status
      inspirePollCount = 0
      scheduleInspirePoll()
    } else if (state.status === 'ready' && !hasInspiration.value
               && !algorithmOnly.value) {
      await loadInspirations()
    }
  } catch (e) {
    // 状态探测失败不影响页面：按钮仍可点击，由 POST 结果兜底
  }
}

async function onGenerateInspiration() {
  if (!canGenerateInspiration.value) return

  if (!isLoggedIn()) {
    openAuthModal('login')
    return
  }

  const cid = routedId.value
  if (!cid) return

  inspireError.value = ''
  inspirePollCount = 0

  try {
    const result = await requestInspirations(cid)
    if (routedId.value !== cid) return
    if (result.status === 'ready') {
      await loadInspirations()
      return
    }
    inspireState.value = result.status === 'running' ? 'running' : 'queued'
    scheduleInspirePoll()
  } catch (e) {
    if (e?.status === 401) {
      openAuthModal('login')
      inspireError.value = '请先登录后再生成灵感'
    } else if (e?.status === 409 && e?.data?.reason === 'no_awards') {
      awardsCount.value = 0
      inspireError.value = '该赛事暂无公开的历年获奖作品，AI 缺少提炼素材'
    } else if (e?.status === 409) {
      inspireError.value = '该赛事刚生成失败过，请稍后再试'
    } else if (e?.status === 429) {
      inspireError.value =
        e?.data?.reason === 'queue_full'
          ? '灵感生成队列已满，请稍后再试'
          : '今日 AI 生成额度已用完，请明天再试'
    } else {
      inspireError.value = e?.message || '生成失败，请稍后重试'
    }
    inspireState.value = 'idle'
  }
}

function toggleFavorite(item) {
  item.favorite = !item.favorite
  if (item.favorite) {
    if (!favIds.value.includes(item.key)) favIds.value.push(item.key)
  } else {
    favIds.value = favIds.value.filter((key) => key !== item.key)
  }
  persist()
}

/* ---- \u5f39\u7a97 Esc \u5173\u95ed ---- */
function onKeydown(e) {
  if (e.key !== 'Escape') return
  // 全屏阅读优先关闭，其次才是作品弹窗
  if (isReaderOpen.value) {
    isReaderOpen.value = false
    return
  }
  if (selectedWork.value) closeWork()
}

/**
 * 退出登录 / 401 时，services/localUserData.js 已清空本地缓存。
 * 这里同步清掉内存中的计划、对话、收藏与弹窗，避免界面继续展示上一账号内容，
 * 也避免下一次操作把旧数据重新写回 localStorage。
 */
function onLocalDataCleared() {
  favIds.value = []
  // 已渲染的卡片仍带 favorite 标记，这里同步复位，避免出现“空收藏但星标仍亮”
  applyFavoriteFlags()
  showFavorites.value = false
  resetPlanState()
  closeWork()
  resetCommonAnalysis()
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('auth:local-data-cleared', onLocalDataCleared)
  // 首屏并行拉取：顶部竞赛信息 + 获奖作品列表 + 该赛事专属灵感
  loadCompetition()
  loadWorks()
  loadInspirations()
  // 计划 / 对话命中本地缓存则直接恢复，刷新不丢。
  // 这里再加一层兜底：即使恢复逻辑内部出现意外，也绝不能让页面白屏 / 死锁。
  try {
    restorePlanState()
  } catch (e) {
    plan.value = ''
    messages.value = [greetingMessage()]
    history.value = []
    viewMode.value = 'chat'
    planSavedAt.value = ''
    cacheWarning.value = ''
  }
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('auth:local-data-cleared', onLocalDataCleared)
  // 定时器必须清掉，否则离开页面后仍会继续轮询
  stopInspirePolling()
  // 离开页面时解除全屏阅读的滚动锁，避免影响其他路由
  document.body.style.overflow = ''
})

</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto w-full max-w-6xl px-4 pb-24 pt-24 sm:px-6 sm:pt-28">
    <!-- ========== 顶部固定区：竞赛小图 + 信息，不随 Tab 切换刷新 ========== -->
    <section
      class="sticky top-16 z-20 -mx-4 mb-6 border-b border-slate-200 dark:border-slate-700 bg-white/90 dark:bg-slate-900/90 px-4 py-4 backdrop-blur sm:-mx-6 sm:px-6"
    >
      <div class="flex items-center gap-4">
        <div class="h-16 w-24 shrink-0 overflow-hidden rounded-xl bg-slate-100 dark:bg-slate-700/60 ring-1 ring-slate-200 dark:ring-slate-700">
          <img v-if="competition.image" :src="competition.image" alt="" class="h-full w-full object-cover" />
          <div v-else class="flex h-full w-full items-center justify-center">
            <svg class="h-6 w-6 text-slate-500 dark:text-slate-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
            </svg>
          </div>
        </div>

        <div class="min-w-0">
          <h1 class="truncate text-xl font-black tracking-tight text-slate-900 dark:text-slate-100">
            {{ competition.title }}
          </h1>
          <div class="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
            <span class="rounded-full bg-blue-50 dark:bg-blue-500/15 px-2.5 py-1 font-bold text-blue-700 dark:text-blue-300">{{ zhLevel(competition.level) }}</span>
            <span class="rounded-full bg-slate-100 dark:bg-slate-700/60 px-2.5 py-1 font-bold text-slate-600 dark:text-slate-300">{{ competition.category }}</span>
            <span class="font-medium text-slate-500 dark:text-slate-400">比赛时间</span>
            <span class="font-semibold text-slate-700 dark:text-slate-200">{{ competition.contestTime }}</span>
            <span class="text-slate-300">·</span>
            <span class="font-medium text-slate-500 dark:text-slate-400">ID</span>
            <span class="font-semibold text-slate-700 dark:text-slate-200">{{ routedId || '—' }}</span>
          </div>
        </div>
      </div>

      <!-- Tab 导航 -->
      <nav class="mt-4 flex flex-nowrap items-center gap-2 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0">
        <button
          v-for="tab in visibleTabs"
          :key="tab.key"
          type="button"
          :data-tab="tab.key"
          class="shrink-0 whitespace-nowrap rounded-full px-4 py-2 text-sm font-bold transition-colors sm:px-5"
          :class="activeTab === tab.key
            ? 'bg-slate-900 text-white shadow-sm'
            : 'text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-700/60 hover:text-slate-900 dark:hover:text-white'"
          @click="activeTab = tab.key"
        >
          {{ tab.label }}
        </button>
      </nav>
    </section>

    <!-- ========== 下方内容区：随 activeTab 切换 ========== -->

    <!-- Tab 1：AI 备赛 -->
    <section v-if="activeTab === 'ai'" class="space-y-5">

      <!-- ===== 计划文档卡：生成成功后接管整个版面 ===== -->
      <article
        v-if="isShowingPlan"
        ref="planCard"
        class="scroll-mt-24 overflow-hidden rounded-2xl bg-white shadow-sm ring-1 ring-slate-200/70 dark:bg-slate-800 dark:ring-slate-700"
      >
        <header class="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 px-5 py-5 dark:border-slate-700 sm:px-8">
          <div class="min-w-0">
            <p class="text-[11px] font-black uppercase tracking-[0.18em] text-blue-600 dark:text-blue-400">
              My Preparation Plan
            </p>
            <h2 class="mt-1 text-xl font-black tracking-tight text-slate-900 dark:text-slate-100 sm:text-2xl">
              我的备赛计划
            </h2>
            <p class="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
              <span class="inline-flex items-center gap-1">
                <svg class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
                </svg>
                已保存到本地
              </span>
              <span v-if="planSavedAt">· {{ planSavedAt }}</span>
              <span v-if="cacheWarning" class="font-bold text-amber-600 dark:text-amber-400">{{ cacheWarning }}</span>
            </p>
          </div>

          <div class="flex shrink-0 items-center gap-2">
            <button
              type="button"
              class="inline-flex h-10 items-center gap-2 rounded-full bg-blue-50 px-4 text-sm font-bold text-blue-700
                     ring-1 ring-blue-200 transition-colors hover:bg-blue-100
                     dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30 dark:hover:bg-blue-500/25"
              title="全屏阅读计划"
              @click="isReaderOpen = true"
            >
              <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M4 9V5a1 1 0 011-1h4m6 0h4a1 1 0 011 1v4m0 6v4a1 1 0 01-1 1h-4m-6 0H5a1 1 0 01-1-1v-4" />
              </svg>
              <span class="hidden sm:inline">全屏阅读</span>
            </button>
            <button
              type="button"
              :disabled="isBusy"
              class="inline-flex h-10 items-center gap-2 rounded-full px-4 text-sm font-bold text-slate-500
                     ring-1 ring-slate-200 transition-colors hover:text-slate-900 hover:ring-slate-300
                     disabled:cursor-not-allowed disabled:opacity-50
                     dark:text-slate-400 dark:ring-slate-700 dark:hover:text-white dark:hover:ring-slate-600"
              title="清空当前计划并重新生成"
              @click="restartPlan"
            >
              重新生成
            </button>
          </div>
        </header>

        <!-- AI 正在增量修改（内联提示，不遮挡页面） -->
        <div
          v-if="isSending || generating"
          class="flex items-center gap-2 border-b border-blue-100 bg-blue-50/60 px-5 py-2.5 text-xs font-bold
                 text-blue-700 dark:border-blue-500/20 dark:bg-blue-500/10 dark:text-blue-300 sm:px-8"
          role="status"
          aria-live="polite"
        >
          <svg class="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
          </svg>
          {{ isSending ? 'AI 正在根据你的补充修改计划…' : 'AI 正在生成计划…' }}
        </div>

        <div class="px-5 py-7 sm:px-8 sm:py-9 lg:px-12">
          <div
            class="prose prose-lg prose-slate max-w-none dark:prose-invert
                   prose-headings:font-bold prose-headings:tracking-tight
                   prose-h1:text-2xl prose-h2:text-xl prose-h3:text-lg
                   prose-p:leading-8 prose-li:leading-8 prose-li:my-1"
            v-html="planHtml"
          ></div>
        </div>
      </article>

      <!-- ===== 当前不展示文档：首轮引导，或 ask 后回到对话 ===== -->
      <section
        v-else
        class="rounded-2xl bg-white p-5 shadow-sm ring-1 ring-slate-200/70 dark:bg-slate-800 dark:ring-slate-700 sm:p-6"
      >
        <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-4 dark:border-slate-700">
          <div class="min-w-0">
            <h2 class="text-base font-bold text-slate-900 dark:text-slate-100">
              {{ hasPlan ? '继续补充信息' : 'AI 备赛教练' }}
            </h2>
            <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">
              <template v-if="hasPlan">
                计划已生成但已切到对话视图，你可以继续提问，或随时返回查看当前计划
              </template>
              <template v-else-if="algorithmOnly">
                告诉我你的目标奖项、比赛语言和当前刷题量，我来排一份刷题路线图
              </template>
              <template v-else>
                先聊聊你的目标与时间安排，信息足够后 AI 会立刻生成计划文档
              </template>
            </p>
          </div>
          <button
            v-if="hasPlan"
            type="button"
            class="inline-flex h-10 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold
                   text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600"
            @click="showPlan"
          >
            <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6M9 8h6M7 3h7l5 5v13H7z" />
            </svg>
            查看当前计划
          </button>
          <button
            v-else
            type="button"
            :disabled="isBusy"
            class="inline-flex h-10 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold
                   text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600
                   disabled:cursor-not-allowed disabled:opacity-60"
            @click="startPlan"
          >
            <svg v-if="generating" class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
            </svg>
            <svg v-else class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round"
                d="M12 3v2m0 14v2M5 12H3m18 0h-2M7.05 7.05L5.64 5.64m12.72 12.72l-1.41-1.41M7.05 16.95l-1.41 1.41m12.72-12.72l-1.41 1.41" />
            </svg>
            {{ generating ? '生成中…' : (algorithmOnly ? '生成刷题路线图' : '生成专属备赛计划') }}
          </button>
        </div>

        <div v-if="generating && !hasPlan" class="mt-5 space-y-3" aria-busy="true">
          <div class="h-4 w-1/3 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
          <div class="h-3 w-full animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
          <div class="h-3 w-11/12 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
          <div class="h-3 w-4/5 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
          <div class="h-4 w-1/4 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
          <div class="h-3 w-full animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
          <div class="h-3 w-3/4 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
        </div>

        <div
          v-else-if="!hasPlan"
          class="mt-5 rounded-xl bg-slate-50 px-4 py-3.5 text-xs leading-relaxed text-slate-500
                 dark:bg-slate-900/60 dark:text-slate-400"
        >
          <p class="font-bold text-slate-600 dark:text-slate-300">可以直接这样告诉我：</p>
          <ul v-if="algorithmOnly" class="mt-1.5 list-disc space-y-1 pl-4">
            <li>“C++ 选手，LeetCode 做过 200 题，每天能刷 2 小时，目标省一等奖。”</li>
            <li>“二分和 DP 有基础，图论基本没刷过，距离比赛还有 2 个月。”</li>
          </ul>
          <ul v-else class="mt-1.5 list-disc space-y-1 pl-4">
            <li>“我是大二学生，数学基础一般，每天能投入 2 小时。”</li>
            <li>“目标国赛一等奖，距离比赛还有 3 个月。”</li>
          </ul>
        </div>
      </section>

      <!-- 错误提示 -->
      <div
        v-if="errorMsg"
        class="flex items-start gap-2 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700
               ring-1 ring-rose-100 dark:bg-rose-500/15 dark:text-rose-300 dark:ring-rose-500/30"
      >
        <svg class="mt-0.5 h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
        </svg>
        <span>{{ errorMsg }}</span>
      </div>

      <!-- ===== 对话记录：计划态折叠，未生成计划时直接平铺 ===== -->
      <div
        ref="transcriptEl"
        class="scroll-mt-24 rounded-2xl bg-white shadow-sm ring-1 ring-slate-200/70 dark:bg-slate-800 dark:ring-slate-700"
      >
        <button
          v-if="isShowingPlan"
          type="button"
          class="flex w-full items-center justify-between gap-3 px-5 py-3.5 text-left"
          :aria-expanded="chatOpen"
          @click="chatOpen = !chatOpen"
        >
          <span class="flex items-center gap-2 text-sm font-bold text-slate-600 dark:text-slate-300">
            <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M8 10h8M8 14h5M21 12a9 9 0 11-4.2-7.6L21 3v6h-6" />
            </svg>
            对话记录
            <span class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-bold text-slate-500 dark:bg-slate-700/60 dark:text-slate-400">
              {{ messages.length }} 条
            </span>
          </span>
          <span class="flex items-center gap-1 text-xs font-bold text-slate-500 dark:text-slate-400">
            {{ chatOpen ? '收起' : '展开' }}
            <svg
              class="h-3.5 w-3.5 transition-transform"
              :class="chatOpen ? 'rotate-180' : ''"
              fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6" />
            </svg>
          </span>
        </button>

        <div
          v-if="!isShowingPlan || chatOpen || isSending"
          class="space-y-3 px-5 pb-5 pt-4"
          :class="isShowingPlan ? 'max-h-80 overflow-y-auto border-t border-slate-100 dark:border-slate-700' : ''"
          :aria-busy="isSending"
        >
          <div
            v-for="(m, i) in messages"
            :key="i"
            class="flex gap-2"
            :class="m.role === 'user' ? 'justify-end' : 'justify-start'"
          >
            <div
              class="max-w-[85%] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-sm leading-relaxed"
              :class="m.role === 'user'
                ? 'bg-blue-600 text-white'
                : 'bg-slate-100 dark:bg-slate-700/60 text-slate-700 dark:text-slate-200'"
            >{{ m.content }}</div>
          </div>

          <!-- 内联「AI 正在思考」，不做全屏遮罩，页面其余操作始终保持可用 -->
          <div v-if="isSending" class="flex gap-2" role="status" aria-live="polite">
            <div class="flex items-center gap-2 rounded-2xl bg-slate-100 px-4 py-2.5 text-sm font-medium text-slate-500 dark:bg-slate-700/60 dark:text-slate-300">
              <svg class="h-4 w-4 animate-spin text-blue-500" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
              </svg>
              AI 正在思考…
            </div>
          </div>
        </div>
      </div>

      <!-- ===== 输入区：吸附在视口底部，长文档阅读时随时补充 ===== -->
      <div class="sticky bottom-4 z-30">
        <div
          class="rounded-2xl bg-white/95 p-3 shadow-lg shadow-slate-900/10 ring-1 ring-slate-200/80
                 backdrop-blur dark:bg-slate-800/95 dark:ring-slate-700"
        >
          <div class="flex items-center gap-2.5">
            <input
              v-model="draft"
              type="text"
              :disabled="isBusy"
              :placeholder="isSending
                ? 'AI 正在思考，请稍候…'
                : (hasPlan
                  ? '补充信息，AI 会直接修改当前计划，例如：我每天只有 2 小时'
                  : '说说你的目标、基础和每天能投入的时间…')"
              class="h-11 flex-1 rounded-full bg-slate-50 px-5 text-sm text-slate-700 dark:bg-slate-900 dark:text-slate-200
                     outline-none ring-1 ring-slate-200 dark:ring-slate-700 placeholder:text-slate-500 dark:placeholder:text-slate-500
                     focus:ring-2 focus:ring-blue-500 disabled:opacity-60"
              @keydown.enter="onEnter"
            />
            <button
              type="button"
              :disabled="isBusy || !draft.trim()"
              class="inline-flex h-11 shrink-0 items-center gap-2 rounded-full bg-blue-600 px-5 text-sm font-bold
                     text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600
                     disabled:cursor-not-allowed disabled:opacity-60 sm:px-6"
              @click="send"
            >
              <svg v-if="isBusy" class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
              </svg>
              <span v-else>{{ hasPlan ? '更新计划' : '发送' }}</span>
              <svg v-if="!isBusy" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 19V5m0 0l-7 7m7-7l7 7" />
              </svg>
            </button>
          </div>
          <p class="mt-1.5 px-2 text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
            {{ hasPlan
              ? '提交时会带上当前计划，AI 只做增量修改，改完直接更新上方文档'
              : '信息足够后 AI 会立即生成完整计划，生成后可继续在下方补充' }}
          </p>
        </div>
      </div>

      <!-- ===== 全屏阅读层 ===== -->
      <Teleport to="body">
        <div
          v-if="isReaderOpen"
          class="fixed inset-0 z-[100] overflow-y-auto bg-white dark:bg-slate-900"
          role="dialog"
          aria-modal="true"
          aria-label="备赛计划全屏阅读"
        >
          <div
            class="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-slate-200
                   bg-white/90 px-4 py-3 backdrop-blur dark:border-slate-700 dark:bg-slate-900/90 sm:px-8"
          >
            <div class="min-w-0">
              <p class="text-xs font-bold text-blue-600 dark:text-blue-400">我的备赛计划</p>
              <p class="truncate text-sm font-black text-slate-900 dark:text-slate-100">
                {{ competition.title || '备赛计划' }}
              </p>
            </div>
            <button
              type="button"
              class="inline-flex h-10 shrink-0 items-center gap-2 rounded-full px-4 text-sm font-bold
                     text-slate-600 ring-1 ring-slate-200 transition-colors hover:bg-slate-100
                     dark:text-slate-300 dark:ring-slate-700 dark:hover:bg-slate-800"
              @click="isReaderOpen = false"
            >
              <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
              退出全屏
            </button>
          </div>

          <article class="mx-auto max-w-3xl px-5 pb-24 pt-10 sm:px-8">
            <div
              class="prose prose-lg prose-slate max-w-none dark:prose-invert lg:prose-xl
                     prose-headings:font-bold prose-headings:tracking-tight
                     prose-h1:text-3xl prose-h2:text-2xl prose-h3:text-xl
                     prose-p:leading-8 prose-li:leading-8 prose-li:my-1"
              v-html="planHtml"
            ></div>
          </article>
        </div>
      </Teleport>
    </section>

    <!-- Tab 2：往年获奖作品 -->
    <section v-else-if="activeTab === 'works'">
      <!-- 顶部：一键 AI 分析全部获奖作品的共性 -->
      <div class="mb-5 rounded-2xl bg-white dark:bg-slate-800 p-5 ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm">
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div class="min-w-0">
            <h3 class="flex items-center gap-2 text-base font-bold text-slate-900 dark:text-slate-100">
              <svg class="h-5 w-5 text-blue-600 dark:text-blue-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z" />
              </svg>
              获奖作品共性分析
            </h3>
            <p class="mt-1 text-xs font-medium text-slate-500 dark:text-slate-400">
              跨赛道抽取 20-30 件获奖作品，拆解技术栈组合、系统架构套路与评审技术关注点。
            </p>
          </div>

          <button
            type="button"
            data-testid="common-analysis"
            class="inline-flex h-11 shrink-0 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold
                   text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600
                   disabled:cursor-not-allowed disabled:opacity-60"
            :disabled="commonLoading || !routedId"
            @click="runCommonAnalysis"
          >
            <svg
              v-if="commonLoading"
              class="h-4 w-4 animate-spin"
              fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M12 3a9 9 0 109 9" />
            </svg>
            AI 分析获奖作品共性
          </button>
        </div>

        <!-- 共性报告正文 -->
        <div v-if="commonLoading && !hasCommon" class="mt-5 space-y-2 rounded-2xl bg-slate-50 dark:bg-slate-900 p-4">
          <div class="h-3 w-2/3 animate-pulse rounded bg-slate-200 dark:bg-slate-700"></div>
          <div class="h-3 w-full animate-pulse rounded bg-slate-200 dark:bg-slate-700"></div>
          <div class="h-3 w-5/6 animate-pulse rounded bg-slate-200 dark:bg-slate-700"></div>
        </div>

        <div v-else-if="commonError" class="mt-5 flex items-start gap-2 rounded-2xl bg-rose-50 dark:bg-rose-500/15 p-4 text-sm text-rose-700 dark:text-rose-300">
          <svg class="mt-0.5 h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v4m0 4h.01M12 3l9 16H3z" />
          </svg>
          <span>{{ commonError }}</span>
        </div>

        <div
          v-else-if="hasCommon"
          class="markdown-body mt-5 max-w-none rounded-2xl bg-slate-50 dark:bg-slate-900 p-4 text-sm leading-relaxed text-slate-700 dark:text-slate-200"
          v-html="commonHtml"
        ></div>
      </div>

      <!-- 列表 loading / 错误 / 空 -->
      <div v-if="worksLoading" class="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="n in 6"
          :key="n"
          class="h-40 animate-pulse rounded-2xl bg-white dark:bg-slate-800 ring-1 ring-slate-200/70 dark:ring-slate-700"
        ></div>
      </div>

      <div
        v-else-if="worksError"
        class="flex items-center justify-between gap-4 rounded-2xl bg-rose-50 dark:bg-rose-500/15 p-5 text-sm text-rose-700 dark:text-rose-300"
      >
        <span>{{ worksError }}</span>
        <button
          type="button"
          class="h-9 shrink-0 rounded-full bg-white dark:bg-slate-800 px-4 text-sm font-bold text-rose-700 dark:text-rose-300 ring-1 ring-rose-200 dark:ring-rose-500/30 hover:bg-rose-100 dark:hover:bg-rose-500/20"
          @click="loadWorks"
        >
          重试
        </button>
      </div>

      <div
        v-else-if="!works.length"
        class="rounded-2xl bg-white dark:bg-slate-800 p-10 text-center text-sm text-slate-500 dark:text-slate-400 ring-1 ring-slate-200/70 dark:ring-slate-700"
      >
        暂无获奖作品数据
      </div>

      <div v-else class="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
        <article
          v-for="work in works"
          :key="work.id"
          class="group flex cursor-pointer flex-col rounded-2xl bg-white dark:bg-slate-800 p-5
                 ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm
                 transition-all duration-200 hover:-translate-y-0.5 hover:shadow-lg hover:shadow-slate-900/10"
          @click="openWork(work)"
        >
          <!-- 顶行：奖项（视觉锚点） -->
          <div class="flex items-center justify-between gap-3">
            <span
              class="inline-flex max-w-[72%] items-center gap-1.5 rounded-full
                     bg-gradient-to-r from-amber-50 to-orange-50 px-2.5 py-1
                     text-[11px] font-bold text-amber-700 ring-1 ring-amber-200/70
                     dark:from-amber-500/15 dark:to-orange-500/10 dark:text-amber-300 dark:ring-amber-500/25"
            >
              <svg class="h-3 w-3 shrink-0" fill="none" stroke="currentColor" stroke-width="2"
                   viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
                <path d="M8 21h8M12 17v4M7 4h10v4a5 5 0 01-10 0V4zM17 5h3v2a3 3 0 01-3 3M7 5H4v2a3 3 0 003 3" />
              </svg>
              <span class="truncate">{{ zhAwardGrade(work.awardGrade) || '获奖作品' }}</span>
            </span>
          </div>

          <!-- 标题：卡片视觉重心 -->
          <h3 class="mt-3 line-clamp-2 text-[18px] font-bold leading-snug tracking-tight text-slate-900 dark:text-slate-100">
            {{ work.title || '未命名作品' }}
          </h3>

          <p
            v-if="work.summary"
            class="mt-2 line-clamp-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400"
          >
            {{ work.summary }}
          </p>

          <!-- 元信息：灰底小标签（团队独占一行，年份 / 分类 / 级别 一行） -->
          <div class="mt-auto space-y-1.5 pt-4">
            <div v-if="work.team" class="flex min-w-0">
              <span :class="META_TAG_CLASS" class="min-w-0">
                <span class="min-w-0 truncate">团队 · {{ work.team }}</span>
              </span>
            </div>
            <div class="flex flex-wrap items-center gap-1.5">
              <span v-if="work.year" :class="META_TAG_CLASS" class="tabular-nums">{{ work.year }} 年</span>
              <span v-if="work.track" :class="META_TAG_CLASS">{{ work.track }}</span>
              <span v-if="work.level" :class="META_TAG_CLASS">{{ zhLevel(work.level) }}</span>
            </div>
          </div>

          <div class="mt-3 flex items-center justify-between border-t border-slate-100 pt-3 dark:border-slate-700/60">
            <span class="text-[11px] font-medium text-slate-400 dark:text-slate-500">作品档案</span>
            <span class="text-xs font-bold text-blue-600 opacity-0 transition-opacity group-hover:opacity-100 dark:text-blue-400">
              详情 →
            </span>
          </div>
        </article>
      </div>

      <!-- 加载更多：每页 20 条，全部加载完显示「没有更多数据了」 -->
      <div
        v-if="works.length && !algorithmOnly"
        class="mt-8 flex flex-col items-center gap-3"
      >
        <button
          v-if="worksHasMore"
          type="button"
          :disabled="worksLoadingMore"
          class="inline-flex min-w-[160px] items-center justify-center gap-2 rounded-full
                 border border-slate-200 bg-white px-6 py-2.5 text-sm font-bold text-slate-700
                 shadow-sm transition-colors hover:border-blue-300 hover:text-blue-600
                 disabled:cursor-not-allowed disabled:opacity-60
                 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
          @click="loadMoreWorks"
        >
          <svg
            v-if="worksLoadingMore"
            class="h-4 w-4 animate-spin"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.5"
          >
            <path d="M21 12a9 9 0 11-6.2-8.6" stroke-linecap="round" />
          </svg>
          <span>{{ worksLoadingMore ? '加载中...' : '加载更多' }}</span>
        </button>
        <span v-else class="text-xs font-medium text-slate-400 dark:text-slate-500">
          没有更多数据了
        </span>
        <span class="text-[11px] tabular-nums text-slate-400 dark:text-slate-500">
          已加载 {{ works.length }} / {{ worksTotal }} 条
        </span>
      </div>

      <!-- 作品详情弹窗 -->
      <!-- 作品详情弹窗：完整档案 -->
      <div
        v-if="selectedWork"
        class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
        @click.self="closeWork"
      >
        <div class="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-3xl bg-white dark:bg-slate-800 shadow-2xl">
          <!-- ===== 档案头部：标题 + 基础信息 ===== -->
          <div class="shrink-0 border-b border-slate-100 bg-gradient-to-br from-blue-50/80 via-white to-white
                      px-7 pb-5 pt-6 dark:border-slate-700 dark:from-slate-800 dark:via-slate-800 dark:to-slate-800">
            <div class="flex items-start justify-between gap-4">
              <div class="min-w-0">
                <h3 class="text-xl font-black leading-snug text-slate-900 dark:text-slate-100">
                  {{ selectedWork.title }}
                </h3>
                <!-- 基础信息徽章行：年份 / 奖项 / 组别 / 赛道 / 场次 -->
                <div class="mt-3 flex flex-wrap items-center gap-2">
                  <span
                    v-if="selectedWork.year"
                    class="rounded-full bg-slate-900 px-3 py-1 text-xs font-bold text-white"
                  >
                    {{ selectedWork.year }}
                  </span>
                  <span
                    v-if="selectedWork.awardGrade"
                    class="rounded-full bg-amber-100 dark:bg-amber-500/20 px-3 py-1 text-xs font-bold text-amber-800 ring-1 ring-amber-200 dark:ring-amber-500/30"
                  >
                    {{ zhAwardGrade(selectedWork.awardGrade) }}
                  </span>
                  <span
                    v-if="selectedWork.level"
                    class="rounded-full bg-emerald-50 dark:bg-emerald-500/15 px-3 py-1 text-xs font-bold text-emerald-700 dark:text-emerald-300 ring-1 ring-emerald-200 dark:ring-emerald-500/30"
                  >
                    {{ zhLevel(selectedWork.level) }}
                  </span>
                  <span
                    v-if="selectedWork.track"
                    class="rounded-full bg-indigo-50 dark:bg-indigo-500/15 px-3 py-1 text-xs font-semibold text-indigo-700 ring-1 ring-indigo-200 dark:text-indigo-300 dark:ring-indigo-500/30"
                  >
                    {{ selectedWork.track }}
                  </span>
                  <span
                    v-if="selectedWork.session"
                    class="rounded-full bg-slate-100 dark:bg-slate-700/60 px-3 py-1 text-xs font-semibold text-slate-600 dark:text-slate-300"
                  >
                    {{ selectedWork.session }}
                  </span>
                </div>
                <p v-if="selectedWork.team" class="mt-2.5 flex items-center gap-1.5 text-xs font-medium text-slate-500 dark:text-slate-400">
                  <svg class="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M17 20h5v-1a4 4 0 00-3-3.87M9 20H4v-1a4 4 0 013-3.87m6-1.13a4 4 0 10-4-4 4 4 0 004 4zm6-4a3 3 0 11-3-3" />
                  </svg>
                  {{ selectedWork.team }}
                </p>
              </div>
              <button
                type="button"
                aria-label="关闭作品详情"
                class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-slate-500 dark:text-slate-400
                       transition-colors hover:bg-slate-100 dark:hover:bg-slate-700/60 hover:text-slate-600 dark:hover:text-slate-300"
                @click="closeWork"
              >
                <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>

          <!-- ===== 档案正文（可滚动） ===== -->
          <div class="min-h-0 flex-1 overflow-y-auto px-7 py-6">
            <!-- 一、作品简介 -->
            <section>
              <h4 class="mb-2.5 flex items-center gap-2 text-sm font-black text-slate-900 dark:text-slate-100">
                <span class="flex h-5 w-5 items-center justify-center rounded-md bg-blue-600 text-[11px] font-black text-white">1</span>
                作品简介
              </h4>
              <p
                v-if="selectedWork.summary"
                class="whitespace-pre-line text-sm leading-7 text-slate-600 dark:text-slate-300"
              >{{ selectedWork.summary }}</p>
              <p v-else class="rounded-xl bg-slate-50 dark:bg-slate-900 px-4 py-3 text-sm text-slate-500 dark:text-slate-400">
                暂无简介，待数据接入后补充。
              </p>
            </section>

            <!-- 二、核心技术栈 -->
            <section class="mt-7">
              <h4 class="mb-2.5 flex items-center gap-2 text-sm font-black text-slate-900 dark:text-slate-100">
                <span class="flex h-5 w-5 items-center justify-center rounded-md bg-violet-600 text-[11px] font-black text-white">2</span>
                核心技术栈
              </h4>
              <div v-if="selectedWork.techStack && selectedWork.techStack.length" class="flex flex-wrap gap-2">
                <span
                  v-for="(t, i) in selectedWork.techStack"
                  :key="t + i"
                  class="rounded-lg px-3 py-1.5 text-xs font-bold ring-1"
                  :class="techTagClass(i)"
                >
                  {{ t }}
                </span>
              </div>
              <p v-else class="rounded-xl bg-slate-50 dark:bg-slate-900 px-4 py-3 text-sm text-slate-500 dark:text-slate-400">
                暂无技术栈信息。
              </p>
            </section>

            <!-- 三、核心亮点与创新点 -->
            <section class="mt-7">
              <h4 class="mb-2.5 flex items-center gap-2 text-sm font-black text-slate-900 dark:text-slate-100">
                <span class="flex h-5 w-5 items-center justify-center rounded-md bg-amber-500 text-[11px] font-black text-white">3</span>
                核心亮点与创新点
              </h4>
              <div
                v-if="selectedWork.highlights"
                class="rounded-2xl border border-amber-200/70 dark:border-amber-500/30 bg-amber-50/50 dark:bg-amber-500/10 px-5 py-4 text-sm leading-7 text-slate-700 dark:text-slate-200 whitespace-pre-line"
              >{{ selectedWork.highlights }}</div>
              <p v-else class="rounded-xl bg-slate-50 dark:bg-slate-900 px-4 py-3 text-sm text-slate-500 dark:text-slate-400">
                暂无亮点描述。
              </p>
            </section>

            <!-- 四、AI 深度解析 -->
            <section class="mt-7">
              <h4 class="mb-2.5 flex items-center gap-2 text-sm font-black text-slate-900 dark:text-slate-100">
                <span class="flex h-5 w-5 items-center justify-center rounded-md bg-slate-900 text-[10px] font-black text-white">AI</span>
                智能解析
              </h4>

              <!-- 解析中 -->
              <div v-if="workParsing" class="flex items-center gap-3 rounded-2xl bg-blue-50/70 dark:bg-blue-500/15 p-4">
                <svg class="h-5 w-5 animate-spin text-blue-600 dark:text-blue-400" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                <p class="text-sm font-bold text-blue-800">AI 正在解析作品结构…</p>
              </div>

              <!-- 解析失败 -->
              <div v-else-if="parseError" class="flex items-start gap-2 rounded-2xl bg-rose-50 dark:bg-rose-500/15 p-4 text-sm text-rose-700 dark:text-rose-300">
                <svg class="mt-0.5 h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v4m0 4h.01M12 3l9 16H3z" />
                </svg>
                <span>{{ parseError }}</span>
              </div>

              <!-- 解析结果 -->
              <div
                v-else-if="parsedWork === selectedWork.id && parsedText"
                class="ai-markdown rounded-2xl border border-slate-200/80 dark:border-slate-700 bg-white dark:bg-slate-800 px-5 py-4"
                v-html="parsedHtml"
              ></div>

              <!-- 未解析提示 -->
              <div v-else class="rounded-2xl border border-dashed border-slate-200 dark:border-slate-700 bg-slate-50/60 dark:bg-slate-800/60 px-4 py-5 text-center text-sm text-slate-500 dark:text-slate-400">
                点击右下角「AI 解析」，了解这篇作品的整体思路、模型选择与写作亮点。
              </div>
            </section>

            <!-- 五、查看完整原文（预留入口） -->
            <section class="mt-7">
              <a
                href="#"
                class="group flex items-center justify-between rounded-2xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 px-5 py-4 transition-colors"
                :class="hasOriginal ? 'hover:border-blue-300 hover:bg-blue-50 dark:hover:bg-blue-500/15' : 'cursor-not-allowed opacity-70'"
                :aria-disabled="!hasOriginal"
                @click.prevent="openOriginal"
              >
                <div class="flex items-center gap-3">
                  <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-white dark:bg-slate-800 ring-1 ring-slate-200 dark:ring-slate-700">
                    <svg class="h-4 w-4 text-slate-500 dark:text-slate-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6M9 8h6M7 3h7l5 5v13H7z" />
                    </svg>
                  </span>
                  <div class="text-left">
                    <p class="text-sm font-bold text-slate-800 dark:text-slate-100">查看完整原文 / 论文</p>
                    <p class="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                      {{ hasOriginal ? '在新窗口打开作品原文' : '后续接入爬虫后可用' }}
                    </p>
                  </div>
                </div>
                <span
                  class="rounded-full px-3 py-1 text-xs font-bold"
                  :class="hasOriginal ? 'bg-blue-600 text-white' : 'bg-slate-200 dark:bg-slate-700 text-slate-500 dark:text-slate-400'"
                >
                  {{ hasOriginal ? '打开 →' : '暂未收录' }}
                </span>
              </a>

              <!-- 暂无原文时的内联提示 -->
              <p
                v-if="originalTip"
                class="mt-2 flex items-start gap-1.5 rounded-xl bg-amber-50 dark:bg-amber-500/15 px-3 py-2 text-xs font-medium text-amber-700 dark:text-amber-300"
              >
                <svg class="mt-0.5 h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4m0 4h.01M12 3l9 16H3z" />
                </svg>
                <span>{{ originalTip }}</span>
              </p>
            </section>
          </div>

          <!-- ===== 档案底部操作栏 ===== -->
          <div class="flex shrink-0 items-center justify-end gap-3 border-t border-slate-100 dark:border-slate-700 bg-white dark:bg-slate-800 px-7 py-4">
            <button
              type="button"
              class="h-10 rounded-full px-5 text-sm font-bold text-slate-500 dark:text-slate-400 transition-colors hover:bg-slate-100 dark:hover:bg-slate-700/60"
              @click="closeWork"
            >
              关闭
            </button>
            <button
              type="button"
              class="inline-flex h-10 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold
                     text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600
                     disabled:cursor-not-allowed disabled:opacity-60"
              :disabled="workParsing"
              @click="parseWork"
            >
              <svg v-if="workParsing" class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              AI 解析
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- Tab 3：灵感工作台 -->
    <section v-else>
      <!-- 顶部提示语 + 切换/刷新 -->
      <div class="mb-5 flex flex-wrap items-center justify-between gap-4 rounded-2xl bg-blue-50/70 dark:bg-blue-500/15 p-5 ring-1 ring-blue-100 dark:ring-blue-500/30">
        <div>
          <p class="text-sm font-bold text-blue-900">
            {{ competition.title || '该赛事' }} · {{ algorithmOnly ? '算法竞技赛事' : '专属灵感工作台' }}
          </p>
          <p v-if="algorithmOnly" class="mt-1 text-xs font-medium text-blue-700/70">
            该赛事只公示个人获奖名单、没有项目作品，平台不做作品与灵感分析；备赛请直接使用「AI备赛」生成刷题路线。
          </p>
          <p v-else class="mt-1 text-xs font-medium text-blue-700/70">
            只展示当前赛事真实获奖作品生成的洞察与选题；切换赛事不会混入其它比赛的数据。
            已收藏 {{ favoriteList.length }} 条灵感，收藏内容保存在本地。
          </p>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <button
            type="button"
            class="inline-flex items-center gap-2 rounded-full px-4 py-2 text-xs font-bold transition-colors"
            :class="showFavorites
              ? 'bg-blue-600 text-white shadow-sm shadow-blue-600/25'
              : 'bg-white dark:bg-slate-800 text-blue-700 dark:text-blue-300 ring-1 ring-blue-200 dark:ring-blue-500/30 hover:bg-blue-100 dark:hover:bg-blue-500/25'"
            @click="showFavorites = !showFavorites"
          >
            <svg class="h-4 w-4" :fill="showFavorites ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round"
                d="M11.48 3.5l2.31 4.68 5.16.75-3.73 3.64.88 5.14-4.62-2.43-4.62 2.43.88-5.14L3.01 8.93l5.16-.75z" />
            </svg>
            {{ showFavorites ? '查看全部' : '我的收藏' }}
          </button>
          <button
            v-if="!algorithmOnly"
            type="button"
            class="inline-flex items-center gap-2 rounded-full px-4 py-2 text-xs font-bold transition-colors"
            :class="canGenerateInspiration
              ? 'bg-blue-600 text-white shadow-sm shadow-blue-600/25 hover:bg-blue-700 dark:hover:bg-blue-600'
              : 'cursor-not-allowed bg-slate-200 text-slate-400 dark:bg-slate-700 dark:text-slate-500'"
            :disabled="!canGenerateInspiration"
            :title="awardsCount > 0 ? '' : '该赛事暂无公开的历年获奖作品，AI 缺少提炼素材'"
            @click="onGenerateInspiration"
          >
            <svg v-if="inspireBusy" class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
            </svg>
            <svg v-else class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round"
                d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
            </svg>
            {{ inspireBusy ? 'AI 正在深度挖掘获奖套路…' : '生成专属灵感' }}
          </button>
          <button
            type="button"
            class="inline-flex items-center gap-2 rounded-full bg-white dark:bg-slate-800 px-4 py-2 text-xs font-bold
                   text-blue-700 dark:text-blue-300 ring-1 ring-blue-200 dark:ring-blue-500/30 transition-colors hover:bg-blue-100 dark:hover:bg-blue-500/25"
            @click="refreshInspiration"
          >
            <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h5M20 20v-5h-5M20 9a8 8 0 00-14.9-2M4 15a8 8 0 0014.9 2" />
            </svg>
            刷新
          </button>
        </div>
      </div>

      <!-- 加载中 / AI 提炼中 -->
      <div
        v-if="inspirationLoading || inspireBusy"
        class="flex flex-col items-center justify-center gap-3 rounded-2xl bg-white dark:bg-slate-800 py-16 text-center text-sm font-bold text-slate-500 dark:text-slate-400 ring-1 ring-slate-200/70 dark:ring-slate-700"
      >
        <svg class="h-5 w-5 animate-spin text-blue-600" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
        </svg>
        <span>{{ inspireBusy ? 'AI 正在深度挖掘获奖套路…' : '正在加载该赛事的灵感数据…' }}</span>
        <span v-if="inspireBusy" class="max-w-md text-xs font-medium leading-5 text-slate-400 dark:text-slate-500">
          正在通读该赛事 {{ awardsCount }} 件获奖作品，提炼评审偏好、选题趋势与可复用的创新点，通常需要 1–3 分钟。
        </span>
      </div>

      <!-- 加载失败 -->
      <div
        v-else-if="inspirationError"
        class="flex flex-wrap items-center justify-between gap-4 rounded-2xl bg-rose-50 dark:bg-rose-500/15 p-5 text-sm text-rose-700 dark:text-rose-300 ring-1 ring-rose-100 dark:ring-rose-500/30"
      >
        <span>{{ inspirationError }}</span>
        <button
          type="button"
          class="h-9 shrink-0 rounded-full bg-white dark:bg-slate-800 px-4 text-sm font-bold text-rose-700 dark:text-rose-300 ring-1 ring-rose-200 dark:ring-rose-500/30 hover:bg-rose-100 dark:hover:bg-rose-500/20"
          @click="loadInspirations"
        >
          重试
        </button>
      </div>

      <!-- 收藏视图：空状态 -->
      <div
        v-else-if="showFavorites && favoriteList.length === 0"
        class="flex flex-col items-center justify-center rounded-2xl bg-white dark:bg-slate-800 py-16 text-center ring-1 ring-slate-200/70 dark:ring-slate-700"
      >
        <div class="flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-50 dark:bg-amber-500/15">
          <svg class="h-7 w-7 text-amber-500" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round"
              d="M11.48 3.5l2.31 4.68 5.16.75-3.73 3.64.88 5.14-4.62-2.43-4.62 2.43.88-5.14L3.01 8.93l5.16-.75z" />
          </svg>
        </div>
        <h3 class="mt-4 text-base font-bold text-slate-900 dark:text-slate-100">还没有收藏的灵感</h3>
        <p class="mt-2 text-sm text-slate-500 dark:text-slate-400">在灵感卡片上点击「收藏」，即可在这里集中查看。</p>
        <button
          type="button"
          class="mt-5 h-10 rounded-full bg-blue-600 px-6 text-sm font-bold text-white
                 shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600"
          @click="showFavorites = false"
        >
          去看看灵感
        </button>
      </div>

      <!-- 纯算法/知识类赛事：不做项目灵感分析，引导去刷题 -->
      <div
        v-else-if="algorithmOnly"
        class="flex flex-col items-center justify-center rounded-2xl bg-white dark:bg-slate-800 px-6 py-16 text-center ring-1 ring-slate-200/70 dark:ring-slate-700"
      >
        <div class="flex h-14 w-14 items-center justify-center rounded-2xl bg-indigo-50 dark:bg-indigo-500/15">
          <svg class="h-7 w-7 text-indigo-500" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round"
              d="M16 18l6-6-6-6M8 6l-6 6 6 6M14.5 4l-5 16" />
          </svg>
        </div>
        <h3 class="mt-4 text-base font-bold text-slate-900 dark:text-slate-100">算法竞技赛事，无需作品与灵感分析</h3>
        <p class="mt-2 max-w-xl text-sm leading-6 text-slate-500 dark:text-slate-400">
          该赛事为算法竞技类，考察编程基本功与算法能力，只公示个人获奖名单、没有项目作品。平台不做作品与灵感分析，请直接使用「AI备赛」生成刷题路线图。
        </p>
      </div>

      <!-- 该赛事暂无灵感数据 -->
      <div
        v-else-if="!showFavorites && inspirations.length === 0"
        class="flex flex-col items-center justify-center rounded-2xl bg-white dark:bg-slate-800 py-16 text-center ring-1 ring-slate-200/70 dark:ring-slate-700"
      >
        <div
          class="flex h-14 w-14 items-center justify-center rounded-2xl"
          :class="awardsCount > 0 ? 'bg-blue-50 dark:bg-blue-500/15' : 'bg-amber-50 dark:bg-amber-500/15'"
        >
          <svg
            class="h-7 w-7"
            :class="awardsCount > 0 ? 'text-blue-500' : 'text-amber-500'"
            fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"
          >
            <path v-if="awardsCount > 0" stroke-linecap="round" stroke-linejoin="round"
              d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
            <path v-else stroke-linecap="round" stroke-linejoin="round"
              d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
          </svg>
        </div>

        <template v-if="awardsCount > 0">
          <h3 class="mt-4 text-base font-bold text-slate-900 dark:text-slate-100">还没有生成专属灵感</h3>
          <p class="mt-2 max-w-xl text-sm leading-6 text-slate-500 dark:text-slate-400">
            平台已收录该赛事 {{ awardsCount }} 件获奖作品。点一下，AI 会通读这些作品，
            提炼评审偏好、选题趋势与可以直接借鉴的创新点。
          </p>
          <div class="mt-5 flex flex-wrap items-center justify-center gap-3">
            <button
              type="button"
              class="inline-flex h-10 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold text-white
                     shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600
                     disabled:cursor-not-allowed disabled:opacity-60"
              :disabled="!canGenerateInspiration"
              @click="onGenerateInspiration"
            >
              <svg v-if="inspireBusy" class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
              </svg>
              生成专属灵感
            </button>
            <button
              type="button"
              class="h-10 rounded-full px-5 text-sm font-bold text-slate-500 dark:text-slate-400 transition-colors hover:bg-slate-100 dark:hover:bg-slate-700/60"
              @click="refreshInspiration"
            >
              刷新看看
            </button>
          </div>
        </template>

        <template v-else>
          <h3 class="mt-4 text-base font-bold text-slate-900 dark:text-slate-100">该赛事暂无灵感素材</h3>
          <p class="mt-2 max-w-xl text-sm leading-6 text-slate-500 dark:text-slate-400">
            该赛事暂无公开的历年获奖作品，AI 缺少提炼素材。请等待平台数据补全后再试。
          </p>
          <button
            type="button"
            class="mt-5 inline-flex h-10 cursor-not-allowed items-center gap-2 rounded-full bg-slate-200 px-6 text-sm font-bold
                   text-slate-400 dark:bg-slate-700 dark:text-slate-500"
            disabled
            title="该赛事暂无公开的历年获奖作品，AI 缺少提炼素材"
          >
            生成专属灵感
          </button>
          <p class="mt-3 text-xs font-medium text-slate-400 dark:text-slate-500">
            已有获奖作品公示的赛事，会随数据补全自动解锁这个按钮。
          </p>
        </template>

        <p v-if="inspireError" class="mt-4 text-xs font-bold text-rose-600 dark:text-rose-400">
          {{ inspireError }}
        </p>
      </div>

      <!-- 灵感卡片列表（全部 / 收藏）：按板块分区渲染 -->
      <div v-else class="space-y-8">
        <!-- ===== 板块一：赛事洞察（经验 / 趋势 / 空白） ===== -->
        <section v-if="visibleInsights.length">
          <header class="mb-4">
            <h2 class="flex items-center gap-2 text-lg font-black tracking-tight text-slate-900 dark:text-slate-100">
              <span aria-hidden="true">📊</span>
              赛事洞察
              <span class="rounded-full bg-slate-100 dark:bg-slate-700/60 px-2 py-0.5 text-[11px] font-bold text-slate-500 dark:text-slate-400">
                {{ visibleInsights.length }} 条
              </span>
            </h2>
            <p class="mt-1 text-xs font-medium text-slate-500 dark:text-slate-400">
              从历年获奖作品中提炼的获奖经验、选题趋势与空白机会
            </p>
          </header>

          <div class="grid grid-cols-1 gap-5 md:grid-cols-2">
            <article
              v-for="item in visibleInsights"
              :key="item.key"
              class="flex flex-col rounded-2xl bg-white dark:bg-slate-800 p-5 ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm transition-all
                     hover:shadow-lg hover:shadow-slate-900/10"
            >
              <span
                class="mb-3 w-fit rounded-full px-2.5 py-1 text-xs font-bold ring-1"
                :class="insightTagClass(item.kind)"
              >
                {{ item.tag }}
              </span>

              <div
                v-if="item.html"
                class="markdown-body text-sm leading-relaxed text-slate-700 dark:text-slate-200"
                v-html="item.html"
              ></div>

              <p v-if="item.refs && item.refs.length" class="mt-3 text-[11px] font-medium text-slate-500 dark:text-slate-400">
                参考样本 ID：{{ item.refs.join('、') }}
              </p>

              <div class="mt-auto flex items-center justify-end pt-4">
                <button
                  type="button"
                  class="inline-flex items-center gap-2 rounded-full px-4 py-1.5 text-xs font-bold transition-colors"
                  :class="item.favorite
                    ? 'bg-amber-50 dark:bg-amber-500/15 text-amber-700 dark:text-amber-300 ring-1 ring-amber-200 dark:ring-amber-500/30'
                    : 'text-slate-500 dark:text-slate-400 ring-1 ring-slate-200 dark:ring-slate-700 hover:text-slate-900 dark:hover:text-white hover:ring-slate-300 dark:hover:ring-slate-600'"
                  @click="toggleFavorite(item)"
                >
                  <svg class="h-4 w-4" :fill="item.favorite ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M11.48 3.5l2.31 4.68 5.16.75-3.73 3.64.88 5.14-4.62-2.43-4.62 2.43.88-5.14L3.01 8.93l5.16-.75z" />
                  </svg>
                  {{ item.favorite ? '已收藏' : '收藏' }}
                </button>
              </div>
            </article>
          </div>
        </section>

        <!-- ===== 板块二：选题灵感（idea） ===== -->
        <section v-if="visibleIdeas.length">
          <header class="mb-4">
            <h2 class="flex items-center gap-2 text-lg font-black tracking-tight text-slate-900 dark:text-slate-100">
              <span aria-hidden="true">💡</span>
              选题灵感
              <span class="rounded-full bg-slate-100 dark:bg-slate-700/60 px-2 py-0.5 text-[11px] font-bold text-slate-500 dark:text-slate-400">
                {{ visibleIdeas.length }} 条
              </span>
            </h2>
            <p class="mt-1 text-xs font-medium text-slate-500 dark:text-slate-400">
              基于该赛事历年获奖方向衍生的可直接落地的选题方案
            </p>
          </header>

          <div class="grid grid-cols-1 gap-5 md:grid-cols-2">
            <article
              v-for="item in visibleIdeas"
              :key="item.key"
              class="flex flex-col rounded-2xl bg-white dark:bg-slate-800 p-5 ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm transition-all
                     hover:shadow-lg hover:shadow-slate-900/10"
            >
              <span class="mb-3 w-fit rounded-full bg-violet-50 dark:bg-violet-500/15 px-2.5 py-1 text-xs font-bold text-violet-700 dark:text-violet-300 ring-1 ring-violet-200 dark:ring-violet-500/30">
                {{ item.tag }}
              </span>

              <h3 class="text-base font-black leading-snug text-slate-900 dark:text-slate-100">
                {{ item.title }}
              </h3>
              <p v-if="item.text" class="mt-1.5 text-sm leading-relaxed text-slate-700 dark:text-slate-200">
                {{ item.text }}
              </p>

              <details
                v-if="item.detailHtml"
                class="mt-3 rounded-xl bg-slate-50 dark:bg-slate-900/60 px-3 py-2"
              >
                <summary class="cursor-pointer text-xs font-bold text-blue-600 dark:text-blue-400">展开方案细节</summary>
                <div class="markdown-body mt-2 text-sm" v-html="item.detailHtml"></div>
              </details>

              <p v-if="item.novelty" class="mt-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                <span class="font-bold text-slate-700 dark:text-slate-200">差异化：</span>{{ item.novelty }}
              </p>
              <p v-if="item.risk" class="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                <span class="font-bold text-slate-700 dark:text-slate-200">风险：</span>{{ item.risk }}
              </p>
              <p v-if="item.refs && item.refs.length" class="mt-2 text-[11px] font-medium text-slate-500 dark:text-slate-400">
                参考样本 ID：{{ item.refs.join('、') }}
              </p>

              <div class="mt-auto flex items-center justify-end pt-4">
                <button
                  type="button"
                  class="inline-flex items-center gap-2 rounded-full px-4 py-1.5 text-xs font-bold transition-colors"
                  :class="item.favorite
                    ? 'bg-amber-50 dark:bg-amber-500/15 text-amber-700 dark:text-amber-300 ring-1 ring-amber-200 dark:ring-amber-500/30'
                    : 'text-slate-500 dark:text-slate-400 ring-1 ring-slate-200 dark:ring-slate-700 hover:text-slate-900 dark:hover:text-white hover:ring-slate-300 dark:hover:ring-slate-600'"
                  @click="toggleFavorite(item)"
                >
                  <svg class="h-4 w-4" :fill="item.favorite ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M11.48 3.5l2.31 4.68 5.16.75-3.73 3.64.88 5.14-4.62-2.43-4.62 2.43.88-5.14L3.01 8.93l5.16-.75z" />
                  </svg>
                  {{ item.favorite ? '已收藏' : '收藏' }}
                </button>
              </div>
            </article>
          </div>

          <!-- ✨ 换一批新灵感：把当前列表（id + 标题）传给后端避开重复，结果追加到尾部 -->
          <div class="mt-6 flex flex-col items-center gap-2">
            <button
              type="button"
              data-testid="more-ideas"
              class="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-violet-600 to-fuchsia-600 px-6 py-2.5 text-sm font-black text-white shadow-lg shadow-violet-500/25 transition-all
                     hover:-translate-y-0.5 hover:shadow-xl hover:shadow-violet-500/30
                     disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:translate-y-0"
              :disabled="moreIdeasLoading || algorithmOnly"
              @click="loadMoreIdeas"
            >
              <span
                v-if="moreIdeasLoading"
                class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
              ></span>
              <span v-else aria-hidden="true">✨</span>
              {{ moreIdeasLoading ? '正在构思新选题…' : '换一批新灵感' }}
            </button>
            <p v-if="moreIdeasError" class="text-xs font-medium text-rose-500">{{ moreIdeasError }}</p>
            <p
              v-else-if="moreIdeasTip"
              class="text-xs font-medium text-emerald-600 dark:text-emerald-400"
            >{{ moreIdeasTip }}</p>
            <p v-else class="text-[11px] font-medium text-slate-500 dark:text-slate-400">
              点一次生成 4 条全新选题（自动避开已展示的 {{ visibleIdeas.length }} 条），越点越多
            </p>
          </div>
        </section>
      </div>
    </section>
    </main>
  </div>
</template>

<style scoped>
/* Markdown 计划正文排版
 *
 * 注意：内容由 v-html 注入，这些节点不会带 scoped 的 data-v 属性。
 * 因此所有“后代选择器”必须用 :deep() 包裹，否则编译成
 * `.markdown-body h1[data-v-xxx]` 后永远匹配不到 v-html 里的 h1。
 */
.markdown-body { font-size: 14px; line-height: 1.75; color: #334155; }
:deep(.markdown-body > *:first-child) { margin-top: 0; }
:deep(.markdown-body > *:last-child) { margin-bottom: 0; }
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  font-weight: 700; color: #0f172a; line-height: 1.35; margin: 1.25em 0 0.6em;
}
.markdown-body :deep(h1) { font-size: 1.35em; }
.markdown-body :deep(h2) { font-size: 1.18em; }
.markdown-body :deep(h3) { font-size: 1.05em; }
.markdown-body :deep(h4) { font-size: 1em; }
.markdown-body :deep(p) { margin: 0.6em 0; }
.markdown-body :deep(ul),
.markdown-body :deep(ol) { margin: 0.6em 0; padding-left: 1.4em; }
.markdown-body :deep(ul) { list-style: disc; }
.markdown-body :deep(ol) { list-style: decimal; }
.markdown-body :deep(li) { margin: 0.25em 0; }
.markdown-body :deep(strong) { font-weight: 700; color: #0f172a; }
.markdown-body :deep(a) { color: #2563eb; text-decoration: underline; }
.markdown-body :deep(code) {
  background: #e2e8f0; border-radius: 4px; padding: 0.1em 0.35em;
  font-size: 0.9em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.markdown-body :deep(pre) {
  background: #0f172a; color: #e2e8f0; border-radius: 10px;
  padding: 12px 14px; overflow-x: auto; margin: 0.8em 0;
}
.markdown-body :deep(pre code) { background: transparent; padding: 0; color: inherit; }
.markdown-body :deep(blockquote) {
  border-left: 3px solid #93c5fd; color: #475569;
  margin: 0.8em 0; padding: 0.2em 0 0.2em 1em;
}
.markdown-body :deep(table) { width: 100%; border-collapse: collapse; margin: 0.8em 0; font-size: 0.95em; }
.markdown-body :deep(th),
.markdown-body :deep(td) { border: 1px solid #e2e8f0; padding: 6px 10px; text-align: left; }
.markdown-body :deep(th) { background: #f1f5f9; font-weight: 700; }
.markdown-body :deep(hr) { border: 0; border-top: 1px solid #e2e8f0; margin: 1.2em 0; }

/* ===== 暗色模式覆盖：AI 解析区在深色下必须保持足够对比度 ===== */
.dark .markdown-body { color: #cbd5e1; }
.dark .markdown-body :deep(h1),
.dark .markdown-body :deep(h2),
.dark .markdown-body :deep(h3),
.dark .markdown-body :deep(h4),
.dark .markdown-body :deep(strong) { color: #f1f5f9; }
.dark .markdown-body :deep(a) { color: #60a5fa; }
.dark .markdown-body :deep(code) { background: #334155; color: #e2e8f0; }
.dark .markdown-body :deep(pre) { background: #020617; color: #e2e8f0; border: 1px solid #334155; }
.dark .markdown-body :deep(pre code) { background: transparent; color: inherit; }
.dark .markdown-body :deep(blockquote) { border-left-color: #3b82f6; color: #94a3b8; }
.dark .markdown-body :deep(th),
.dark .markdown-body :deep(td) { border-color: #334155; }
.dark .markdown-body :deep(th) { background: #1e293b; color: #f1f5f9; }
.dark .markdown-body :deep(hr) { border-top-color: #334155; }
</style>

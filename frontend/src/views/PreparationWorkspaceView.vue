<script setup>
import { ref, computed, reactive, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import Header from '@/components/Header.vue'
import { analyzeWork, analyzeWorks, generatePlan, getCompetitionAwards, getCompetitionDetail } from '@/services/api'
import { renderMarkdown } from '@/services/markdown'
import {
  formatSavedAt,
  getCommonAnalysis,
  getWorkAnalysis,
  setCommonAnalysis,
  setWorkAnalysis
} from '@/services/analysisCache'

const route = useRoute()
// 当前竞赛 ID（路由参数），供 AI 接口使用
const routedId = computed(() => route.params.id)

// ---- 顶部竞赛信息（按路由真实 id 拉取详情，不随 Tab 切换重复请求）----
const competition = reactive({
  title: '',
  level: '',
  category: '',
  contestTime: '',
  image: ''
})
const compLoading = ref(true)
const compError = ref('')

function applyDetail(d) {
  if (!d) return
  competition.title = d.title || d.name || ''
  competition.level = d.level || d.grade || ''
  competition.category = d.category || d.type || ''
  competition.image = d.image || d.cover || d.coverUrl || ''
  const startT = d.contestStart || d.startTime || d.contestTime || d.registrationStart || ''
  const endT = d.contestEnd || d.endTime || d.registrationEnd || ''
  competition.contestTime = startT && endT ? startT + ' 至 ' + endT : (startT || endT || '')
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
    applyDetail(d && d.data ? d.data : d)
    if (!competition.title) compError.value = '未获取到竞赛信息'
  } catch (e) {
    compError.value = (e && e.message) || '竞赛信息加载失败'
  } finally {
    compLoading.value = false
  }
}

// 路由切换到另一场竞赛时，重新拉取顶部信息
watch(routedId, () => {
  loadCompetition()
  loadWorks()
  // 竞赛变了，Tab1 的计划/对话属于上一场赛事，必须清空
  resetPlanState()
  // 竞赛变了，之前打开的作品弹窗不再属于当前赛事
  closeWork()
  // 共性报告属于竞赛级数据，先清空再尝试回填新竞赛的缓存
  resetCommonAnalysis()
  restoreCommonAnalysis()
})

// ---- Tab \u5207\u6362\uff08\u54cd\u5e94\u5f0f\u53d8\u91cf\uff09----
const tabs = [
  { key: 'ai', label: 'AI\u5907\u8d5b' },
  { key: 'works', label: '\u5f80\u5e74\u83b7\u5956\u4f5c\u54c1' },
  { key: 'idea', label: '\u7075\u611f\u5de5\u4f5c\u53f0' }
]
const activeTab = ref('ai')

/* =========================================================
   Tab1：AI 备赛计划（真实接口：渐进式多轮对话）
   ---------------------------------------------------------
   后端 /api/ai/plan 三种返回：
     status = 'ask'    -> 反问，追加到对话气泡，等待用户作答
     status = 'plan'   -> 首次生成完整计划，渲染为 Markdown 卡片
     status = 'refine' -> 在原计划上增量修改，原地替换计划正文
   ========================================================= */

// 对话气泡（仅展示追问 / 提示，不含计划正文）
const messages = ref([
  {
    role: 'assistant',
    content:
      '你好，我是你的专属备赛教练。为了给你量身定制计划，' +
      '请告诉我你的备赛目标、基础情况以及每天能投入的时间。'
  }
])

// 当前完整计划（Markdown 正文）；非空即视为「已有计划」，后续请求走 refine
const plan = ref('')
const hasPlan = computed(() => plan.value.trim().length > 0)
const planHtml = computed(() => (plan.value.trim() ? renderMarkdown(plan.value) : ''))

// 输入框
const draft = ref('')

// loading / 错误
const generating = ref(false)
const errorMsg = ref('')

// 多轮对话历史：每轮 {"user": "...", "assistant": "..."}，拼接后作为 user_input
const history = ref([])

const planBody = ref(null)

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
  messages.value = [
    {
      role: 'assistant',
      content:
        '你好，我是你的专属备赛教练。为了给你量身定制计划，' +
        '请告诉我你的备赛目标、基础情况以及每天能投入的时间。'
    }
  ]
  draft.value = ''
  errorMsg.value = ''
  generating.value = false
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
    currentPlan: hasPlan.value ? plan.value : null
  })

  // 竞赛已切换 → 本次结果作废，不写 plan / messages / history
  if (routedId.value !== cid) return

  const status = res?.status || 'plan'
  const reply = (res?.reply || '').trim()

  if (!reply) throw new Error('AI 未返回内容，请稍后重试')

  if (status === 'ask') {
    // 反问：只进对话气泡，不产生计划
    messages.value.push({ role: 'assistant', content: reply })
    history.value.push({ user: latestInput, assistant: reply })
    return 'ask'
  }

  // plan / refine：正文即完整计划，原地替换
  const isRefine = status === 'refine'
  plan.value = reply
  history.value.push({
    user: latestInput,
    assistant: isRefine ? '（已根据你的补充更新完整计划）' : '（已生成完整备赛计划）'
  })
  messages.value.push({
    role: 'assistant',
    content: isRefine ? '已根据你的补充更新计划，请查看上方计划区域。' : '已为你生成备赛计划，可继续补充信息让我调整。'
  })
  return status
}

// 「生成专属备赛计划」：首轮
async function startPlan() {
  if (generating.value) return
  const cid = routedId.value
  generating.value = true
  try {
    const status = await requestPlan('')
    if (status === 'ask' && routedId.value === cid) {
      // 反问场景：把输入框滚动到视野内，提示用户作答
      await nextTick()
      planBody.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
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

// 发送补充信息
async function send() {
  const text = draft.value.trim()
  if (!text || generating.value) return
  const cid = routedId.value
  messages.value.push({ role: 'user', content: text })
  draft.value = ''
  generating.value = true
  try {
    await requestPlan(text)
  } catch (e) {
    if (routedId.value !== cid) return
    errorMsg.value = e?.message || '更新失败，请稍后重试'
  } finally {
    if (routedId.value === cid) generating.value = false
  }
}

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
    const res = await getCompetitionAwards(cid, { limit: 20 })
    works.value = res.items
  } catch (e) {
    works.value = []
    worksError.value = e?.message || '获奖作品加载失败，请稍后重试'
  } finally {
    worksLoading.value = false
  }
}

/* ---- 共性分析：竞赛级报告（一键分析全部获奖作品的共性） ---- */
const commonText = ref('')          // 共性分析正文（markdown）
const commonLoading = ref(false)    // 请求中
const commonError = ref('')
const commonSavedAt = ref('')       // 缓存写入时间（展示用）
const hasCommon = computed(() => !!commonText.value)
const commonHtml = computed(() => (commonText.value ? renderMarkdown(commonText.value) : ''))

/** 挂载时先回填缓存，命中则秒出，不请求接口 */
function restoreCommonAnalysis() {
  const cid = routedId.value
  if (!cid) return
  const cached = getCommonAnalysis(cid)
  if (cached?.text) {
    commonText.value = cached.text
    commonSavedAt.value = formatSavedAt(cached.savedAt)
  }
}

/**
 * 触发共性分析。
 * @param {boolean} force true = 忽略缓存强制重新分析
 */
async function runCommonAnalysis(force = false) {
  if (commonLoading.value) return

  const cid = routedId.value
  if (!cid) {
    commonError.value = '竞赛 ID 缺失，请返回列表重新进入'
    return
  }

  // 非强制且已有缓存内容，直接复用
  if (!force && commonText.value) return

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
      const saved = setCommonAnalysis(cid, analysis)
      commonSavedAt.value = formatSavedAt(saved?.savedAt || Date.now())
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
  commonSavedAt.value = ''
}

function openWork(work) {
  if (!work) return
  // 切换作品时清空上一篇的解析结果与错误，避免残留内容串台
  selectedWork.value = work
  workParsing.value = false
  parsedWork.value = null
  parsedText.value = ''
  parseError.value = ''
  workSavedAt.value = ''
  originalTip.value = ''

  // 命中缓存则直接回填，按钮自动变为「重新解析」
  const cid = routedId.value
  const cached = cid ? getWorkAnalysis(cid, work.id) : null
  if (cached?.text) {
    parsedText.value = cached.text
    parsedWork.value = work.id
    workSavedAt.value = formatSavedAt(cached.savedAt)
  }
}

function closeWork() {
  selectedWork.value = null
  workParsing.value = false
  parsedWork.value = null
  parsedText.value = ''
  parseError.value = ''
  workSavedAt.value = ''
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


// Modal 内「AI 解析」：调用后端 /api/ai/analyze
const parsedText = ref('')
const parseError = ref('')
const workSavedAt = ref('')
const originalTip = ref('')      // 原文入口的提示文案（暂无链接时展示）       // 该作品解析的缓存时间（展示用）
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
  workSavedAt.value = ''

  // 请求前快照（竞赛 + 作品），避免 await 期间用户切换导致结果串台
  const workId = selectedWork.value.id
  // 回调是否仍属当前上下文：竞赛未切走 且 弹窗仍是同一个作品
  const isStillCurrent = () => routedId.value === cid && selectedWork.value?.id === workId
  try {
    // analyzeWork 内部已做竞赛 ID 校验，指向单作品接口
    const res = await analyzeWork(workId, { competitionId: cid })
    const analysis = res?.analysis || ''
    if (!analysis) {
      // 连错误提示也要守卫，否则会把别处的弹窗写上错误
      if (isStillCurrent()) parseError.value = '未获取到解析内容，请稍后重试'
    } else {
      // 缓存永远写到快照时的竞赛（cid），不受用户切走影响
      const saved = setWorkAnalysis(cid, workId, analysis)
      // 但只有仍在同一竞赛 + 同一作品时才回填 UI
      if (isStillCurrent()) {
        parsedText.value = analysis
        parsedWork.value = workId
        workSavedAt.value = formatSavedAt(saved?.savedAt || Date.now())
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
const FAV_KEY = 'prep_favorites_v1'

function loadFavorites() {
  try {
    const raw = localStorage.getItem(FAV_KEY)
    return raw ? JSON.parse(raw) : []
  } catch (e) {
    return []
  }
}

const inspirations = ref([
  { id: 1, text: '\u628a\u961f\u4f0d\u5e94\u7528\u5230\u98df\u5802\u7a97\u53e3\u6392\u961f\uff0c\u7528\u534f\u540c\u8fc7\u6ee4\u6a21\u578b\u5199\u6210\u5feb\u901f\u53d1\u9910\u7b56\u7565\u3002', tag: '\u5efa\u6a21\u601d\u8def', favorite: false },
  { id: 2, text: '\u7528\u7070\u8272\u9884\u6d4b + \u5e72\u9884\u6a21\u578b\u5efa\u5c0f\u6837\u672c\u6570\u636e\uff0c\u5bf9\u6bd4 ARIMA \u6548\u679c\u3002', tag: '\u7b97\u6cd5\u9009\u578b', favorite: false },
  { id: 3, text: '\u8bba\u6587\u6458\u8981\u5148\u5199\u7ed3\u8bba\u518d\u5199\u65b9\u6cd5\uff0c\u8bc4\u59d4\u9605\u5377\u65f6\u95f4\u6709\u9650\uff0c\u524d\u4e09\u53e5\u5fc5\u987b\u4eae\u51fa\u7ed3\u679c\u3002', tag: '\u5199\u4f5c\u6280\u5de7', favorite: false }
])

// \u4ece localStorage \u6062\u590d\u6536\u85cf\u72b6\u6001
const favIds = ref(loadFavorites())
inspirations.value.forEach((it) => {
  if (favIds.value.includes(it.id)) it.favorite = true
})

// \u5f53\u524d\u662f\u5426\u5904\u4e8e\u300c\u6211\u7684\u6536\u85cf\u300d\u89c6\u56fe\uff08Tab3 \u5185\u6761\u4ef6\u6e32\u67d3\uff09
const showFavorites = ref(false)

const favoriteList = computed(() => inspirations.value.filter((it) => favIds.value.includes(it.id)))

function persist() {
  try {
    localStorage.setItem(FAV_KEY, JSON.stringify(favIds.value))
  } catch (e) { /* ignore */ }
}

// \u5237\u65b0\u7075\u611f\uff08mock\uff1a\u6253\u4e71\u987a\u5e8f\uff09
function refreshInspiration() {
  inspirations.value = [...inspirations.value].sort(() => Math.random() - 0.5)
}

function toggleFavorite(item) {
  item.favorite = !item.favorite
  if (item.favorite) {
    if (!favIds.value.includes(item.id)) favIds.value.push(item.id)
  } else {
    favIds.value = favIds.value.filter((id) => id !== item.id)
  }
  persist()
}

/* ---- \u5f39\u7a97 Esc \u5173\u95ed ---- */
function onKeydown(e) {
  if (e.key === 'Escape' && selectedWork.value) closeWork()
}
onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  // 首屏并行拉取：顶部竞赛信息 + 获奖作品列表
  loadCompetition()
  loadWorks()
  // 共性分析命中缓存则秒出，避免每次进页面都重复调用 AI
  restoreCommonAnalysis()
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

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
            <svg class="h-6 w-6 text-slate-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 0L20 14m-6-6h.01M6 20h12a2 002-2V6a2 00-2-2H6a2 00-2 2v12a2 002 2z" />
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
            <span class="font-medium text-slate-400">比赛时间</span>
            <span class="font-semibold text-slate-700 dark:text-slate-200">{{ competition.contestTime }}</span>
            <span class="text-slate-300">·</span>
            <span class="font-medium text-slate-400">ID</span>
            <span class="font-semibold text-slate-700 dark:text-slate-200">{{ routedId || '—' }}</span>
          </div>
        </div>
      </div>

      <!-- Tab 导航 -->
      <nav class="mt-4 flex flex-nowrap items-center gap-2 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
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
    <section v-if="activeTab === 'ai'" ref="planBody" class="rounded-2xl bg-white dark:bg-slate-800 p-6 ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm">

      <!-- 顶部操作条 -->
      <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 dark:border-slate-700 pb-4">
        <div>
          <h2 class="text-base font-bold text-slate-900 dark:text-slate-100">AI 备赛计划</h2>
          <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">
            生成后可继续对话补充信息，AI 会在原计划上增量调整
          </p>
        </div>
        <button
          type="button"
          :disabled="generating"
          class="inline-flex h-10 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold
                 text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600
                 disabled:cursor-not-allowed disabled:opacity-60"
          @click="startPlan"
        >
          <svg v-if="generating" class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 00-4 4H4z" />
          </svg>
          <svg v-else class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round"
              d="M12 3v2m0 14v2M5 12H3m18 0h-2M7.05 7.05L5.64 5.64m12.72 12.72l-1.41-1.41M7.05 16.95l-1.41 1.41m12.72-12.72l-1.41 1.41" />
          </svg>
          {{ generating ? '生成中…' : (hasPlan ? '重新生成' : '生成专属备赛计划') }}
        </button>
      </div>

      <!-- 错误提示 -->
      <div
        v-if="errorMsg"
        class="mt-4 flex items-start gap-2 rounded-xl bg-red-50 dark:bg-red-500/15 px-4 py-3 text-sm text-red-600 dark:text-red-400 ring-1 ring-red-100"
      >
        <svg class="mt-0.5 h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 001.71-3L13.71 3.86a2 00-3.42 0z" />
        </svg>
        <span>{{ errorMsg }}</span>
      </div>

      <!-- 计划正文（Markdown） -->
      <div v-if="hasPlan" class="mt-5 rounded-xl bg-slate-50/70 dark:bg-slate-800/60 p-5 ring-1 ring-slate-200/60 dark:ring-slate-700">
        <div class="markdown-body" v-html="planHtml"></div>
      </div>

      <!-- 未生成计划：空状态 -->
      <div v-else-if="!generating" class="flex flex-col items-center justify-center py-16 text-center">
        <div class="flex h-16 w-16 items-center justify-center rounded-2xl bg-blue-50 dark:bg-blue-500/15">
          <svg class="h-8 w-8 text-blue-600 dark:text-blue-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6M9 8h6M7 3h7l5 5v13H7z" />
          </svg>
        </div>
        <h3 class="mt-5 text-lg font-bold text-slate-900 dark:text-slate-100">还没有备赛计划</h3>
        <p class="mt-2 max-w-md text-sm leading-relaxed text-slate-500 dark:text-slate-400">
          在下方输入框告诉我你的目标、基础和时间安排，AI 会先了解你的情况；信息足够后立刻给出完整计划。
        </p>
      </div>

      <!-- 生成中骨架 -->
      <div v-else class="mt-5 space-y-3" aria-busy="true">
        <div class="h-4 w-1/3 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
        <div class="h-3 w-full animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
        <div class="h-3 w-11/12 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
        <div class="h-3 w-4/5 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
        <div class="h-4 w-1/4 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
        <div class="h-3 w-full animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
        <div class="h-3 w-3/4 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
      </div>

      <!-- 对话区 -->
      <div class="mt-6 space-y-3">
        <div
          v-for="(m, i) in messages"
          :key="i"
          class="flex gap-2"
          :class="m.role === 'user' ? 'justify-end' : 'justify-start'"
        >
          <div
            class="max-w-[80%] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-sm leading-relaxed"
            :class="m.role === 'user'
              ? 'bg-blue-600 text-white'
              : 'bg-slate-100 dark:bg-slate-700/60 text-slate-700 dark:text-slate-200'"
          >{{ m.content }}</div>
        </div>
      </div>

      <!-- 输入区 -->
      <div class="mt-4 flex items-center gap-3">
        <input
          v-model="draft"
          type="text"
          :disabled="generating"
          placeholder="补充你的时间安排或需求，例如：我每天只有 2 小时"
          class="h-11 flex-1 rounded-full bg-slate-50 dark:bg-slate-900 px-5 text-sm text-slate-700 dark:text-slate-200
                 outline-none ring-1 ring-slate-200 dark:ring-slate-700 placeholder:text-slate-400 dark:placeholder:text-slate-500
                 focus:ring-2 focus:ring-blue-500 disabled:opacity-60"
          @keydown.enter="send"
        />
        <button
          type="button"
          :disabled="generating || !draft.trim()"
          class="inline-flex h-11 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold
                 text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600
                 disabled:cursor-not-allowed disabled:opacity-60"
          @click="send"
        >
          发送
          <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 19V5m0 0l-7 7m7-7l7 7" />
          </svg>
        </button>
      </div>
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
              让 AI 横向对比历年获奖作品，总结选题偏好、建模套路与写作亮点，快速抓住评审口味。
            </p>
            <p v-if="commonSavedAt" class="mt-1 text-xs text-slate-400">上次分析：{{ commonSavedAt }}</p>
          </div>

          <button
            type="button"
            class="inline-flex h-11 shrink-0 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold
                   text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600
                   disabled:cursor-not-allowed disabled:opacity-60"
            :disabled="commonLoading || !routedId"
            @click="runCommonAnalysis(hasCommon)"
          >
            <svg
              v-if="commonLoading"
              class="h-4 w-4 animate-spin"
              fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M12 3a9 9 0 109" />
            </svg>
            {{ commonLoading ? '分析中…' : (hasCommon ? '重新分析共性' : 'AI 分析获奖作品共性') }}
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
          class="h-64 animate-pulse rounded-2xl bg-white dark:bg-slate-800 ring-1 ring-slate-200/70 dark:ring-slate-700"
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
          class="group flex cursor-pointer flex-col rounded-2xl bg-white dark:bg-slate-800 p-5 ring-1 ring-slate-200/70 dark:ring-slate-700
                 shadow-sm transition-all hover:-translate-y-0.5 hover:shadow-lg hover:shadow-slate-900/10"
          @click="openWork(work)"
        >
          <div class="mb-4 aspect-[4/3] w-full overflow-hidden rounded-xl bg-slate-100 dark:bg-slate-700/60">
            <div class="flex h-full w-full items-center justify-center">
              <svg class="h-8 w-8 text-slate-300" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6M9 8h6M7 3h7l5 5v13H7z" />
              </svg>
            </div>
          </div>

          <h3 class="text-base font-bold leading-snug text-slate-900 dark:text-slate-100">{{ work.title }}</h3>
          <p class="mt-2 text-xs font-medium text-slate-400">{{ work.team }} · {{ work.year }}</p>

          <div class="mt-3 flex items-center justify-between">
            <span class="rounded-full bg-amber-50 dark:bg-amber-500/15 px-2.5 py-1 text-xs font-bold text-amber-700 dark:text-amber-300">
              {{ work.award }}
            </span>
            <span class="text-xs font-bold text-blue-600 dark:text-blue-400 opacity-0 transition-opacity group-hover:opacity-100">
              查看详情 →
            </span>
          </div>
        </article>
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
          <div class="shrink-0 border-b border-slate-100 dark:border-slate-700 bg-gradient-to-br from-blue-50/80 via-white to-white px-7 pb-5 pt-6">
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
                    class="rounded-full bg-indigo-50 dark:bg-indigo-500/15 px-3 py-1 text-xs font-semibold text-indigo-700 ring-1 ring-indigo-200"
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
                    <path stroke-linecap="round" stroke-linejoin="round" d="M17 20h5v-1a4 4 0 00-3-3.87M9 20H4v-1a4 013-3.87m6-1.13a4 10-4-4 004 4zm6-4a3 3 11-3-3" />
                  </svg>
                  {{ selectedWork.team }}
                </p>
              </div>
              <button
                type="button"
                class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-slate-400
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
              <p v-else class="rounded-xl bg-slate-50 dark:bg-slate-900 px-4 py-3 text-sm text-slate-400">
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
              <p v-else class="rounded-xl bg-slate-50 dark:bg-slate-900 px-4 py-3 text-sm text-slate-400">
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
              <p v-else class="rounded-xl bg-slate-50 dark:bg-slate-900 px-4 py-3 text-sm text-slate-400">
                暂无亮点描述。
              </p>
            </section>

            <!-- 四、AI 深度解析 -->
            <section class="mt-7">
              <h4 class="mb-2.5 flex items-center gap-2 text-sm font-black text-slate-900 dark:text-slate-100">
                <span class="flex h-5 w-5 items-center justify-center rounded-md bg-slate-900 text-[10px] font-black text-white">AI</span>
                智能解析
                <span v-if="workSavedAt" class="text-xs font-medium text-slate-400">缓存于 {{ workSavedAt }}</span>
              </h4>

              <!-- 解析中 -->
              <div v-if="workParsing" class="flex items-center gap-3 rounded-2xl bg-blue-50/70 dark:bg-blue-500/15 p-4">
                <svg class="h-5 w-5 animate-spin text-blue-600 dark:text-blue-400" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 5.373 12h4z" />
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
                    <p class="mt-0.5 text-xs text-slate-400">
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
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 5.373 12h4z" />
              </svg>
              {{ workParsing ? '解析中…' : (parsedWork === selectedWork.id ? '重新解析' : 'AI 解析') }}
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
          <p class="text-sm font-bold text-blue-900">灵感库会持续为你推送建模思路、算法选型与写作技巧</p>
          <p class="mt-1 text-xs font-medium text-blue-700/70">
            已收藏 {{ favoriteList.length }} 条灵感，收藏内容保存在本地，方便随时回看。
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
            type="button"
            class="inline-flex items-center gap-2 rounded-full bg-white dark:bg-slate-800 px-4 py-2 text-xs font-bold
                   text-blue-700 dark:text-blue-300 ring-1 ring-blue-200 dark:ring-blue-500/30 transition-colors hover:bg-blue-100 dark:hover:bg-blue-500/25"
            @click="refreshInspiration"
          >
            <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h5M20 20v-5h-5M20 9a8 8 0 00-14.9-2M4 15a8 0014.9 2" />
            </svg>
            刷新
          </button>
        </div>
      </div>

      <!-- 收藏视图：空状态 -->
      <div
        v-if="showFavorites && favoriteList.length === 0"
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

      <!-- 灵感卡片列表（全部 / 收藏） -->
      <div v-else class="grid grid-cols-1 gap-5 sm:grid-cols-2">
        <article
          v-for="item in (showFavorites ? favoriteList : inspirations)"
          :key="item.id"
          class="flex flex-col rounded-2xl bg-white dark:bg-slate-800 p-5 ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm transition-all
                 hover:shadow-lg hover:shadow-slate-900/10"
        >
          <span class="mb-3 w-fit rounded-full bg-slate-100 dark:bg-slate-700/60 px-2.5 py-1 text-xs font-bold text-slate-600 dark:text-slate-300">
            {{ item.tag }}
          </span>
          <p class="text-sm leading-relaxed text-slate-700 dark:text-slate-200">{{ item.text }}</p>

          <div class="mt-4 flex items-center justify-end">
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
    </main>
  </div>
</template>

<style scoped>
/* Markdown 计划正文排版 */
.markdown-body { font-size: 14px; line-height: 1.75; color: #334155; }
.markdown-body > *:first-child { margin-top: 0; }
.markdown-body > *:last-child { margin-bottom: 0; }
.markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4 {
  font-weight: 700; color: #0f172a; line-height: 1.35; margin: 1.25em 0 0.6em;
}
.markdown-body h1 { font-size: 1.35em; }
.markdown-body h2 { font-size: 1.18em; }
.markdown-body h3 { font-size: 1.05em; }
.markdown-body h4 { font-size: 1em; }
.markdown-body p { margin: 0.6em 0; }
.markdown-body ul, .markdown-body ol { margin: 0.6em 0; padding-left: 1.4em; }
.markdown-body ul { list-style: disc; }
.markdown-body ol { list-style: decimal; }
.markdown-body li { margin: 0.25em 0; }
.markdown-body strong { font-weight: 700; color: #0f172a; }
.markdown-body a { color: #2563eb; text-decoration: underline; }
.markdown-body code {
  background: #e2e8f0; border-radius: 4px; padding: 0.1em 0.35em;
  font-size: 0.9em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.markdown-body pre {
  background: #0f172a; color: #e2e8f0; border-radius: 10px;
  padding: 12px 14px; overflow-x: auto; margin: 0.8em 0;
}
.markdown-body pre code { background: transparent; padding: 0; color: inherit; }
.markdown-body blockquote {
  border-left: 3px solid #93c5fd; color: #475569;
  margin: 0.8em 0; padding: 0.2em 0 0.2em 1em;
}
.markdown-body table { width: 100%; border-collapse: collapse; margin: 0.8em 0; font-size: 0.95em; }
.markdown-body th, .markdown-body td { border: 1px solid #e2e8f0; padding: 6px 10px; text-align: left; }
.markdown-body th { background: #f1f5f9; font-weight: 700; }
.markdown-body hr { border: 0; border-top: 1px solid #e2e8f0; margin: 1.2em 0; }

/* ===== 暗色模式覆盖：AI 解析区在深色下必须保持足够对比度 ===== */
.dark .markdown-body { color: #cbd5e1; }
.dark .markdown-body h1,
.dark .markdown-body h2,
.dark .markdown-body h3,
.dark .markdown-body h4,
.dark .markdown-body strong { color: #f1f5f9; }
.dark .markdown-body a { color: #60a5fa; }
.dark .markdown-body code { background: #334155; color: #e2e8f0; }
.dark .markdown-body pre { background: #020617; color: #e2e8f0; border: 1px solid #334155; }
.dark .markdown-body pre code { background: transparent; color: inherit; }
.dark .markdown-body blockquote { border-left-color: #3b82f6; color: #94a3b8; }
.dark .markdown-body th,
.dark .markdown-body td { border-color: #334155; }
.dark .markdown-body th { background: #1e293b; color: #f1f5f9; }
.dark .markdown-body hr { border-top-color: #334155; }
</style>

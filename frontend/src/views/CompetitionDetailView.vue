<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Header from '@/components/Header.vue'
import { getCompetitionDetail, requestGuide, getGuideStatus } from '@/services/api'
import { isLoggedIn } from '@/services/auth'
import { openAuthModal } from '@/composables/useAuthModal'
import { COVER_USE_REMOTE } from '@/utils/coverFallback'
import CoverArt from '@/components/CoverArt.vue'

const route = useRoute()

const detail = ref(null)
const loading = ref(true)
const errorMsg = ref('')

// 详情页大图：与列表统一的程序化封面（CoverArt），除非开启 COVER_USE_REMOTE 且确有图片
const heroFailed = ref(false)
const heroArt = computed(() => !COVER_USE_REMOTE || !detail.value?.image || heroFailed.value)

function onHeroError() {
  heroFailed.value = true
}

watch(
  () => detail.value?.image,
  () => {
    heroFailed.value = false
  }
)

// ---- 后端字段格式化 ----
// 双轨制：日期轨（reg_*_date / contest_*_date）优先，为空时降级到月份轨；
// 统一实现在 utils/timeFormat，避免各页面文案漂移。
import { toMonth, parseDate, formatSignUp, formatContest, formatStageRange, formatStageSpan } from '../utils/timeFormat'

// 封面图：只接受 http(s) 绝对地址，其余回落大图占位
function resolveImage(item) {
  const raw = item.image ?? item.cover ?? item.image_url ?? ''
  const url = typeof raw === 'string' ? raw.trim() : ''
  return /^https?:\/\//i.test(url) ? url : ''
}

// 从后端详情接口获取数据
async function loadDetail(id) {
  loading.value = true
  errorMsg.value = ''
  detail.value = null

  try {
    const data = await getCompetitionDetail(id)
    // 先展开原始数据，再用格式化字段兜底，避免 ...data 覆盖已归一化的值
    detail.value = {
      ...data,
      title: data.name || data.title || '未命名竞赛',
      signUpTime: data.sign_up_time || data.signUpTime || formatSignUp(data),
      contestTime: data.contest_time || data.contestTime || formatContest(data),
      image: resolveImage(data)
    }
    syncGuideStateFromDetail(detail.value)
  } catch (err) {
    errorMsg.value = err?.message || '加载竞赛详情失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => loadDetail(route.params.id))

// 支持同组件内切换不同 id（例如返回列表再点另一张卡片）
watch(
  () => route.params.id,
  (id) => {
    if (id !== undefined && route.name === 'competition-detail') {
      stopGuidePolling()
      resetGuideGeneration()
      loadDetail(id)
    }
  }
)

/* ------------------------------------------------------------------ */
/* 攻略按需生成（登录 + 异步 + 轮询）                                  */
/* ------------------------------------------------------------------ */
// idle / queued / running / ready / failed，与后端 guide_status 同源
const guideState = ref('idle')
const guideRequesting = ref(false)
const guideError = ref('')
const guidePollCount = ref(0)

let guideTimer = null

const GUIDE_POLL_FAST_LIMIT = 20 // 前 20 次 3s（约 1 分钟）
const GUIDE_POLL_MAX = 38 // 总次数上限（约 4 分钟）
const GUIDE_POLL_FAST_MS = 3000
const GUIDE_POLL_SLOW_MS = 10000

const guideBusy = computed(
  () => guideState.value === 'queued' || guideState.value === 'running'
)
const canGenerateGuide = computed(
  () => !detail.value?.full_process_guide && !guideBusy.value
)

function syncGuideStateFromDetail(data) {
  const state = data?.guide_status
  // 详情接口一定带 guide_status；缺失时用攻略是否为空兜底
  guideState.value = ['idle', 'queued', 'running', 'ready', 'failed'].includes(state)
    ? state
    : data?.full_process_guide
      ? 'ready'
      : 'idle'
  if (guideState.value === 'failed' && !guideError.value) {
    guideError.value = '上次生成未成功，请稍后重新尝试'
  }
}

function resetGuideGeneration() {
  guideState.value = 'idle'
  guideError.value = ''
  guideRequesting.value = false
  guidePollCount.value = 0
}

function stopGuidePolling() {
  if (guideTimer) {
    clearTimeout(guideTimer)
    guideTimer = null
  }
}

function scheduleGuidePoll() {
  stopGuidePolling()
  const delay =
    guidePollCount.value < GUIDE_POLL_FAST_LIMIT ? GUIDE_POLL_FAST_MS : GUIDE_POLL_SLOW_MS
  guideTimer = setTimeout(pollGuideStatus, delay)
}

async function pollGuideStatus() {
  const routeId = route.params.id
  guidePollCount.value += 1
  try {
    const state = await getGuideStatus(routeId)
    // 轮询期间切走了详情页：丢弃本次结果
    if (route.params.id !== routeId) return
    guideState.value = state.status

    if (state.status === 'ready') {
      stopGuidePolling()
      await loadDetail(routeId)
      return
    }
    if (state.status === 'failed') {
      stopGuidePolling()
      guideError.value = 'AI 生成失败，请稍后重试'
      return
    }
    if (guidePollCount.value >= GUIDE_POLL_MAX) {
      stopGuidePolling()
      guideState.value = 'idle'
      guideError.value = '生成时间较长，请稍后刷新页面查看'
      return
    }
    scheduleGuidePoll()
  } catch (err) {
    stopGuidePolling()
    guideState.value = 'idle'
    if (err?.status === 401) {
      guideError.value = '登录状态已失效，请重新登录后再生成'
    } else {
      guideError.value = err?.message || '查询生成状态失败'
    }
  }
}

async function onGenerateGuide() {
  if (guideRequesting.value || guideBusy.value) return

  // 后端强制登录：未登录直接唤起登录弹窗，避免打一次必然 401 的请求
  if (!isLoggedIn()) {
    openAuthModal('login')
    return
  }

  guideRequesting.value = true
  guideError.value = ''
  guidePollCount.value = 0

  try {
    const result = await requestGuide(route.params.id)
    if (result.status === 'ready') {
      await loadDetail(route.params.id)
      return
    }
    guideState.value = result.status === 'running' ? 'running' : 'queued'
    scheduleGuidePoll()
  } catch (err) {
    if (err?.status === 401) {
      openAuthModal('login')
      guideError.value = '请先登录后再生成攻略'
    } else if (err?.status === 429) {
      guideError.value =
        err?.data?.reason === 'queue_full'
          ? '生成队列已满，请稍后再试'
          : '今日 AI 生成额度已用完，请明天再试'
    } else if (err?.status === 409) {
      guideError.value = '该赛事攻略刚刚生成失败，请稍后再试'
    } else {
      guideError.value = err?.message || '生成失败，请稍后重试'
    }
    guideState.value = 'idle'
  } finally {
    guideRequesting.value = false
  }
}

onUnmounted(stopGuidePolling)

// 攻略 Markdown 的第一行是攻略标题，详情页已有大标题，渲染时去掉避免重复 H1
function stripGuideTitle(markdown) {
  return String(markdown || '').replace(/^\uFEFF?\s*#\s+[^\n]*(?:\r?\n)+/, '')
}

// 参赛全流程攻略：Markdown 渲染 + XSS 净化
const renderedGuide = computed(() => {
  if (!detail.value?.full_process_guide) return ''
  return DOMPurify.sanitize(marked.parse(stripGuideTitle(detail.value.full_process_guide)))
})

/**
 * 外链安全校验：只允许 http / https 的绝对地址。
 *
 * 后端字段（爬虫入库 / 运营录入）不可信，`javascript:alert(...)` 这类值
 * 直接绑到 :href 会在点击时执行。解析失败或协议不符一律返回空串，
 * 模板据此隐藏“查看官网”入口。
 */
function safeExternalUrl(raw) {
  if (typeof raw !== 'string') return ''
  const value = raw.trim()
  if (!/^https?:\/\//i.test(value)) return ''
  try {
    const parsed = new URL(value)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : ''
  } catch (e) {
    return ''
  }
}

const officialUrl = computed(() => safeExternalUrl(detail.value?.official_url))

// 目录页 / 通知页不是「赛事官网」：教育部门名录里的每条候选共用同一个目录链接
// （如 https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm），把它渲染成「查看官网」
// 按钮会误导用户点进学校教务处列表页，所以这类地址只保留纯文本来源标注。
const ROSTER_HOSTS = ['sdaeu.edu.cn']
const NOTICE_PATH_RE = /\/info\/\d{1,6}\/\d{1,6}\.(?:html?|shtml?)$/i

function isRealOfficialSite(raw) {
  const href = safeExternalUrl(raw)
  if (!href) return false
  try {
    const url = new URL(href)
    const roster = ROSTER_HOSTS.some(
      (host) => url.hostname === host || url.hostname.endsWith('.' + host)
    )
    if (roster) return false
    return !NOTICE_PATH_RE.test(url.pathname)
  } catch (e) {
    return false
  }
}

const officialIsReal = computed(() => isRealOfficialSite(detail.value?.official_url))

// 名录类来源在库里没有独立字段（discover_source 未下发到详情），
// 用「链接是不是目录页」反推来源文案，避免多改后端模型。
const officialFallbackText = computed(() =>
  officialUrl.value && !officialIsReal.value ? '来源：教育部门名录' : '暂未收录官网'
)

// 官网域名（用于在没有完整 URL 时也能显示真实来源）
const officialHost = computed(() => {
  if (!officialUrl.value) return ''
  try {
    return new URL(officialUrl.value).host
  } catch (e) {
    return ''
  }
})

// 简介：intro 与 summary 都是真实采集字段，去重后逐段渲染
const introParagraphs = computed(() => {
  const intro = String(detail.value?.intro || '').trim()
  const summary = String(detail.value?.summary || '').trim()
  const list = []
  if (intro) list.push(intro)
  if (summary && summary !== intro) list.push(summary)
  return list
})

const techStack = computed(() => {
  const raw = detail.value?.tech_stack
  if (!Array.isArray(raw)) return []
  return raw.map((item) => String(item ?? '').trim()).filter(Boolean)
})

// 未来后端若补充这三个字段，卡片会自动渲染真实内容；当前为空时保留空壳
const participants = computed(() =>
  String(detail.value?.participants || detail.value?.participant || '').trim()
)
const contentScope = computed(() =>
  String(detail.value?.content_scope || detail.value?.scope || '').trim()
)
const examArrangement = computed(() =>
  String(detail.value?.exam_arrangement || detail.value?.exam_schedule || '').trim()
)

// 时间展示文案 + 可用性判断。
// 双轨制：日期轨（原文写到「日」）优先，月份轨（老数据 / 只写到月）兜底，
// 两轨都空才显示「暂未收录」——**判断口径必须和 utils/timeFormat 的降级顺序一致**，
// 否则会出现「列表卡片有精确日期、详情页却写暂未收录」。
const regTimeText = computed(() => {
  const legacy = String(detail.value?.sign_up_time || '').trim()
  return legacy || formatSignUp(detail.value || {})
})
const contestTimeText = computed(() => {
  const legacy = String(detail.value?.contest_time || '').trim()
  if (legacy) return legacy
  const data = detail.value || {}
  const hasDateTrack = Boolean(parseDate(data.contest_start_date) || parseDate(data.contest_end_date))
  // 只有月份轨时，阶段表里的「YYYY年M月」能补回年份（2025年11月 – 2026年3月）
  if (!hasDateTrack) {
    const stageSpan = formatStageSpan(data.stage_schedule)
    if (stageSpan) return stageSpan
  }
  return formatContest(data)
})

const hasRegTime = computed(() =>
  Boolean(
    parseDate(detail.value?.reg_start_date) ||
    parseDate(detail.value?.reg_end_date) ||
    toMonth(detail.value?.reg_start_month) ||
    toMonth(detail.value?.reg_end_month) ||
    String(detail.value?.sign_up_time || '').trim()
  )
)
const hasContestTime = computed(() =>
  Boolean(
    parseDate(detail.value?.contest_start_date) ||
    parseDate(detail.value?.contest_end_date) ||
    toMonth(detail.value?.contest_start_month) ||
    toMonth(detail.value?.contest_end_month) ||
    String(detail.value?.contest_time || '').trim()
  )
)
const hasAnyTime = computed(() => hasRegTime.value || hasContestTime.value)

// 阶段时间表：后端已过「原文佐证 + 年份」护栏，这里只做展示格式化
const STAGE_TONE = {
  报名: 'border-blue-400 text-blue-700 dark:text-blue-300',
  校赛: 'border-sky-400 text-sky-700 dark:text-sky-300',
  省赛: 'border-teal-400 text-teal-700 dark:text-teal-300',
  初赛: 'border-indigo-400 text-indigo-700 dark:text-indigo-300',
  复赛: 'border-violet-400 text-violet-700 dark:text-violet-300',
  决赛: 'border-rose-400 text-rose-700 dark:text-rose-300'
}
const stageList = computed(() => {
  const raw = detail.value?.stage_schedule
  if (!Array.isArray(raw)) return []
  return raw
    .filter((row) => row && row.stage)
    .map((row) => ({
      stage: String(row.stage),
      range: formatStageRange(row.start_date, row.end_date),
      note: String(row.note || '').trim(),
      tone: STAGE_TONE[String(row.stage)] || 'border-slate-400 text-slate-700 dark:text-slate-300'
    }))
    .filter((row) => row.range || row.note)
})

const guideReady = computed(() => Boolean(detail.value?.full_process_guide))
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto w-full max-w-6xl px-4 pb-24 pt-28 sm:px-6">
      <!-- 加载中 -->
      <div v-if="loading" class="space-y-6">
        <div class="h-64 w-full animate-pulse rounded-2xl bg-slate-200 dark:bg-slate-700"></div>
        <div class="h-10 w-2/3 animate-pulse rounded-lg bg-slate-200 dark:bg-slate-700"></div>
        <div class="h-5 w-1/2 animate-pulse rounded bg-slate-200 dark:bg-slate-700"></div>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="errorMsg"
        class="rounded-2xl border border-dashed border-rose-200 dark:border-rose-500/30 bg-rose-50/40 dark:bg-rose-500/10 px-6 py-16 text-center"
      >
        <p class="font-bold text-rose-500">{{ errorMsg }}</p>
        <button
          class="mt-6 rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white
                 shadow-sm shadow-blue-600/25 hover:bg-blue-700 dark:hover:bg-blue-600 transition-colors"
          @click="loadDetail(route.params.id)"
        >
          重新加载
        </button>
      </div>

      <!-- 正常内容 -->
      <template v-else>
        <!-- 1. 全宽竞赛大图：宽度撑满、高度按原图比例自适应，最高 400px。
             只 shrink 不 crop：object-contain 保证海报上的标题/文字完整可见，
             比例不匹配时的留白由容器背景色兜住，不再切掉核心文字。 -->
        <div class="mt-8 w-full">
          <div class="w-full overflow-hidden rounded-2xl bg-[#0a1730] ring-1 ring-slate-200/60 dark:ring-slate-700/60">
            <div class="h-[220px] w-full sm:h-[300px]">
              <CoverArt
                v-if="heroArt"
                :title="detail?.name || ''"
                :short-name="detail?.short_name || ''"
                :category="detail?.category || ''"
                :seed="detail?.id || route.params.id"
                variant="hero"
                class="h-full w-full"
              />
              <img
                v-else
                :src="detail.image"
                alt=""
                class="block h-full w-full object-cover object-center"
                @error="onHeroError"
              />
            </div>
          </div>
        </div>

        <!-- 2. 标题与真实标签 -->
        <section class="mt-9">
          <div class="flex flex-wrap items-center gap-2">
            <span
              v-if="detail.category"
              class="rounded-full bg-white px-3 py-1 text-xs font-bold text-slate-600
                     ring-1 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700"
            >
              {{ detail.category }}
            </span>
            <span
              v-if="guideReady"
              class="rounded-full bg-emerald-50 px-3 py-1 text-xs font-bold text-emerald-700
                     ring-1 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/20"
            >
              AI 攻略已就绪
            </span>
          </div>

          <h1
            class="mt-4 text-3xl font-black leading-tight tracking-tight text-slate-900
                   dark:text-slate-100 sm:text-4xl"
          >
            {{ detail?.title }}
          </h1>

          <div class="mt-5 flex flex-wrap items-center gap-x-4 gap-y-2">
            <a
              v-if="officialIsReal"
              :href="officialUrl"
              target="_blank"
              rel="noopener noreferrer"
              class="inline-flex items-center gap-1.5 rounded-full bg-white px-4 py-2 text-xs font-bold
                     text-slate-700 ring-1 ring-slate-200 transition-colors
                     hover:bg-slate-50 hover:text-blue-700 hover:ring-blue-200
                     dark:bg-slate-800 dark:text-slate-200 dark:ring-slate-700
                     dark:hover:bg-slate-700 dark:hover:text-blue-300"
            >
              <span class="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden="true"></span>
              官网 {{ officialHost }}
              <span aria-hidden="true">↗</span>
            </a>
            <span
              v-else
              class="inline-flex items-center gap-1.5 rounded-full bg-slate-100 px-4 py-2 text-xs
                     font-semibold text-slate-500 dark:bg-slate-800 dark:text-slate-400"
            >
              {{ officialFallbackText }}
            </span>
            <span class="text-xs text-slate-500 dark:text-slate-400">
              信息来自平台采集，最终以官网当年通知为准
            </span>
          </div>
        </section>

        <!-- 3. 关键信息速览（真实字段；缺失时明确写“暂未收录”） -->
        <section class="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div
            class="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-sm
                   dark:border-slate-700 dark:bg-slate-800"
          >
            <span class="block h-1.5 w-8 rounded-full bg-blue-500"></span>
            <p class="mt-3 text-xs font-bold tracking-wide text-slate-500 dark:text-slate-400">报名时间</p>
            <p
              class="mt-1.5 text-lg font-black leading-tight"
              :class="hasRegTime ? 'text-slate-800 dark:text-slate-100' : 'text-slate-500 dark:text-slate-400'"
            >
              {{ hasRegTime ? regTimeText : '暂未收录' }}
            </p>
          </div>

          <div
            class="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-sm
                   dark:border-slate-700 dark:bg-slate-800"
          >
            <span class="block h-1.5 w-8 rounded-full bg-sky-500"></span>
            <p class="mt-3 text-xs font-bold tracking-wide text-slate-500 dark:text-slate-400">比赛时间</p>
            <p
              class="mt-1.5 text-lg font-black leading-tight"
              :class="hasContestTime ? 'text-slate-800 dark:text-slate-100' : 'text-slate-500 dark:text-slate-400'"
            >
              {{ hasContestTime ? contestTimeText : '暂未收录' }}
            </p>
          </div>

          <div
            class="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-sm
                   dark:border-slate-700 dark:bg-slate-800"
          >
            <span class="block h-1.5 w-8 rounded-full bg-amber-500"></span>
            <p class="mt-3 text-xs font-bold tracking-wide text-slate-500 dark:text-slate-400">赛事类别</p>
            <p
              class="mt-1.5 text-lg font-black leading-tight"
              :class="detail.category ? 'text-slate-800 dark:text-slate-100' : 'text-slate-500 dark:text-slate-400'"
            >
              {{ detail.category || '暂未收录' }}
            </p>
          </div>
        </section>

        <!-- 4. 分块卡片：时间安排 / 赛事简介 / 参赛对象 / 内容范围 / 考试安排 -->
        <section class="mt-8">
          <div class="mb-4 flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 class="text-xl font-bold text-slate-900 dark:text-slate-100">赛事详情</h2>
              <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">
                分模块展示平台已收录的信息；标注「暂未收录」的模块尚未获得官方结构化数据。
              </p>
            </div>
            <a
              v-if="officialIsReal"
              :href="officialUrl"
              target="_blank"
              rel="noopener noreferrer"
              class="inline-flex items-center gap-1 text-xs font-bold text-blue-600 hover:text-blue-700
                     hover:underline dark:text-blue-400 dark:hover:text-blue-300"
            >
              前往官网核对 <span aria-hidden="true">↗</span>
            </a>
          </div>

          <div class="grid gap-5 lg:grid-cols-12">
            <!-- 时间安排 -->
            <article
              class="relative overflow-hidden rounded-3xl border border-blue-200/80 bg-gradient-to-br
                     from-blue-50/90 via-white to-sky-50/70 p-6 shadow-sm
                     dark:border-blue-400/20 dark:from-blue-500/[0.10] dark:via-slate-800 dark:to-sky-500/[0.06]
                     lg:col-span-7"
            >
              <div class="flex items-center gap-2">
                <span class="h-4 w-[3px] rounded-full bg-blue-500" aria-hidden="true"></span>
                <h3 class="text-sm font-bold tracking-wide text-blue-700 dark:text-blue-300">时间安排</h3>
                <span class="ml-auto text-[10px] font-bold uppercase tracking-[0.18em] text-blue-400/70">
                  Schedule
                </span>
              </div>

              <div v-if="hasAnyTime" class="mt-5 space-y-4">
                <div v-if="hasRegTime" class="border-l-2 border-blue-400/70 pl-4">
                  <p class="text-xs font-bold tracking-wide text-blue-500 dark:text-blue-400">报名阶段</p>
                  <p class="mt-1 text-xl font-black text-slate-800 dark:text-slate-100">
                    {{ regTimeText }}
                  </p>
                </div>
                <div v-if="!hasRegTime" class="border-l-2 border-dashed border-slate-300 pl-4 dark:border-slate-600">
                  <p class="text-xs font-bold tracking-wide text-slate-500 dark:text-slate-400">报名阶段</p>
                  <p class="mt-1 text-sm font-bold text-slate-500 dark:text-slate-400">暂未收录</p>
                </div>
                <div v-if="hasContestTime" class="border-l-2 border-sky-400/70 pl-4">
                  <p class="text-xs font-bold tracking-wide text-sky-500 dark:text-sky-400">比赛阶段</p>
                  <p class="mt-1 text-xl font-black text-slate-800 dark:text-slate-100">
                    {{ contestTimeText }}
                  </p>
                </div>
                <div v-if="!hasContestTime" class="border-l-2 border-dashed border-slate-300 pl-4 dark:border-slate-600">
                  <p class="text-xs font-bold tracking-wide text-slate-500 dark:text-slate-400">比赛阶段</p>
                  <p class="mt-1 text-sm font-bold text-slate-500 dark:text-slate-400">暂未收录</p>
                </div>
                <!-- 阶段时间表：报名 -> 初赛 -> 复赛 -> 决赛 的时间轴 -->
                <div
                  v-if="stageList.length"
                  class="mt-5 border-t border-blue-200/70 pt-4 dark:border-blue-400/20"
                >
                  <p class="text-xs font-bold tracking-wide text-blue-500 dark:text-blue-400">
                    阶段时间表
                  </p>
                  <ol class="mt-3 space-y-4 border-l border-blue-300/60 pl-4 dark:border-blue-400/25">
                    <li
                      v-for="(row, index) in stageList"
                      :key="row.stage + '-' + index"
                      class="relative"
                    >
                      <span
                        class="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 bg-white dark:bg-slate-900"
                        :class="row.tone.split(' ')[0]"
                        aria-hidden="true"
                      ></span>
                      <div class="flex flex-wrap items-baseline gap-x-2">
                        <span class="text-sm font-black" :class="row.tone.split(' ').slice(1).join(' ')">
                          {{ row.stage }}
                        </span>
                        <span
                          v-if="row.range"
                          class="text-xs font-bold text-slate-600 dark:text-slate-300"
                        >
                          {{ row.range }}
                        </span>
                      </div>
                      <p v-if="row.note" class="mt-0.5 text-xs leading-5 text-slate-500 dark:text-slate-400">
                        {{ row.note }}
                      </p>
                    </li>
                  </ol>
                </div>

                <p class="text-xs leading-5 text-slate-500 dark:text-slate-400">
                  时间来自平台采集：原文写到「日」就显示精确日期，只写到月份则显示月份；
                  具体起止日期与校内选拔安排请以官网当年通知为准。
                </p>
              </div>

              <div
                v-else
                class="mt-5 rounded-2xl border border-dashed border-blue-200/80 bg-white/60 p-5
                       dark:border-blue-400/20 dark:bg-slate-900/30"
              >
                <p class="text-sm font-bold text-slate-600 dark:text-slate-300">官方暂未公布时间窗口</p>
                <p class="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
                  平台尚未采集到该赛事的报名或比赛月份。
                </p>
                <a
                  v-if="officialIsReal"
                  :href="officialUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="mt-3 inline-flex items-center gap-1 text-xs font-bold text-blue-600 hover:underline
                         dark:text-blue-400"
                >
                  去官网查看通知 <span aria-hidden="true">↗</span>
                </a>
              </div>
            </article>

            <!-- 赛事简介 -->
            <article
              class="rounded-3xl border border-slate-200/70 bg-white p-6 shadow-sm
                     dark:border-slate-700 dark:bg-slate-800 lg:col-span-5"
            >
              <div class="flex items-center gap-2">
                <span class="h-4 w-[3px] rounded-full bg-slate-400" aria-hidden="true"></span>
                <h3 class="text-sm font-bold tracking-wide text-slate-700 dark:text-slate-200">赛事简介</h3>
                <span class="ml-auto text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">
                  Overview
                </span>
              </div>

              <div v-if="introParagraphs.length" class="mt-4 space-y-3">
                <p
                  v-for="(text, i) in introParagraphs"
                  :key="i"
                  class="text-sm leading-7 text-slate-600 dark:text-slate-300"
                >
                  {{ text }}
                </p>
              </div>

              <div v-else class="mt-4 rounded-2xl border border-dashed border-slate-200 p-4 dark:border-slate-700">
                <p class="text-sm font-bold text-slate-500 dark:text-slate-400">暂未收录赛事简介</p>
                <p class="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
                  平台尚未采集到该赛事的简介或摘要。
                </p>
              </div>
            </article>

            <!-- 参赛对象 -->
            <article
              class="rounded-3xl border border-teal-200/80 bg-gradient-to-br from-teal-50/90 via-white
                     to-emerald-50/60 p-5 shadow-sm dark:border-teal-400/20 dark:from-teal-500/[0.09]
                     dark:via-slate-800 dark:to-emerald-500/[0.05] lg:col-span-4"
            >
              <div class="flex items-center gap-2">
                <span class="h-4 w-[3px] rounded-full bg-teal-500" aria-hidden="true"></span>
                <h3 class="text-sm font-bold tracking-wide text-teal-700 dark:text-teal-300">参赛对象</h3>
                <span class="ml-auto text-[10px] font-bold uppercase tracking-[0.18em] text-teal-400/70">
                  Eligibility
                </span>
              </div>

              <p
                v-if="participants"
                class="mt-3 text-sm leading-7 text-slate-600 dark:text-slate-300"
              >
                {{ participants }}
              </p>

              <div
                v-else
                class="mt-4 rounded-2xl border border-dashed border-teal-200/80 bg-white/60 p-4
                       dark:border-teal-400/20 dark:bg-slate-900/30"
              >
                <p class="text-sm font-bold text-slate-600 dark:text-slate-300">暂未收录参赛对象</p>
                <p class="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
                  平台尚未采集到该赛事的参赛资格与组队要求。
                </p>
                <a
                  v-if="officialIsReal"
                  :href="officialUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="mt-3 inline-flex items-center gap-1 text-xs font-bold text-teal-600 hover:underline
                         dark:text-teal-400"
                >
                  去官网查看 <span aria-hidden="true">↗</span>
                </a>
              </div>
            </article>

            <!-- 内容范围 -->
            <article
              class="rounded-3xl border border-amber-200/80 bg-gradient-to-br from-amber-50/90 via-white
                     to-orange-50/60 p-5 shadow-sm dark:border-amber-400/20 dark:from-amber-500/[0.09]
                     dark:via-slate-800 dark:to-orange-500/[0.05] lg:col-span-4"
            >
              <div class="flex items-center gap-2">
                <span class="h-4 w-[3px] rounded-full bg-amber-500" aria-hidden="true"></span>
                <h3 class="text-sm font-bold tracking-wide text-amber-700 dark:text-amber-300">内容范围</h3>
                <span class="ml-auto text-[10px] font-bold uppercase tracking-[0.18em] text-amber-400/70">
                  Scope
                </span>
              </div>

              <p
                v-if="contentScope"
                class="mt-3 text-sm leading-7 text-slate-600 dark:text-slate-300"
              >
                {{ contentScope }}
              </p>

              <div
                v-else
                class="mt-4 rounded-2xl border border-dashed border-amber-200/80 bg-white/60 p-4
                       dark:border-amber-400/20 dark:bg-slate-900/30"
              >
                <p class="text-sm font-bold text-slate-600 dark:text-slate-300">暂未收录内容范围</p>
                <p class="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
                  平台尚未采集到该赛事的赛项与作品范围说明。
                </p>
                <a
                  v-if="officialIsReal"
                  :href="officialUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="mt-3 inline-flex items-center gap-1 text-xs font-bold text-amber-600 hover:underline
                         dark:text-amber-400"
                >
                  去官网查看 <span aria-hidden="true">↗</span>
                </a>
              </div>
            </article>

            <!-- 考试安排 -->
            <article
              class="rounded-3xl border border-violet-200/80 bg-gradient-to-br from-violet-50/90 via-white
                     to-fuchsia-50/60 p-5 shadow-sm dark:border-violet-400/20 dark:from-violet-500/[0.09]
                     dark:via-slate-800 dark:to-fuchsia-500/[0.05] lg:col-span-4"
            >
              <div class="flex items-center gap-2">
                <span class="h-4 w-[3px] rounded-full bg-violet-500" aria-hidden="true"></span>
                <h3 class="text-sm font-bold tracking-wide text-violet-700 dark:text-violet-300">考试安排</h3>
                <span class="ml-auto text-[10px] font-bold uppercase tracking-[0.18em] text-violet-400/70">
                  Format
                </span>
              </div>

              <p
                v-if="examArrangement"
                class="mt-3 text-sm leading-7 text-slate-600 dark:text-slate-300"
              >
                {{ examArrangement }}
              </p>

              <div
                v-else
                class="mt-4 rounded-2xl border border-dashed border-violet-200/80 bg-white/60 p-4
                       dark:border-violet-400/20 dark:bg-slate-900/30"
              >
                <p class="text-sm font-bold text-slate-600 dark:text-slate-300">暂未收录考试安排</p>
                <p class="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
                  平台尚未采集到该赛事的赛制、考试或答辩安排。
                </p>
                <a
                  v-if="officialIsReal"
                  :href="officialUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="mt-3 inline-flex items-center gap-1 text-xs font-bold text-violet-600 hover:underline
                         dark:text-violet-400"
                >
                  去官网查看 <span aria-hidden="true">↗</span>
                </a>
              </div>
            </article>
          </div>

          <!-- 技术栈：有真实数据才渲染，不占空壳 -->
          <div
            v-if="techStack.length"
            class="mt-5 rounded-3xl border border-slate-200/70 bg-white p-6 shadow-sm
                   dark:border-slate-700 dark:bg-slate-800"
          >
            <div class="flex items-center gap-2">
              <span class="h-4 w-[3px] rounded-full bg-sky-500" aria-hidden="true"></span>
              <h3 class="text-sm font-bold tracking-wide text-slate-700 dark:text-slate-200">涉及技术栈</h3>
              <span class="ml-auto text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">
                Stack
              </span>
            </div>
            <ul class="mt-4 flex flex-wrap gap-2">
              <li
                v-for="tag in techStack"
                :key="tag"
                class="rounded-full bg-sky-50 px-3 py-1.5 text-xs font-bold text-sky-700
                       ring-1 ring-sky-200 dark:bg-sky-500/10 dark:text-sky-300 dark:ring-sky-500/20"
              >
                {{ tag }}
              </li>
            </ul>
          </div>
        </section>

        <!-- 5. 参赛全流程攻略：有内容直接渲染；为空时保留卡片外壳 + 生成按钮 -->
        <section id="guide" class="mt-8 scroll-mt-28">
          <div
            v-if="detail.full_process_guide"
            class="overflow-hidden rounded-3xl border border-slate-200/80 bg-white shadow-sm
                   dark:border-slate-700 dark:bg-slate-800"
          >
            <div
              class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/80
                     bg-gradient-to-r from-indigo-50 via-white to-sky-50 px-6 py-5
                     dark:border-slate-700 dark:from-indigo-500/[0.10] dark:via-slate-800 dark:to-sky-500/[0.06]"
            >
              <div class="flex items-center gap-3">
                <span
                  class="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-indigo-600
                         text-xl text-white shadow-sm shadow-indigo-600/25"
                  aria-hidden="true"
                >
                  🗺️
                </span>
                <div>
                  <h2 class="text-lg font-bold text-slate-900 dark:text-slate-100">参赛全流程攻略</h2>
                  <p class="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                    AI 依据平台已收录资料整理 · 具体规则请以官网当年通知为准
                  </p>
                </div>
              </div>
              <div class="flex flex-wrap items-center gap-2">
                <span
                  v-if="detail.guide_generated_at"
                  class="rounded-full bg-white/80 px-3 py-1 text-xs font-semibold text-slate-500
                         ring-1 ring-slate-200 dark:bg-slate-900/40 dark:text-slate-400 dark:ring-slate-700"
                >
                  生成于 {{ detail.guide_generated_at }}
                </span>
                <span
                  class="rounded-full bg-emerald-50 px-3 py-1 text-xs font-bold text-emerald-700
                         ring-1 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/20"
                >
                  AI 生成
                </span>
              </div>
            </div>

            <div
              class="prose prose-slate dark:prose-invert max-w-none px-6 py-6 text-slate-600
                     prose-headings:scroll-mt-28 prose-h2:text-xl prose-h2:font-bold
                     prose-h3:text-base prose-h3:font-bold prose-p:text-sm prose-p:leading-7
                     prose-li:text-sm prose-table:text-sm
                     dark:text-slate-300 sm:px-8"
              v-html="renderedGuide"
            ></div>
          </div>

          <div
            v-else
            class="relative overflow-hidden rounded-3xl border border-indigo-200/70
                   bg-gradient-to-br from-indigo-50 via-white to-sky-50 px-6 py-12 text-center
                   shadow-sm dark:border-indigo-400/20 dark:from-indigo-500/[0.10]
                   dark:via-slate-800 dark:to-sky-500/[0.06]"
          >
            <div
              class="pointer-events-none absolute -right-12 -top-12 h-48 w-48 rounded-full bg-indigo-200/40 blur-3xl
                     dark:bg-indigo-500/10"
              aria-hidden="true"
            ></div>

            <div class="relative">
              <div
                class="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-white text-2xl
                       shadow-sm ring-1 ring-indigo-100 dark:bg-slate-800 dark:ring-indigo-400/20"
                aria-hidden="true"
              >
                🗺️
              </div>
              <h2 class="mt-4 text-xl font-bold text-slate-900 dark:text-slate-100">参赛全流程攻略</h2>
              <p class="mx-auto mt-2 max-w-xl text-sm leading-6 text-slate-500 dark:text-slate-400">
                该赛事还没有生成结构化攻略。AI 可以基于平台已收录的赛事资料，整理报名、备赛、提交与答辩的完整流程。
              </p>

              <div v-if="guideBusy" class="mt-6">
                <p class="text-sm font-semibold text-indigo-700 dark:text-indigo-300">
                  ✨ AI 正在整理该赛事的参赛攻略…
                </p>
                <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  通常需要 30–60 秒，可以先浏览上方内容
                </p>
                <div class="mx-auto mt-4 h-1.5 w-44 overflow-hidden rounded-full bg-indigo-100 dark:bg-slate-700">
                  <div class="h-full w-1/2 animate-pulse rounded-full bg-indigo-500"></div>
                </div>
              </div>

              <template v-else>
                <button
                  v-if="canGenerateGuide"
                  type="button"
                  class="mt-6 inline-flex items-center justify-center gap-2 rounded-xl bg-indigo-600 px-6 py-3
                         text-sm font-bold text-white shadow-sm shadow-indigo-600/25 transition
                         hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60
                         dark:bg-indigo-500 dark:hover:bg-indigo-400"
                  :disabled="guideRequesting"
                  @click="onGenerateGuide"
                >
                  <span
                    v-if="guideRequesting"
                    class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
                  ></span>
                  <span>{{ guideRequesting ? '提交中…' : '✨ 生成 AI 攻略' }}</span>
                </button>

                <p v-if="guideError" class="mt-3 text-sm font-semibold text-red-500 dark:text-red-400">
                  {{ guideError }}
                </p>
                <p class="mt-4 text-xs text-slate-500 dark:text-slate-400">
                  需登录后使用 · 生成内容仅供参考，具体规则请以官网通知为准
                </p>
              </template>
            </div>
          </div>
        </section>
      </template>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Header from '@/components/Header.vue'
import { getCompetitionDetail, requestGuide, getGuideStatus } from '@/services/api'
import { isLoggedIn } from '@/services/auth'
import { openAuthModal } from '@/composables/useAuthModal'
import { coverFallback } from '@/utils/coverFallback'

const route = useRoute()

const detail = ref(null)
const loading = ref(true)
const errorMsg = ref('')

// 详情页大图兜底：无图 / 加载失败时用名称哈希渐变 + 首字，尺寸保持不变
const heroFailed = ref(false)
const heroFallback = computed(() => coverFallback(detail.value?.title || '竞赛'))

function onHeroError() {
  heroFailed.value = true
}

watch(
  () => detail.value?.image,
  () => {
    heroFailed.value = false
  }
)

// 详情页只渲染后端真实字段：
// 这里不再维护任何静态“套话”区块（时间安排 / 参赛对象 / 内容范围 …），
// 字段为空时整块隐藏，避免用通用文案冒充真实赛事信息。

// ---- 后端字段格式化 ----
// 详情接口返回的是 *月份*（reg_*_month / contest_*_month，1-12 的数字），
// 不是完整日期，因此“报名时间 / 比赛时间”必须在前端拼展示文案。
function toMonth(value) {
  if (value === null || value === undefined || value === '') return null
  const str = String(value).trim()

  if (/^\d{1,2}$/.test(str)) {
    const n = parseInt(str, 10)
    return n >= 1 && n <= 12 ? n : null
  }

  const parts = str.split(/[-/.\sT:]+/).filter(Boolean)
  if (parts.length >= 2) {
    if (parts[0].length === 4) {
      const m = parseInt(parts[1], 10)
      if (m >= 1 && m <= 12) return m
    }
    const m = parseInt(parts[0], 10)
    if (m >= 1 && m <= 12) return m
  }

  const d = new Date(str)
  if (!Number.isNaN(d.getTime())) return d.getMonth() + 1
  return null
}

function formatSignUp(item) {
  const start = toMonth(item.reg_start_month)
  const end = toMonth(item.reg_end_month)

  if (start && end) {
    const crossYear =
      item.reg_cross_year === true || item.reg_cross_year === 1 ||
      item.reg_cross_year === '1' || item.reg_cross_year === 'true' ||
      end < start

    if (crossYear && end < start) return `${start} 月 - 次年 ${end} 月`
    return `${start} 月 - ${end} 月`
  }
  if (start) return `${start} 月起`
  if (end) return `截止 ${end} 月`
  return '待公布'
}

// 同月 -> “11 月”；跨月 -> “10 月 - 11 月”；跨年 -> “12 月 - 次年 1 月”；缺值 -> “待定”
function formatContest(item) {
  const start = toMonth(item.contest_start_month)
  const end = toMonth(item.contest_end_month)

  if (start && end) {
    if (end < start) return `${start} 月 - 次年 ${end} 月`
    if (start === end) return `${start} 月`
    return `${start} 月 - ${end} 月`
  }
  if (start) return `${start} 月`
  if (end) return `${end} 月`
  return '待定'
}

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

// 参赛全流程攻略：Markdown 渲染 + XSS 净化
const renderedGuide = computed(() => {
  if (!detail.value?.full_process_guide) return ''
  return DOMPurify.sanitize(marked.parse(detail.value.full_process_guide))
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

// 官网域名（用于在没有完整 URL 时也能显示真实来源）
const officialHost = computed(() => {
  if (!officialUrl.value) return ''
  try {
    return new URL(officialUrl.value).host
  } catch (e) {
    return ''
  }
})

// 真实级别文案：优先后端 level_label，缺失时按 level 兜底
const levelText = computed(() => {
  const label = String(detail.value?.level_label || '').trim()
  if (label) return label
  if (detail.value?.level === 'provincial') return '省级'
  if (detail.value?.level === 'national') return '国家级'
  return ''
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

// 赛事档案里只要有一个真实字段就展示，全空时整块隐藏
const hasArchive = computed(
  () =>
    Boolean(officialUrl.value) ||
    Boolean(levelText.value) ||
    Boolean(String(detail.value?.category || '').trim()) ||
    introParagraphs.value.length > 0 ||
    techStack.value.length > 0
)
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
        <!-- 1. 全宽竞赛大图（无图/加载失败时走渐变首字兜底） -->
        <div class="mt-8 w-full">
          <div class="relative h-64 w-full overflow-hidden rounded-2xl bg-slate-50 dark:bg-slate-900">
            <img
              v-if="detail?.image && !heroFailed"
              :src="detail.image"
              alt=""
              class="h-full w-full object-cover"
              @error="onHeroError"
            />
            <div
              v-else
              class="cover-fallback cover-fallback--hero"
              :style="{ '--cover-hue': heroFallback.hue }"
              aria-hidden="true"
            >
              <span class="cover-fallback__glyph">{{ heroFallback.glyph }}</span>
            </div>
          </div>
        </div>

        <!-- 2. 信息区：超大加粗标题 + 时间 -->
        <section class="mt-10 pb-2">
          <h1 class="text-3xl font-black leading-tight tracking-tight text-slate-900 dark:text-slate-100 sm:text-4xl">
            {{ detail?.title }}
          </h1>

          <div class="mt-6 flex flex-wrap items-center gap-x-10 gap-y-3 text-base">
            <p class="flex items-center gap-2">
              <span class="font-medium text-slate-500 dark:text-slate-400">报名时间</span>
              <span class="font-bold text-slate-800 dark:text-slate-100">{{ detail?.signUpTime }}</span>
            </p>
            <p class="flex items-center gap-2">
              <span class="font-medium text-slate-500 dark:text-slate-400">比赛时间</span>
              <span class="font-bold text-slate-800 dark:text-slate-100">{{ detail?.contestTime }}</span>
            </p>
            <a
              v-if="officialUrl"
              :href="officialUrl"
              target="_blank"
              rel="noopener noreferrer"
              class="ml-auto inline-flex items-center gap-1 rounded-full bg-blue-50 px-3.5 py-1.5 text-xs font-bold
                     text-blue-700 ring-1 ring-blue-200 transition-colors
                     hover:bg-blue-100 hover:text-blue-800 hover:ring-blue-300
                     dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30
                     dark:hover:bg-blue-500/25 dark:hover:text-blue-200"
            >
              查看官网
              <span aria-hidden="true">↗</span>
            </a>
          </div>
        </section>

        <!-- 3. 赛事档案：全部字段来自 /api/competitions/{id}，没有数据就不渲染 -->
        <section
          v-if="hasArchive"
          class="mt-10 border-t border-slate-200 dark:border-slate-700 pt-8"
        >
          <h2 class="text-xl font-bold text-slate-900 dark:text-slate-100">赛事档案</h2>

          <dl class="mt-6 grid gap-4 sm:grid-cols-2">
            <div
              v-if="officialUrl"
              class="rounded-2xl bg-white p-5 ring-1 ring-slate-200/70 shadow-sm dark:bg-slate-800 dark:ring-slate-700 sm:col-span-2"
            >
              <dt class="text-xs font-bold tracking-wide text-slate-400 dark:text-slate-500">赛事官网</dt>
              <dd class="mt-2 flex flex-wrap items-center gap-2">
                <a
                  :href="officialUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="break-all text-sm font-bold text-blue-600 hover:text-blue-700 hover:underline dark:text-blue-400"
                >
                  {{ officialHost || officialUrl }}
                </a>
                <span class="text-xs text-slate-400 dark:text-slate-500">（点击核对当年通知）</span>
              </dd>
            </div>

            <div
              v-if="levelText"
              class="rounded-2xl bg-white p-5 ring-1 ring-slate-200/70 shadow-sm dark:bg-slate-800 dark:ring-slate-700"
            >
              <dt class="text-xs font-bold tracking-wide text-slate-400 dark:text-slate-500">赛事级别</dt>
              <dd class="mt-2 text-sm font-bold text-slate-800 dark:text-slate-100">{{ levelText }}</dd>
            </div>

            <div
              v-if="detail.category"
              class="rounded-2xl bg-white p-5 ring-1 ring-slate-200/70 shadow-sm dark:bg-slate-800 dark:ring-slate-700"
            >
              <dt class="text-xs font-bold tracking-wide text-slate-400 dark:text-slate-500">赛事类别</dt>
              <dd class="mt-2 text-sm font-bold text-slate-800 dark:text-slate-100">{{ detail.category }}</dd>
            </div>

            <div class="rounded-2xl bg-white p-5 ring-1 ring-slate-200/70 shadow-sm dark:bg-slate-800 dark:ring-slate-700">
              <dt class="text-xs font-bold tracking-wide text-slate-400 dark:text-slate-500">报名时间</dt>
              <dd class="mt-2 text-sm font-bold text-slate-800 dark:text-slate-100">{{ detail.signUpTime }}</dd>
            </div>

            <div class="rounded-2xl bg-white p-5 ring-1 ring-slate-200/70 shadow-sm dark:bg-slate-800 dark:ring-slate-700">
              <dt class="text-xs font-bold tracking-wide text-slate-400 dark:text-slate-500">比赛时间</dt>
              <dd class="mt-2 text-sm font-bold text-slate-800 dark:text-slate-100">{{ detail.contestTime }}</dd>
            </div>
          </dl>

          <div
            v-if="introParagraphs.length"
            class="mt-6 rounded-2xl bg-white p-6 ring-1 ring-slate-200/70 shadow-sm dark:bg-slate-800 dark:ring-slate-700"
          >
            <h3 class="text-lg font-bold text-slate-900 dark:text-slate-100">赛事简介</h3>
            <p
              v-for="(text, i) in introParagraphs"
              :key="i"
              class="mt-3 text-sm leading-7 text-slate-600 dark:text-slate-300"
            >
              {{ text }}
            </p>
          </div>

          <div
            v-if="techStack.length"
            class="mt-6 rounded-2xl bg-white p-6 ring-1 ring-slate-200/70 shadow-sm dark:bg-slate-800 dark:ring-slate-700"
          >
            <h3 class="text-lg font-bold text-slate-900 dark:text-slate-100">涉及技术栈</h3>
            <ul class="mt-3 flex flex-wrap gap-2">
              <li
                v-for="tag in techStack"
                :key="tag"
                class="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-600 dark:bg-slate-700 dark:text-slate-300"
              >
                {{ tag }}
              </li>
            </ul>
          </div>
        </section>

        <!-- 4. 参赛全流程攻略：有内容直接渲染；为空时提供「按需生成」入口 -->
        <div
          v-if="detail.full_process_guide"
          class="mt-8 bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700 rounded-2xl p-6"
        >
          <div class="mb-4 flex flex-wrap items-center justify-between gap-2">
            <h3 class="text-xl font-bold text-slate-800 dark:text-slate-100 flex items-center gap-2">🗺️ 参赛全流程攻略</h3>
            <p v-if="detail.guide_generated_at" class="text-xs text-slate-400 dark:text-slate-500">
              AI 生成于 {{ detail.guide_generated_at }}，请以官网通知为准
            </p>
          </div>
          <div class="prose prose-slate dark:prose-invert max-w-none text-slate-600 dark:text-slate-300" v-html="renderedGuide"></div>
        </div>

        <div
          v-else
          class="mt-8 rounded-2xl border border-dashed border-slate-300 bg-slate-50 px-6 py-8 text-center dark:border-slate-700 dark:bg-slate-800/30"
        >
          <template v-if="guideBusy">
            <p class="text-slate-600 dark:text-slate-300">✨ AI 正在整理该赛事的参赛攻略…</p>
            <p class="mt-2 text-xs text-slate-400 dark:text-slate-500">通常需要 30–60 秒，可以先浏览其他内容</p>
            <div class="mx-auto mt-4 h-1.5 w-40 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
              <div class="h-full w-1/2 animate-pulse rounded-full bg-blue-500"></div>
            </div>
          </template>
          <template v-else>
            <p class="text-slate-500 dark:text-slate-400">该赛事攻略正在收集中，敬请期待...</p>
            <button
              v-if="canGenerateGuide"
              type="button"
              class="mt-4 inline-flex items-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-blue-500 dark:hover:bg-blue-400"
              :disabled="guideRequesting"
              @click="onGenerateGuide"
            >
              <span
                v-if="guideRequesting"
                class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
              ></span>
              <span>{{ guideRequesting ? '提交中…' : '✨ 生成 AI 攻略' }}</span>
            </button>
            <p v-if="guideError" class="mt-3 text-sm text-red-500 dark:text-red-400">{{ guideError }}</p>
            <p class="mt-3 text-xs text-slate-400 dark:text-slate-500">
              需登录后使用；攻略由 AI 依据平台已有资料生成，具体时间与规则请以官网通知为准。
            </p>
          </template>
        </div>
      </template>
    </main>
  </div>
</template>

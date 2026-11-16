<script setup>
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import Header from '@/components/Header.vue'
import CompetitionCard from '@/components/CompetitionCard.vue'
import CoverArt from '@/components/CoverArt.vue'
import BackToTop from '@/components/BackToTop.vue'
import { useRoute, useRouter } from 'vue-router'
import { getCompetitions, getHotCompetitions, checkPrepared } from '@/services/api'
import { authState } from '@/services/auth'
import { theme } from '@/composables/useTheme'
// 卡片字段映射统一走 utils/competitionCard（首页 / 搜索页 / 备赛页共用一套口径）

// 当前路由：监听 ?q= 变化，承接顶部/大搜索框的跳转
const route = useRoute()
const router = useRouter()

const competitions = ref([])
// 收录总数：数据条上写「已收录竞赛 N 项」，数字来自后端全量列表，不写死
const totalCount = ref(0)
const loading = ref(true)
const errorMsg = ref('')

// 首页热门条数：与后端 /api/competitions/hot 的默认值保持一致
const HOT_LIMIT = 8

/* ---------------- 入场动效状态 ---------------- */
// heroReady：首屏元素是否已就绪。挂载后下一帧置 true，触发 hero 元素滑入。
// 用 ref + onMounted 而非纯 CSS animation，是为了让动画在「组件真正进入视口」时
// 才开始，避免首屏白屏期间动画已播完。
const heroReady = ref(false)
// listAnimKey：列表动画批次号。仅在「数据首次到位 / 列表内容被替换」时自增，
// 使 v-for 的 :key 前缀变化，从而重新挂载卡片、重放交错弹入动画。
// 点卡片、加备赛等交互不会改它，因此不会干扰后续操作。
const listAnimKey = ref(0)
// 上一次参与动画的数据签名，用于判断列表是否真的变了
let lastSignature = ''

// 时间展示统一走 utils/timeFormat（双轨制：精确日期优先，缺失降级到月份）
import { formatSignUp, formatContest, countdownState } from '../utils/timeFormat'

// 首页只负责「热门竞赛」：搜索统一交给独立页面 /search?q=
// 卡片字段映射来自 utils/competitionCard，避免各页面各写一份
import { mapCompetitionToCard as mapToCard } from '@/utils/competitionCard'
// ---------- 官方风右侧「情报速览」 ----------
// 示例提问：点一下填进搜索框并检索，顺手把 AI 语义检索的能力讲明白
const QUICK_QUERIES = [
  '我想找寒假能准备的算法赛',
  '还有哪些商科赛在报名期',
  '文科生能参加的国家级赛事'
]

// 近期截止：复用首页热门列表（后端已按紧迫度排序），取前 3 条
const deadlineBrief = computed(() =>
  competitions.value.slice(0, 3).map((c) => {
    const state = countdownState({
      reg_start_date: c.regStartDate,
      reg_end_date: c.regEndDate,
      contest_start_date: c.contestStartDate
    })
    const days = state && typeof state.days === 'number' ? state.days : null
    let badge = ''
    if (days !== null) {
      if (days < 0) badge = '已截止'
      else if (state.tone === 'soon') badge = `距开报 ${days} 天`
      else if (days === 0) badge = '今天截止'
      else badge = `剩 ${days} 天`
    }
    return {
      id: c.id,
      title: c.title,
      endText: c.regEndDate ? String(c.regEndDate).slice(5) : '',
      badge
    }
  })
)

function goDetail(id) {
  router.push(`/competition/${id}`)
}

function applyQuickQuery(text) {
  keyword.value = text
  submitSearch()
}

// preparedMap: competition id -> whether current user already joined preparation
const preparedMap = ref({})

// Batch-query preparation state for the given list (not logged in -> all false)
async function loadPreparedStates(list) {
  if (!authState.user || !Array.isArray(list) || list.length === 0) {
    preparedMap.value = {}
    return
  }
  try {
    const results = await Promise.all(
      list.map(async (item) => {
        const id = item.id
        if (id === null || id === undefined) return [null, false]
        const ok = await checkPrepared(id)
        return [String(id), ok]
      })
    )
    const next = {}
    for (const [key, ok] of results) {
      if (key !== null) next[key] = ok
    }
    preparedMap.value = next
  } catch (err) {
    console.error('[HomeView] load prepared states failed:', err)
    preparedMap.value = {}
  }
}

// Card emits preparation-change -> keep local state in sync (no refetch)
function onPreparationChange({ id, prepared }) {
  if (id === null || id === undefined) return
  preparedMap.value = { ...preparedMap.value, [String(id)]: prepared }
}

async function loadCompetitions() {
  loading.value = true
  errorMsg.value = ''
  try {
    // 首页热门：后端已按「国家级 + 报名中/未开始 + 紧迫度」排序并限流到 8 条，
    // 前端原样渲染（不做 sort / slice，避免打乱后端的紧迫度顺序）
    const list = await getHotCompetitions(HOT_LIMIT)
    competitions.value = list.map(mapToCard)
    bumpListAnimation()
    loadPreparedStates(competitions.value)
  } catch (err) {
    errorMsg.value = err?.message || '加载竞赛数据失败'
    competitions.value = []
  } finally {
    loading.value = false
  }
}


/**
 * 列表内容变化时递增批次号，重放交错入场动画
 * 签名 = 卡片 id 顺序拼接；仅当内容真的变了才重放，
 * 避免「点了备赛按钮 / 切换选中态」这类无关更新触发整列重播。
 */
function bumpListAnimation() {
  const signature = competitions.value.map((c) => c.id).join('|')
  if (signature === lastSignature) return
  lastSignature = signature
  listAnimKey.value += 1
}

/** 统计已收录竞赛条数（全量列表长度，失败则保持 0 由模板降级显示） */
async function loadTotalCount() {
  try {
    const all = await getCompetitions()
    totalCount.value = Array.isArray(all) ? all.length : 0
  } catch {
    totalCount.value = 0
  }
}

onMounted(() => {
  loadTotalCount()
  // 双 rAF：确保首帧已经绘制出「位移 + 透明」的初始态，
  // 下一帧再切到终态，过渡才会真正发生（单帧内改值可能被浏览器合并而跳过动画）
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      heroReady.value = true
    })
  })
})

/* ---------------- 搜索入口 ----------------
 * 首页不再承载检索结果：大搜索框只负责把关键词交给独立搜索页 /search?q=，
 * 检索、空态、URL 回放在 SearchView 里统一处理。
 * 这样首页永远是「热门竞赛」的稳定形态，也不会再出现搜索后跳回首页的割裂感。
 */
const keyword = ref('')   // 搜索框绑定值（仅本页输入用，不参与检索状态）

/** 提交搜索：交给独立搜索页；空输入直接聚焦回来，不做无意义跳转 */
function submitSearch() {
  const q = keyword.value.trim()
  if (!q) {
    keyword.value = ''
    return
  }
  router.push({ path: '/search', query: { q } })
}

/** 清空输入框（仅清本地输入，不影响页面内容） */
function submitReset() {
  keyword.value = ''
}

// 平滑滚动到竞赛列表区：「向下探索」按钮使用
function scrollToList() {
  if (typeof document === 'undefined') return
  const el = document.getElementById('competition-list')
  if (!el) return

  if (typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    return
  }

  // 兜底：极老浏览器不支持平滑滚动时，用 scrollTo 算绝对位置
  if (typeof window !== 'undefined' && typeof window.scrollTo === 'function') {
    const top = el.getBoundingClientRect().top + window.pageYOffset
    window.scrollTo({ top, behavior: 'smooth' })
  }
}

/** 「查看更多」引导按钮：热门列表加载完且非空时才出现 */
const showViewMore = computed(() => !loading.value && competitions.value.length > 0)

/** 点击「查看更多」→ 前往竞赛列表页 */
function goCompetitions() {
  router.push('/competitions')
}
</script>

<template>
  <!-- 「官方风」下方列表区为纯白，波浪渐变底部正好接上它；其余主题维持原底色 -->
  <div
    class="relative"
    :class="theme === 'official' ? 'bg-white' : 'bg-slate-50 dark:bg-slate-900'"
  >
    <Header />

    <!-- ========== 全屏 Hero 首屏 ========== -->
    <!-- 官方风把整块背景（含波浪）抬高 6rem，让下方多出一段白色留白 -->
    <section
      class="relative flex flex-col overflow-hidden"
      :class="theme === 'official' ? 'min-h-[calc(100vh_-_6rem)]' : 'min-h-screen'"
    >
      <!-- Hero 背景：与列表卡片、详情页同一套封面材质（CoverArt banner 变体）
           只当底片用，上面压一层径向白纱，中间文字区干净、四周露出纸纹/点阵/母题 -->
      <div class="pointer-events-none absolute inset-0 z-0" aria-hidden="true">
        <CoverArt variant="banner" class="absolute inset-0" title="库奥 大学生竞赛智库" />
        <div
          class="absolute inset-0"
          :class="theme === 'tech' ? 'hero-wash hero-wash--dark' : 'hero-wash hero-wash--light'"
        ></div>
      </div>

      <!-- 「官方风」专属：Hero 底部的多层渐变波浪过渡
           仅在图片背景主题下渲染；极简风 / 科技蓝直接让上下颜色自然相接。
           三层填充波浪，每层一条独立的垂直渐变、各代表一种颜色：
             1) 浅蓝层（最靠后）
             2) 薄荷绿层（中间）
             3) 白绿层（最前，底部纯白，接下方热门竞赛白底）
           三层速度/方向不同，波峰会不断相互交叉、此起彼伏；
           每条路径都画满两个周期，位移恰好一个周期，循环无缝。
           pointer-events-none 保证不拦截首屏交互。 -->
      <svg
        v-if="theme === 'official'"
        class="hero-waves pointer-events-none absolute inset-x-0 bottom-0 z-[1] h-[64px] w-full md:h-[92px]"
        viewBox="0 0 1440 140"
        preserveAspectRatio="none"
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          <!-- 第一层：浅蓝渐变 -->
          <linearGradient id="official-wave-layer-blue" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#7fb0d6" stop-opacity="0.85" />
            <stop offset="45%" stop-color="#b8d1e6" stop-opacity="0.5" />
            <stop offset="100%" stop-color="#e9f1f8" stop-opacity="0" />
          </linearGradient>
          <!-- 第二层：薄荷绿渐变 -->
          <linearGradient id="official-wave-layer-mint" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#9fb6d4" stop-opacity="0.7" />
            <stop offset="45%" stop-color="#c8d7e8" stop-opacity="0.4" />
            <stop offset="100%" stop-color="#f1f5fa" stop-opacity="0" />
          </linearGradient>
          <!-- 第三层（最前）：浅绿 → 纯白，底部与竞赛列表白底融为一体 -->
          <linearGradient id="official-hero-wave" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#dce7f6" />
            <stop offset="50%" stop-color="#eff3fa" />
            <stop offset="100%" stop-color="#ffffff" />
          </linearGradient>
        </defs>

        <!-- 第一层：浅蓝波浪（最靠后，波峰最高） -->
        <g class="hero-wave-layer-blue">
          <path
            d="M0,44 C240,22 480,22 720,44 C960,66 1200,66 1440,44
               C1680,22 1920,22 2160,44 C2400,66 2640,66 2880,44
               L2880,140 L0,140 Z"
            fill="url(#official-wave-layer-blue)"
          />
        </g>

        <!-- 第二层：薄荷绿波浪（中间） -->
        <g class="hero-wave-layer-mint">
          <path
            d="M0,54 C240,35 480,35 720,54 C960,73 1200,73 1440,54
               C1680,35 1920,35 2160,54 C2400,73 2640,73 2880,54
               L2880,140 L0,140 Z"
            fill="url(#official-wave-layer-mint)"
          />
        </g>

        <!-- 第三层：白绿波浪（最前，接白底） -->
        <g class="hero-wave-flow">
          <path
            d="M0,64 C240,48 480,48 720,64 C960,80 1200,80 1440,64
               C1680,48 1920,48 2160,64 C2400,80 2640,80 2880,64
               L2880,140 L0,140 Z"
            fill="url(#official-hero-wave)"
          />
        </g>
      </svg>

      <!-- 内容区：官方风左对齐排成「文件头」，其余主题保持水平居中 -->
      <div
        class="relative z-10 flex flex-1 flex-col justify-center px-6 pt-16 pb-52 xl:px-0"
        :class="theme === 'official'
          ? 'mx-auto w-full max-w-[1248px] items-start text-left'
          : 'items-center text-center'"
      >
        <div class="flex w-full flex-col xl:flex-row xl:items-start xl:justify-between xl:gap-20">
        <div
          :class="theme === 'official'
            ? 'w-full xl:max-w-[700px] xl:flex-1'
            : 'flex w-full flex-col items-center'"
        >
        <!-- 官方风：小型大写眉标 -->
        <p
          v-if="theme === 'official'"
          class="hero-in iss-eyebrow mb-7"
          :style="{ animationDelay: '0ms' }"
        >
          竞赛情报 · Competition Intelligence
        </p>

        <!-- 大标题 -->
        <h1
          class="hero-in"
          :class="theme === 'official'
            ? 'text-[42px] leading-[1.05] font-bold tracking-[-0.03em] md:text-[54px] lg:text-[70px]'
            : 'text-3xl sm:text-4xl md:text-5xl lg:text-6xl font-extrabold tracking-tight text-slate-900 dark:text-slate-100'"
          :style="{ animationDelay: '0ms' }"
        >
          大学生竞赛智库
        </h1>

        <!-- 副标题 -->
        <p
          class="hero-in mt-6 leading-relaxed font-medium"
          :class="theme === 'official'
            ? 'max-w-[46ch] text-[17px] leading-[1.7] text-slate-600'
            : 'max-w-none text-xs text-slate-500 dark:text-slate-400 sm:whitespace-nowrap sm:text-sm md:text-base'"
          :style="{ animationDelay: '120ms' }"
        >
          库奥 | 这是一个融合大学生竞赛信息与AI技术的智库，致力于助你科学、系统地完成整个竞赛周期。
        </p>

        <!-- 搜索框 -->
        <div
          class="hero-in hero-search mt-12 flex items-center gap-3 px-6 py-3
                 rounded-full bg-white/90 dark:bg-slate-800/90 backdrop-blur
                 shadow-xl shadow-slate-900/10 ring-1 ring-slate-200/70 dark:ring-slate-700
                 transition-shadow focus-within:shadow-2xl focus-within:shadow-blue-600/10
                 focus-within:ring-blue-300/60"
          :class="theme === 'official' ? 'w-full max-w-full' : 'mx-auto w-[90%] max-w-4xl md:w-full'"
          :style="{ animationDelay: '240ms' }"
        >
          <!-- 放大镜 Icon -->
          <svg
            class="h-6 w-6 shrink-0 text-slate-500 dark:text-slate-400"
            fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
            aria-hidden="true"
          >
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0z" />
          </svg>

          <input
            v-model="keyword"
            type="text"
            :placeholder="theme === 'official'
              ? '搜索赛事，或描述需求…'
              : '输入比赛名称，或像聊天一样描述需求（如：我想找寒假能准备的算法赛）...'"
            class="w-full bg-transparent text-[17px] text-slate-800 dark:text-slate-100 placeholder:text-slate-500 dark:placeholder:text-slate-500
                   font-medium focus:outline-none disabled:opacity-60"
            @keyup.enter="submitSearch"
          />

          <!-- 清空按钮：仅在有输入时显示 -->
          <button
            v-if="keyword"
            type="button"
            aria-label="清空搜索"
            class="mr-1 shrink-0 rounded-full p-1.5 text-slate-500 dark:text-slate-400
                   hover:bg-slate-100 dark:hover:bg-slate-700/60 hover:text-slate-600 dark:hover:text-slate-300 transition-colors
                   disabled:cursor-not-allowed disabled:opacity-50"
            @click="submitReset"
          >
            <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24"
                 stroke="currentColor" stroke-width="2.2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>

          <button
            class="shrink-0 rounded-full bg-blue-600 px-7 py-3 text-[15px] font-bold text-white
                   shadow-md shadow-blue-600/30 hover:bg-blue-700 dark:hover:bg-blue-600 transition-colors
                   disabled:cursor-not-allowed disabled:opacity-60"
            @click="submitSearch"
          >
            搜索
          </button>
        </div>

        <!-- 官方风：首屏数据条（情报台的量感，不用文档编号那套） -->
        <div
          v-if="theme === 'official'"
          class="hero-in stat-strip mt-12 w-full"
          :style="{ animationDelay: '320ms' }"
        >
          <div class="stat-strip__item">
            <span class="stat-strip__num">{{ totalCount || '—' }}<small>项</small></span>
            <span class="stat-strip__label">已收录竞赛</span>
          </div>
          <div class="stat-strip__item">
            <span class="stat-strip__num">AI</span>
            <span class="stat-strip__label">语义检索引擎</span>
          </div>
          <div class="stat-strip__item">
            <span class="stat-strip__num">全周期</span>
            <span class="stat-strip__label">报名 · 备赛 · 复盘</span>
          </div>
        </div>

        </div>

        <!-- 官方风：右侧情报栏（近期截止 + 示例提问），把首屏右半区用起来 -->
        <aside
          v-if="theme === 'official'"
          class="hero-in brief hidden w-[340px] shrink-0 xl:block"
          :style="{ animationDelay: '360ms' }"
          aria-label="情报速览"
        >
          <div class="brief__head">
            <span class="brief__eyebrow">情报速览</span>
            <span class="brief__en">BRIEF</span>
          </div>

          <p class="brief__label">近期截止</p>
          <ul class="brief__list">
            <li
              v-for="item in deadlineBrief"
              :key="item.id"
              class="brief__row"
              role="button"
              tabindex="0"
              @click="goDetail(item.id)"
              @keyup.enter="goDetail(item.id)"
            >
              <span class="brief__name">{{ item.title }}</span>
              <span class="brief__meta">
                <span v-if="item.endText">报名截止 {{ item.endText }}</span>
                <span v-else>报名时间待定</span>
                <span v-if="item.badge" class="brief__days">{{ item.badge }}</span>
              </span>
            </li>
            <li v-if="!deadlineBrief.length" class="brief__row brief__row--empty">暂无在报赛事</li>
          </ul>
          <button type="button" class="brief__more" @click="goCompetitions">
            查看全部赛事
            <span aria-hidden="true">→</span>
          </button>

          <p class="brief__label brief__label--gap">试试这样问</p>
          <ul class="brief__chips">
            <li v-for="q in QUICK_QUERIES" :key="q">
              <button type="button" class="brief-chip" @click="applyQuickQuery(q)">{{ q }}</button>
            </li>
          </ul>
        </aside>
        </div>
      </div>

      <!-- 首屏底部居中：向下探索 -->
      <button
        type="button"
        class="absolute bottom-24 z-10 flex flex-col items-center gap-2 text-slate-500 dark:text-slate-400
               transition-colors hover:text-blue-600 dark:hover:text-blue-400
               focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded-lg"
        :class="theme === 'official' ? 'left-[max(24px,calc(50vw-624px))] hidden md:flex' : 'left-1/2 -translate-x-1/2'"
        @click="scrollToList"
      >
        <span class="text-sm font-bold tracking-widest">向下探索</span>
        <svg
          class="h-6 w-6 animate-bounce"
          fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
          aria-hidden="true"
        >
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 14l-7 7-7-7M19 5l-7 7-7-7" />
        </svg>
      </button>
    </section>

    <!-- ========== 首屏之外的竞赛列表 ========== -->
    <section
      id="competition-list"
      class="mx-auto w-[92%] max-w-[1248px] pt-24 pb-28 scroll-mt-24"
    >
      <div
        class="mb-10"
        :class="theme === 'official' ? 'doc-section-head' : 'text-center'"
      >
        <span v-if="theme === 'official'" class="doc-kicker mb-3">01 · 赛事索引</span>
        <h2 class="text-3xl font-extrabold tracking-tight text-slate-900 dark:text-slate-100">
          热门竞赛
        </h2>
        <p class="mt-2 text-base font-medium text-slate-500 dark:text-slate-400">
          为你精选的高含金量赛事
        </p>
      </div>

      <!-- 加载中：骨架屏 -->
      <div v-if="loading" class="space-y-6">
        <div
          v-for="n in 3"
          :key="n"
          class="flex animate-pulse gap-7 rounded-2xl bg-white dark:bg-slate-800 p-6 ring-1 ring-slate-200/70 dark:ring-slate-700"
        >
          <div class="w-52 shrink-0 rounded-xl bg-slate-200/80 dark:bg-slate-700" style="aspect-ratio: 4 / 3"></div>
          <div class="flex flex-1 flex-col">
            <div class="h-6 w-2/5 rounded bg-slate-200/80 dark:bg-slate-700"></div>
            <div class="mt-4 h-4 w-1/2 rounded bg-slate-200/60 dark:bg-slate-700"></div>
            <div class="mt-2 h-4 w-1/3 rounded bg-slate-200/60 dark:bg-slate-700"></div>
            <div class="mt-auto flex justify-end gap-3 pt-5">
              <div class="h-8 w-20 rounded-full bg-slate-200/60 dark:bg-slate-700"></div>
              <div class="h-8 w-20 rounded-full bg-slate-200/60 dark:bg-slate-700"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- 请求失败 -->
      <div
        v-else-if="errorMsg"
        class="rounded-2xl border border-red-100 dark:border-red-500/30 bg-red-50/60 dark:bg-red-500/10 px-6 py-12 text-center"
      >
        <div class="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-red-100 dark:bg-red-500/15">
          <svg class="h-6 w-6 text-red-500 dark:text-red-400" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
          </svg>
        </div>
        <p class="mt-4 font-bold text-slate-700 dark:text-slate-200">竞赛数据加载失败</p>
        <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">{{ errorMsg }}</p>
        <button
          class="mt-6 rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white
                 shadow-sm shadow-blue-600/25 hover:bg-blue-700 dark:hover:bg-blue-600 transition-colors"
          @click="loadCompetitions"
        >
          重新加载
        </button>
      </div>

      <!-- 空数据 -->
      <div
        v-else-if="competitions.length === 0"
        class="rounded-2xl border border-dashed border-slate-200 dark:border-slate-700 px-6 py-16 text-center"
      >
        <p class="font-bold text-slate-500 dark:text-slate-400">
          暂无竞赛数据
        </p>
        <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
          稍后再来看看吧
        </p>
      </div>

      <!-- 正常列表 -->
      <div v-else :key="listAnimKey" class="space-y-6">
        <CompetitionCard
          v-for="(item, index) in competitions"
          :key="listAnimKey + '-' + item.id"
          :id="item.id"
          class="card-in"
          :style="{ animationDelay: index * 100 + 'ms' }"
          :image="item.image"
          :short-name="item.shortName"
          :title="item.title"
          :category="item.category"
          :sign-up-time="item.signUpTime"
          :contest-time="item.contestTime"
          :reg-start-date="item.regStartDate"
          :reg-end-date="item.regEndDate"
          :contest-start-date="item.contestStartDate"
          :prepared="preparedMap[String(item.id)] === true"
          @preparation-change="onPreparationChange"
        />
      </div>

      <!-- 查看更多：仅在默认「热门竞赛」态下展示，居中吸引导流到完整列表 -->
      <div v-if="showViewMore" class="mt-14 flex justify-center">
        <button
          type="button"
          class="group inline-flex items-center gap-2 rounded-full
                 bg-white dark:bg-slate-800 px-7 py-3 text-sm font-bold text-slate-700 dark:text-slate-200
                 ring-1 ring-slate-200 dark:ring-slate-700 shadow-sm shadow-slate-900/5
                 transition-all duration-300
                 hover:-translate-y-0.5 hover:text-blue-600 dark:hover:text-blue-400
                 hover:ring-blue-200 hover:shadow-md hover:shadow-blue-600/10
                 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500
                 cursor-pointer"
          @click="goCompetitions"
        >
          <span>查看全部竞赛</span>
          <svg
            class="h-4 w-4 transition-transform duration-300 group-hover:translate-x-1"
            fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"
            aria-hidden="true"
          >
            <path stroke-linecap="round" stroke-linejoin="round" d="M13 7l5 5-5 5M6 12h12" />
          </svg>
        </button>
      </div>
    </section>

    <!-- 全局回到顶部 -->
    <BackToTop />
  </div>
</template>

<style scoped>
/* 波浪压在纸面地平线上，压到 0.6 才不会在浅底上跳出来 */
.hero-waves {
  opacity: 0.6;
}

/* Hero 底片的纱层：中间留白给标题和搜索框，四周逐步露出材质 */
.hero-wash--light {
  background:
    radial-gradient(120% 92% at 50% 38%, rgb(255 255 255 / 0.9) 0%, rgb(255 255 255 / 0.78) 40%,
      rgb(255 255 255 / 0.42) 68%, rgb(255 255 255 / 0.12) 100%);
}

.hero-wash--dark {
  background:
    radial-gradient(120% 92% at 50% 38%, rgb(7 16 38 / 0.88) 0%, rgb(7 16 38 / 0.74) 42%,
      rgb(7 16 38 / 0.42) 70%, rgb(7 16 38 / 0.16) 100%);
}

/* 多层波浪持续交错流动：整组左移一个完整周期（1440 用户单位）后无缝回到起点。
   三层速度/方向不同，波峰相互交叉、此起彼伏，形成叠动的渐变层次。 */
.hero-wave-flow {
  animation: heroWaveFlow 18s linear infinite;
  will-change: transform;
}

.hero-wave-layer-mint {
  animation: heroWaveFlow 26s linear infinite reverse;
  animation-delay: -7s;
  will-change: transform;
}

.hero-wave-layer-blue {
  animation: heroWaveFlow 34s linear infinite;
  animation-delay: -14s;
  will-change: transform;
}

@keyframes heroWaveFlow {
  from {
    transform: translateX(0);
  }
  to {
    transform: translateX(-1440px);
  }
}

/* 系统开启「减弱动态效果」时停止流动，保持静态波浪 */
@media (prefers-reduced-motion: reduce) {
  .hero-wave-flow,
  .hero-wave-layer-mint,
  .hero-wave-layer-blue {
    animation: none;
  }
}

/* ---------------- 首屏入场：由下往上 + 由浅入深 ---------------- */
/* forwards 保持终态，避免动画结束后跳回初始的透明/位移态 */
@keyframes fadeSlideUp {
  from {
    opacity: 0;
    transform: translateY(24px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.hero-in {
  opacity: 0;
  animation: fadeSlideUp 0.7s cubic-bezier(0.22, 1, 0.36, 1) forwards;
  /* 抬升到独立图层，保证 transform / opacity 走合成器 */
  will-change: transform, opacity;
}

/* ---------------- 卡片：缩小 + 淡入 → 正常 ---------------- */
@keyframes cardPopIn {
  from {
    opacity: 0;
    transform: scale(0.94) translateY(16px);
  }
  to {
    opacity: 1;
    transform: scale(1) translateY(0);
  }
}

.card-in {
  opacity: 0;
  animation: cardPopIn 0.55s cubic-bezier(0.22, 1, 0.36, 1) both;
}

/* ---------------- 减少动态效果偏好 ---------------- */
/* 系统开启「减弱动态效果」时直接呈现终态，不做位移与缩放 */
@media (prefers-reduced-motion: reduce) {
  .hero-in,
  .card-in {
    animation: none;
    opacity: 1;
    transform: none;
  }
}
</style>

<script setup>
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import Header from '@/components/Header.vue'
import CompetitionCard from '@/components/CompetitionCard.vue'
import BackToTop from '@/components/BackToTop.vue'
import { useRoute, useRouter } from 'vue-router'
import { getCompetitions, searchCompetitions, checkPrepared } from '@/services/api'
import { authState } from '@/services/auth'

// 当前路由：监听 ?q= 变化，承接顶部/大搜索框的跳转
const route = useRoute()
const router = useRouter()

const competitions = ref([])
const loading = ref(true)
const errorMsg = ref('')

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

// 只取月份数字：兼容 "2025-03-01" / "2025.03.01" / "2025/3/5" / 纯数字月份 "3" / Date 字符串
function toMonth(value) {
  if (value === null || value === undefined || value === '') return null
  const str = String(value).trim()

  // 纯数字：可能是月份(1-12)或时间戳
  if (/^\d{1,2}$/.test(str)) {
    const n = parseInt(str, 10)
    return n >= 1 && n <= 12 ? n : null
  }

  // 带分隔符的日期：按 - . / 拆分，从候选中挑出 1-12 的月份
  // 形如 2025-03-01：年份是 4 位，月份是第 2 段
  const parts = str.split(/[-/.\sT:]+/).filter(Boolean)
  if (parts.length >= 2) {
    if (parts[0].length === 4) {
      const m = parseInt(parts[1], 10)
      if (m >= 1 && m <= 12) return m
    }
    // 形如 03-01（无年份）：第 1 段即月份
    const m = parseInt(parts[0], 10)
    if (m >= 1 && m <= 12) return m
  }

  // 兜底交给 Date 解析
  const d = new Date(str)
  if (!Number.isNaN(d.getTime())) return d.getMonth() + 1
  return null
}

// 报名时间格式化为 "X 月 - Y 月"
function formatSignUp(item) {
  const start = toMonth(item.reg_start_month)
  const end = toMonth(item.reg_end_month)

  if (start && end) {
    // 跨年报名：结束月份小于开始月份，例如 12 月 - 次年 1 月
    // 后端 reg_cross_year 为真时也按跨年处理
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

// 级别兜底：后端 level 是枚举 national/provincial，中文标签在 level_label
const LEVEL_FALLBACK = {
  national: '国家级',
  provincial: '省级',
  municipal: '市级',
  regional: '区域级',
  international: '国际级'
}

// 后端字段 -> 卡片 props（以后端真实字段为准）
function mapToCard(item, index) {
  const level = item.level_label || LEVEL_FALLBACK[item.level] || item.level || ''
  return {
    id: item.id ?? index,
    title: item.name || '未命名竞赛',
    level,
    category: item.category || item.subject || '',
    image: item.image ?? item.cover ?? '',
    signUpTime: formatSignUp(item),
    contestTime: '待定'
  }
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
    const list = await getCompetitions()
    competitions.value = list.slice(0, 8).map(mapToCard)
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

onMounted(() => {
  // 双 rAF：确保首帧已经绘制出「位移 + 透明」的初始态，
  // 下一帧再切到终态，过渡才会真正发生（单帧内改值可能被浏览器合并而跳过动画）
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      heroReady.value = true
    })
  })
})

/* ---------------- AI 语义检索 ---------------- */
const keyword = ref('')        // 搜索框绑定值
// 检索中标志：每次组件挂载都是全新实例，初始值绝对为 false
// 只由 runSearch 的 finally 复位，避免跨组件实例状态污染
const searching = ref(false)
const searchError = ref('')    // 检索错误
const searched = ref(false)    // 是否已执行过检索（决定列表标题/空态文案）


function resetToDefault() {
  keyword.value = ''
  searched.value = false
  searchError.value = ''
  loadCompetitions()
}

/**
 * 大搜索框唯一入口：只负责改地址栏，不直接发起请求
 *
 * 设计意图：
 *   让 ?q= 成为唯一状态源（Single Source of Truth）。
 *   无论顶部小搜索框还是首页大搜索框，都只 push 路由，
 *   真正的检索统一交给下面的 watch(() => route.query.q) 执行。
 *
 * 好处：
 *   - 两个入口行为完全一致，不会出现"一套状态两套逻辑"
 *   - 浏览器前进/后退、刷新、分享链接都能正确复现检索结果
 *
 * 边界处理：
 *   若当前 URL 的 q 已经等于目标值，router.push 不会产生变化，
 *   watch 也就不会触发 → 此时需要主动 forceReload 一次。
 */
function submitSearch() {
  const q = keyword.value.trim()

  // 空输入：清空 q 回到默认列表（同样通过 URL 驱动）
  if (!q) {
    submitReset()
    return
  }

  const currentQ = typeof route.query.q === 'string' ? route.query.q.trim() : ''

  if (currentQ === q) {
    // URL 未变（重复搜同一关键词）→ watch 不触发，手动放行一次
    runSearch(q)
    return
  }

  // 统一走路由：状态变化完全由 URL 驱动
  router.push({ path: '/', query: { q } })
}

/**
 * 清空搜索：同样通过 URL 驱动，保证与 submitSearch 一致
 */
function submitReset() {
  keyword.value = ''

  // 有 q 时才需要改 URL，否则 watch 不会触发，手动恢复列表
  if (route.query.q !== undefined) {
    router.push({ path: '/', query: {} })
    return
  }

  resetToDefault()
}

/**
 * 请求序号：用于竞态隔离
 * 每次发起检索自增，只有"最后一次"请求的回调才允许改写 UI 状态，
 * 避免快速切换页面/重复触发时旧请求覆盖新结果。
 */
let requestId = 0

// 组件卸载标记：卸载后不再写响应式状态，防止幽灵更新
// 注意：requestId / unmounted 都是每次 setup 新建的闭包变量，
//      旧实例自增自己的 requestId 不会影响新实例的计数器，
//      因此不会出现"旧组件卸载把新组件状态搞乱"的问题。
let unmounted = false

onBeforeUnmount(() => {
  unmounted = true
  // 只作废【本实例】的在途请求（闭包内独立计数器，不跨实例）。
  // 自增后正在跑的请求 currentId !== requestId，会走 finally 但不再写状态。
  requestId++
})

/**
 * 执行 AI 检索
 * 注意：本函数不再做 searching 死锁拦截，重复调用由 watch 单一入口保证，
 *      竞态由 requestId 兜底，finally 无条件复位 searching。
 * @param {string} [rawQuery] 传入检索词；不传则取输入框当前值
 */
async function runSearch(rawQuery) {
  const q = String(rawQuery ?? keyword.value ?? '').trim()

  // 空输入：不请求，直接回到默认列表
  if (!q) {
    resetToDefault()
    return
  }

  const currentId = ++requestId

  searching.value = true
  // 关键：检索本身就是一次"首屏加载"。
  // 跨页搜索时组件是全新挂载的，loading 还是初始值 true，
  // 若不在这里接管，检索结束 searching 复位后，
  // 模板里的 v-if="loading && !searching" 会重新命中，骨架屏永久卡住。
  loading.value = true
  searchError.value = ''

  try {
    const res = await searchCompetitions(q)

    // 已被更新的请求取代（含组件卸载时自增的那次）→ 放弃结果
    if (currentId !== requestId) return

    competitions.value = (res.items || []).map(mapToCard)
    bumpListAnimation()
    searched.value = true
    // 检索后滚动到列表区，让用户立刻看到结果
    scrollToList()
  } catch (err) {
    if (currentId !== requestId) return
    searchError.value = err?.message || 'AI 检索失败，请稍后重试'
    competitions.value = []
    searched.value = true
  } finally {
    // 无条件复位：只要仍是"最后一次"请求，就把两个加载态一起收掉。
    // 成功 / 失败 / 早退 / 竞态被取代，任何路径都不会留下卡住的骨架屏。
    if (currentId === requestId) {
      searching.value = false
      loading.value = false
    }
  }
}

// 平滑滚动到竞赛列表区：搜索成功后、「向下探索」按钮共用
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

/**
 * 唯一的数据入口：监听地址栏 ?q=
 * - immediate: true  → 组件挂载时立即执行一次，天然取代 onMounted 的判断逻辑
 * - 单一入口        → 避免"onMounted + watch"双触发导致的并发冲突
 *
 * 语义：
 *   q 有值  → 回填输入框并检索
 *   q 为空  → 加载默认热门列表（仅首次挂载时需要，之后由 resetToDefault 负责）
 */
/**
 * 是否展示「查看更多」引导按钮
 * 仅在默认「热门竞赛」态下出现：
 *   - !searched   → 非 AI 检索结果态
 *   - !loading    → 骨架屏已结束
 *   - !searching  → 不在检索进行中
 *   - 列表非空    → 避免空态下引导到列表页显得突兀
 */
const showViewMore = computed(
  () => !searched.value && !loading.value && !searching.value && competitions.value.length > 0
)

/** 点击「查看更多」→ 前往竞赛列表页 */
function goCompetitions() {
  router.push('/competitions')
}

watch(
  () => route.query.q,
  (q, oldQ) => {
    if (unmounted) return

    const val = typeof q === 'string' ? q.trim() : ''

    if (val) {
      keyword.value = val
      runSearch(val)
      return
    }

    // 无 q：首次挂载直接加载热门列表
    if (oldQ === undefined) {
      loadCompetitions()
      return
    }

    // 原本有 q、现在被清掉（如点 Logo 返回首页）→ 恢复正常列表
    resetToDefault()
  },
  { immediate: true }
)
</script>

<template>
  <div class="relative bg-slate-50 dark:bg-slate-900">
    <Header />

    <!-- ========== 全屏 Hero 首屏 ========== -->
    <section class="relative flex min-h-screen flex-col overflow-hidden">
      <!-- 背景装饰光晕 -->
      <div class="pointer-events-none absolute inset-0 -z-10">
        <div class="absolute -top-32 left-1/2 -translate-x-1/2 h-[420px] w-[820px]
                    rounded-full bg-blue-300/30 blur-3xl"></div>
        <div class="absolute top-40 -left-24 h-[320px] w-[320px]
                    rounded-full bg-sky-200/40 blur-3xl"></div>
        <div class="absolute top-24 -right-24 h-[320px] w-[320px]
                    rounded-full bg-indigo-200/40 blur-3xl"></div>
      </div>

      <!-- 内容区：水平居中，整体上移，使胶囊搜索框落在视口垂直正中 -->
      <div class="flex flex-1 flex-col items-center justify-center px-6 pt-16 pb-52 text-center">
        <!-- 大标题 -->
        <h1
          class="hero-in text-3xl sm:text-4xl md:text-5xl lg:text-6xl font-extrabold tracking-tight text-slate-900 dark:text-slate-100"
          :style="{ animationDelay: '0ms' }"
        >
          大学生竞赛智库
        </h1>

        <!-- 副标题 -->
        <p
          class="hero-in mt-6 max-w-none text-xs leading-relaxed text-slate-500 dark:text-slate-400 font-medium sm:whitespace-nowrap sm:text-sm md:text-base"
          :style="{ animationDelay: '120ms' }"
        >
          库奥 | 这是一个融合大学生竞赛信息与AI技术的智库，致力于助你科学、系统地完成整个竞赛周期。
        </p>

        <!-- 搜索框 -->
        <div
          class="hero-in mx-auto mt-12 flex w-[90%] max-w-4xl items-center gap-3 md:w-full
                 rounded-full bg-white/90 dark:bg-slate-800/90 backdrop-blur
                 px-6 py-3
                 shadow-xl shadow-slate-900/10 ring-1 ring-slate-200/70 dark:ring-slate-700
                 transition-shadow focus-within:shadow-2xl focus-within:shadow-blue-600/10
                 focus-within:ring-blue-300/60"
          :style="{ animationDelay: '240ms' }"
        >
          <!-- 放大镜 Icon -->
          <svg
            class="h-6 w-6 shrink-0 text-slate-400"
            fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
            aria-hidden="true"
          >
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0z" />
          </svg>

          <input
            v-model="keyword"
            type="text"
            placeholder="输入比赛名称，或像聊天一样描述需求（如：我想找寒假能准备的算法赛）..."
            class="w-full bg-transparent text-base text-slate-800 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500
                   font-medium focus:outline-none disabled:opacity-60"
            :disabled="searching"
            @keyup.enter="submitSearch"
          />

          <!-- 清空按钮：仅在有输入时显示 -->
          <button
            v-if="keyword"
            type="button"
            aria-label="清空搜索"
            class="mr-1 shrink-0 rounded-full p-1.5 text-slate-400
                   hover:bg-slate-100 dark:hover:bg-slate-700/60 hover:text-slate-600 dark:hover:text-slate-300 transition-colors
                   disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="searching"
            @click="submitReset"
          >
            <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24"
                 stroke="currentColor" stroke-width="2.2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>

          <button
            class="shrink-0 rounded-full bg-blue-600 px-6 py-2.5 text-sm font-bold text-white
                   shadow-md shadow-blue-600/30 hover:bg-blue-700 dark:hover:bg-blue-600 transition-colors
                   disabled:cursor-not-allowed disabled:opacity-60"
            :disabled="searching"
            @click="submitSearch"
          >
            {{ searching ? '匹配中…' : 'AI检索' }}
          </button>
        </div>

        <!-- 检索失败提示 -->
        <p v-if="searchError" class="mt-3 flex items-center gap-1.5 text-sm font-medium text-rose-600">
          <svg class="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
          </svg>
          {{ searchError }}
        </p>
      </div>

      <!-- 首屏底部居中：向下探索 -->
      <button
        type="button"
        class="absolute bottom-10 left-1/2 -translate-x-1/2
               flex flex-col items-center gap-2 text-slate-400
               transition-colors hover:text-blue-600 dark:hover:text-blue-400
               focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded-lg"
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
      class="mx-auto w-[85%] max-w-6xl pt-24 pb-28 scroll-mt-24"
    >
      <div class="mb-10 text-center">
        <h2 class="text-2xl font-extrabold tracking-tight text-slate-900 dark:text-slate-100">
          {{ searched ? '检索结果' : '热门竞赛' }}
        </h2>
        <p class="mt-2 text-sm font-medium text-slate-400">
          {{ searched ? 'AI 根据你的描述匹配的赛事' : '为你精选的高含金量赛事' }}
        </p>
      </div>

      <!-- AI 检索中 -->
      <div v-if="searching" class="mb-8 flex items-center justify-center gap-3
                                  rounded-2xl bg-blue-50/70 dark:bg-blue-500/15 px-6 py-4 ring-1 ring-blue-100 dark:ring-blue-500/30">
        <span class="h-3 w-3 animate-ping rounded-full bg-blue-500"></span>
        <p class="text-sm font-bold text-blue-700 dark:text-blue-300">AI 正在深度匹配...</p>
      </div>

      <!-- 加载中：骨架屏 -->
      <div v-if="loading && !searching" class="space-y-6">
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
          {{ searched ? '未找到相关赛事，换个描述试试' : '暂无竞赛数据' }}
        </p>
        <p class="mt-1 text-sm text-slate-400">
          {{ searched ? '可以试试更宽泛的方向，或更换关键词' : '稍后再来看看吧' }}
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
          :title="item.title"
          :level="item.level"
          :category="item.category"
          :sign-up-time="item.signUpTime"
          :contest-time="item.contestTime"
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

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Header from '@/components/Header.vue'
import BackToTop from '@/components/BackToTop.vue'
import CompetitionCard from '@/components/CompetitionCard.vue'
import {
  checkPrepared,
  searchCompetitionList,
  searchCompetitions
} from '@/services/api'
import { authState } from '@/services/auth'
import { openAuthModal } from '@/composables/useAuthModal'
import { mapCompetitionToCard } from '@/utils/competitionCard'

const route = useRoute()
const router = useRouter()

/* ---------------- 页面状态 ---------------- */
const keyword = ref('')      // 搜索框绑定值（回显 URL 中的 q）
const results = ref([])      // 卡片 props 数组
const loading = ref(false)
const errorMsg = ref('')
const searched = ref(false)  // 是否已执行过检索（决定空态文案）
const semantic = ref(false)  // 结果是否来自 AI 语义检索兜底
const semanticTried = ref(false)   // 本次查询是否已经试过 AI 语义检索
const semanticBusy = ref(false)    // AI 语义检索进行中
const semanticError = ref('')      // AI 语义检索失败原因（未登录/配额/网络）
const preparedMap = ref({})

const query = computed(() => {
  const raw = route.query.q
  return (typeof raw === 'string' ? raw : Array.isArray(raw) ? raw[0] : '') || ''
})

const HOT_KEYWORDS = ['程序设计', '数学建模', '机器人', '创新创业', '工业设计', '电子设计']

/* ---------------- 竞态隔离 ---------------- */
let requestId = 0
let unmounted = false
onBeforeUnmount(() => {
  unmounted = true
  requestId++
})

/**
 * 是否是「描述型需求」而非具体赛事名。
 * 只有这类查询才值得动用 AI 语义兜底：
 * 关键词检索无结果时，短词/专有名词（如「量子织布机」）应该老老实实给空态，
 * 而不是让语义模型硬凑一堆不相关的赛事。
 */
const INTENT_HINTS = /(我想|想找|想参加|适合|能不能|可以参加|有没有|推荐|哪些|什么|如何|怎么|准备|方向|寒假|暑假|大一|大二|大三|文科|理科|零基础)/

function looksLikeIntent(q) {
  return q.length >= 10 || INTENT_HINTS.test(q)
}

/**
 * 检索主入口：统一由 URL 的 ?q= 驱动
 *   1) 先走公开关键词检索（快、字段全，游客可用）
 *   2) 关键词命中为空、且查询是「描述型需求」、且已登录时，再用 AI 语义检索兜底
 */
async function runSearch(rawQuery) {
  const q = String(rawQuery ?? '').trim()
  keyword.value = q

  if (!q) {
    results.value = []
    searched.value = false
    semantic.value = false
    errorMsg.value = ''
    loading.value = false
    return
  }

  const currentId = ++requestId
  loading.value = true
  errorMsg.value = ''
  semantic.value = false
  semanticTried.value = false
  semanticError.value = ''

  try {
    let items = await searchCompetitionList(q)

    if (!items.length && authState.user && looksLikeIntent(q)) {
      try {
        const res = await searchCompetitions(q)
        items = res.items || []
        semantic.value = items.length > 0
      } catch (err) {
        // 语义兜底失败不影响主流程（可能是配额用尽）
        console.warn('[SearchView] semantic fallback skipped:', err?.message || err)
      } finally {
        semanticTried.value = true
      }
    }

    if (currentId !== requestId) return
    results.value = items.map((item, index) => mapCompetitionToCard(item, index))
    searched.value = true
    loadPreparedStates(results.value)
  } catch (err) {
    if (currentId !== requestId) return
    errorMsg.value = err?.message || '搜索失败，请稍后重试'
    results.value = []
    searched.value = true
  } finally {
    if (currentId === requestId) loading.value = false
  }
}

/**
 * 显式「AI 语义搜索」：空状态里的主动入口。
 * 像「文科生能参加的国家级赛事」这种句子，关键词检索天生无能为力，
 * 是否花一次 AI 配额交给用户自己点（游客先引导登录）。
 */
async function runSemantic() {
  const q = query.value.trim()
  if (!q) return
  if (!authState.user) {
    openAuthModal('login')
    return
  }

  const currentId = ++requestId
  semanticBusy.value = true
  semanticError.value = ''
  try {
    const res = await searchCompetitions(q)
    if (currentId !== requestId) return
    const items = res.items || []
    results.value = items.map((item, index) => mapCompetitionToCard(item, index))
    semantic.value = items.length > 0
    searched.value = true
    if (!items.length) semanticError.value = 'AI 也没找到贴合这句描述的赛事，换个说法再试试'
    loadPreparedStates(results.value)
  } catch (err) {
    if (currentId !== requestId) return
    semanticError.value = err?.message || 'AI 语义检索失败，请稍后重试'
  } finally {
    if (currentId === requestId) {
      semanticBusy.value = false
      semanticTried.value = true
    }
  }
}

// 登录后如果当前关键词还是 0 结果，自动帮用户把这句话交给 AI 再试一次
watch(
  () => authState.user,
  (user, prev) => {
    if (user && !prev && searched.value && results.value.length === 0 && !semanticTried.value) {
      runSemantic()
    }
  }
)

/** 备赛状态批量查询（未登录全部 false） */
async function loadPreparedStates(list) {
  if (!authState.user || !Array.isArray(list) || list.length === 0) {
    preparedMap.value = {}
    return
  }
  try {
    const pairs = await Promise.all(
      list.map(async (item) => {
        const id = item.id
        if (id === null || id === undefined) return [null, false]
        return [String(id), await checkPrepared(id)]
      })
    )
    const next = {}
    for (const [key, ok] of pairs) if (key !== null) next[key] = ok
    preparedMap.value = next
  } catch (err) {
    console.error('[SearchView] load prepared states failed:', err)
    preparedMap.value = {}
  }
}

function onPreparationChange({ id, prepared }) {
  if (id === null || id === undefined) return
  preparedMap.value = { ...preparedMap.value, [String(id)]: prepared }
}

/* ---------------- 交互 ---------------- */
/** 提交搜索：只改 URL，检索由 watch 统一执行（状态唯一来源） */
function submitSearch(rawValue) {
  const q = String(rawValue ?? keyword.value ?? '').trim()
  if (!q) {
    keyword.value = ''
    router.push({ path: '/search' })
    return
  }
  if (q === query.value) {
    runSearch(q) // URL 未变，watch 不触发，手动重放一次
    return
  }
  router.push({ path: '/search', query: { q } })
}

function clearKeyword() {
  keyword.value = ''
  router.push({ path: '/search' })
}

function pickKeyword(word) {
  keyword.value = word
  submitSearch(word)
}

/* ---------------- 数据入口 ---------------- */
watch(
  query,
  (q) => {
    if (unmounted) return
    runSearch(q)
  },
  { immediate: true }
)
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto w-[92%] max-w-[1160px] pt-28 pb-24 sm:pt-32">
      <!-- 页头 + 搜索框 -->
      <section class="mb-10">
        <p class="text-xs font-bold tracking-[0.18em] text-blue-600 dark:text-blue-400">SEARCH</p>
        <h1 class="mt-2 text-3xl font-extrabold tracking-tight text-slate-900 dark:text-slate-100 sm:text-4xl">
          搜索竞赛
        </h1>
        <p class="mt-2 text-[15px] font-medium text-slate-500 dark:text-slate-400">
          输入赛事名称、学科方向，或像聊天一样描述你的需求
        </p>

        <div
          class="mt-6 flex items-center gap-3 rounded-full bg-white px-6 py-3 shadow-lg shadow-slate-900/5
                 ring-1 ring-slate-200 transition-shadow focus-within:shadow-xl focus-within:ring-blue-300/70
                 dark:bg-slate-800 dark:ring-slate-700"
        >
          <svg class="h-5 w-5 shrink-0 text-slate-400 dark:text-slate-500" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0z" />
          </svg>

          <input
            v-model="keyword"
            type="text"
            placeholder="搜索赛事，例如：机器人、数学建模、寒假能准备的算法赛"
            class="w-full bg-transparent text-[16px] font-medium text-slate-800 placeholder:text-slate-400
                   focus:outline-none dark:text-slate-100 dark:placeholder:text-slate-500"
            @keyup.enter="submitSearch()"
          />

          <button
            v-if="keyword"
            type="button"
            aria-label="清空搜索"
            class="shrink-0 rounded-full p-1.5 text-slate-400 transition-colors hover:bg-slate-100
                   hover:text-slate-600 dark:hover:bg-slate-700/60 dark:hover:text-slate-300"
            @click="clearKeyword"
          >
            <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>

          <button
            class="shrink-0 rounded-full bg-blue-600 px-6 py-2.5 text-sm font-bold text-white
                   shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700"
            @click="submitSearch()"
          >
            搜索
          </button>
        </div>

        <!-- 未输入时给几个可点的方向，避免空白页 -->
        <div v-if="!query" class="mt-4 flex flex-wrap items-center gap-2">
          <span class="text-xs font-bold text-slate-400 dark:text-slate-500">试试</span>
          <button
            v-for="word in HOT_KEYWORDS"
            :key="word"
            type="button"
            class="rounded-full bg-white px-3.5 py-1.5 text-[13px] font-semibold text-slate-600
                   ring-1 ring-slate-200 transition-colors hover:bg-blue-50 hover:text-blue-700
                   hover:ring-blue-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700"
            @click="pickKeyword(word)"
          >
            {{ word }}
          </button>
        </div>
      </section>

      <!-- 结果抬头 -->
      <section v-if="query" class="mb-6 flex flex-wrap items-baseline justify-between gap-2">
        <h2 class="text-lg font-extrabold text-slate-900 dark:text-slate-100">
          <template v-if="loading">正在搜索「{{ query }}」…</template>
          <template v-else-if="searched">
            「{{ query }}」的搜索结果
            <span class="ml-1 text-sm font-bold text-slate-400 dark:text-slate-500">{{ results.length }} 项</span>
          </template>
          <template v-else>「{{ query }}」的搜索结果</template>
        </h2>
        <span
          v-if="semantic"
          class="rounded-full bg-blue-50 px-3 py-1 text-[12px] font-bold text-blue-700
                 ring-1 ring-blue-100 dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30"
        >
          AI 语义匹配
        </span>
      </section>

      <!-- 加载骨架 -->
      <div v-if="loading" class="space-y-6">
        <div
          v-for="n in 3"
          :key="n"
          class="flex animate-pulse gap-7 rounded-2xl bg-white p-6 ring-1 ring-slate-200/70 dark:bg-slate-800 dark:ring-slate-700"
        >
          <div class="w-52 shrink-0 rounded-xl bg-slate-200/80 dark:bg-slate-700" style="aspect-ratio: 16 / 9"></div>
          <div class="flex flex-1 flex-col">
            <div class="h-6 w-2/5 rounded bg-slate-200/80 dark:bg-slate-700"></div>
            <div class="mt-4 h-4 w-1/2 rounded bg-slate-200/60 dark:bg-slate-700"></div>
            <div class="mt-2 h-4 w-1/3 rounded bg-slate-200/60 dark:bg-slate-700"></div>
          </div>
        </div>
      </div>

      <!-- 请求失败 -->
      <div
        v-else-if="errorMsg"
        class="rounded-2xl border border-rose-100 bg-rose-50/60 px-6 py-14 text-center dark:border-rose-500/30 dark:bg-rose-500/10"
      >
        <p class="font-bold text-slate-700 dark:text-slate-200">搜索出错了</p>
        <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">{{ errorMsg }}</p>
        <button
          class="mt-6 rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white hover:bg-blue-700"
          @click="runSearch(query)"
        >
          重试
        </button>
      </div>

      <!-- 空状态：搜不到 -->
      <div
        v-else-if="searched && results.length === 0"
        class="rounded-3xl border border-dashed border-slate-200 bg-white px-8 py-20 text-center
               dark:border-slate-700 dark:bg-slate-800/60"
      >
        <div class="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-slate-100 dark:bg-slate-700/60">
          <svg class="h-8 w-8 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.7">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0z" />
          </svg>
        </div>
        <p class="mt-5 text-lg font-extrabold text-slate-700 dark:text-slate-200">
          没有找到与「{{ query }}」相关的竞赛
        </p>
        <p class="mt-2 text-sm font-medium text-slate-500 dark:text-slate-400">
          换个更宽泛的词，或者点击下面的方向试试
        </p>

        <!-- AI 语义检索：句子式需求的第二入口（游客引导登录，登录后由后端扣配额） -->
        <div class="mt-7 flex flex-col items-center gap-2">
          <button
            type="button"
            class="inline-flex items-center gap-2 rounded-full bg-blue-600 px-6 py-2.5 text-sm font-bold text-white
                   shadow-sm shadow-blue-600/20 transition-colors hover:bg-blue-700
                   disabled:cursor-not-allowed disabled:opacity-60"
            :disabled="semanticBusy"
            @click="runSemantic"
          >
            <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8">
              <path stroke-linecap="round" stroke-linejoin="round"
                    d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6L12 3zM18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2z" />
            </svg>
            <template v-if="semanticBusy">AI 正在理解这句话…</template>
            <template v-else-if="!authState.user">登录后用 AI 语义搜索</template>
            <template v-else>用 AI 理解这句话</template>
          </button>
          <p class="text-[12px] font-medium text-slate-400 dark:text-slate-500">
            <template v-if="!authState.user">这是一句需求描述，关键词匹配不到；登录后可用 AI 按语义找赛事</template>
            <template v-else-if="semanticTried && !semanticError">AI 语义检索消耗 1 次额度</template>
            <template v-else-if="!semanticError">关键词没搜到，试试让 AI 按语义理解</template>
          </p>
          <p v-if="semanticError" class="text-[12px] font-semibold text-rose-500">{{ semanticError }}</p>
        </div>

        <div class="mt-6 flex flex-wrap justify-center gap-2">
          <button
            v-for="word in HOT_KEYWORDS"
            :key="word"
            type="button"
            class="rounded-full bg-slate-50 px-4 py-2 text-[13px] font-semibold text-slate-600
                   ring-1 ring-slate-200 transition-colors hover:bg-blue-50 hover:text-blue-700
                   hover:ring-blue-200 dark:bg-slate-700/60 dark:text-slate-300 dark:ring-slate-600"
            @click="pickKeyword(word)"
          >
            {{ word }}
          </button>
        </div>
      </div>

      <!-- 结果列表 -->
      <div v-else-if="results.length" class="space-y-6">
        <CompetitionCard
          v-for="item in results"
          :key="item.id"
          :id="item.id"
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
    </main>

    <BackToTop />
  </div>
</template>
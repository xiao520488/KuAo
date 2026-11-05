<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import Header from '@/components/Header.vue'
import CompetitionCard from '@/components/CompetitionCard.vue'
import BackToTop from '@/components/BackToTop.vue'
import { getCompetitions, checkPrepared } from '@/services/api'
import { authState } from '@/services/auth'

const competitions = ref([])
const loading = ref(true)
const errorMsg = ref('')
// 竞赛 ID -> 是否已加入备赛（登录后批量拉取）
const preparedMap = ref({})

// 并发查询每张卡片的备赛状态；未登录直接跳过（全部视为未加入）
async function loadPreparedStates(list) {
  if (!authState.user || !Array.isArray(list) || list.length === 0) {
    preparedMap.value = {}
    return
  }
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
}

// ===== 筛选栏（当前为静态 UI，先把交互状态搭好，后端筛选参数后续接入） =====
const subjects = ['全部科目', '工科', '理科', '文体', '商科', '综合', '计算机类', '创新创业']
const levels = ['全部级别', '国家级', '省级', '市级']

const subject = ref('全部科目')
const level = ref('全部级别')

// 真正的筛选逻辑先保留骨架：目前只做展示，不需要筛选时直接返回全量
const CATEGORY_KEYWORDS = {
  工科: ['工科', '工程', '机械', '电子', '电气', '土木', '建筑', '材料', '自动化', '能源'],
  理科: ['理科', '数学', '物理', '化学', '生物', '统计', '科学'],
  文体: ['文体', '文艺', '文化', '体育', '演讲', '辩论', '书法', '设计', '艺术'],
  商科: ['商科', '商业', '经济', '金融', '会计', '管理', '营销', '创业计划'],
  综合: ['综合', '全能', '通识', '跨学科'],
  计算机类: ['计算机', '编程', '算法', '软件', '人工智能', 'AI', '网络安全', '数据', '信息'],
  创新创业: ['创新', '创业', '创造', '挑战杯', '互联网+'],
}
const matchCategory = (item, category) => {
  if (category === '全部科目') return true
  const keywords = CATEGORY_KEYWORDS[category] || []
  const haystack = `${item.category || ''} ${item.subject || ''} ${item.title || ''}`
  return keywords.some((kw) => haystack.includes(kw))
}
const filtered = computed(() => {
  return competitions.value.filter((item) => {
    const subjectOk = matchCategory(item, subject.value)
    const levelOk = level.value === '全部级别' || item.level === level.value
    return subjectOk && levelOk
  })
})

// ---------- 数据格式化（与首页保持一致） ----------
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

const LEVEL_FALLBACK = {
  national: '国家级',
  provincial: '省级',
  municipal: '市级',
  regional: '区域级',
  international: '国际级'
}

function mapToCard(item, index) {
  return {
    id: item.id ?? index,
    title: item.name || '未命名竞赛',
    level: item.level_label || LEVEL_FALLBACK[item.level] || item.level || '',
    category: item.category || item.subject || '',
    image: item.image ?? item.cover ?? '',
    signUpTime: formatSignUp(item),
    contestTime: '待定'
  }
}

async function loadCompetitions() {
  loading.value = true
  errorMsg.value = ''
  try {
    const list = await getCompetitions()
    competitions.value = list.map(mapToCard)
    // 状态查询失败不影响列表展示
    loadPreparedStates(competitions.value)
  } catch (err) {
    errorMsg.value = err?.message || '加载竞赛列表失败'
    competitions.value = []
  } finally {
    loading.value = false
  }
}

// 登录 / 退出后重新计算备赛状态
watch(
  () => authState.user,
  () => loadPreparedStates(competitions.value)
)

// 卡片内切换备赛后同步本地状态，避免重新请求
function onPreparationChange({ id, prepared }) {
  if (id === null || id === undefined) return
  preparedMap.value = { ...preparedMap.value, [String(id)]: prepared }
}

onMounted(loadCompetitions)
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto w-[85%] max-w-6xl pb-24 pt-28">
      <!-- 页头 -->
      <div class="mb-8">
        <h1 class="text-3xl font-black tracking-tight text-slate-900 dark:text-slate-100">全部竞赛</h1>
        <p class="mt-2 text-sm text-slate-500 dark:text-slate-400">
          共 {{ competitions.length }} 项竞赛，按科目与级别快速定位适合你的赛事
        </p>
      </div>

      <!-- ========== 顶部筛选栏 ========== -->
      <section
        class="mb-6 flex flex-col items-stretch gap-3 rounded-2xl bg-white dark:bg-slate-800 p-4
               sm:flex-row sm:flex-wrap sm:items-center ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm"
      >
        <!-- 科目 -->
        <label class="flex flex-1 items-center gap-2 sm:flex-none">
          <span class="shrink-0 text-sm font-medium text-slate-400">科目</span>
          <select
            v-model="subject"
            class="w-full min-w-0 flex-1 rounded-xl border-0 bg-slate-100 dark:bg-slate-700/60 px-4 py-2 text-sm font-semibold
                   text-slate-700 dark:text-slate-200 outline-none ring-1 ring-transparent
                   transition focus:bg-white dark:focus:bg-slate-800 focus:ring-blue-500 sm:w-auto sm:flex-none"
          >
            <option v-for="s in subjects" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>

        <!-- 级别 -->
        <label class="flex flex-1 items-center gap-2 sm:flex-none">
          <span class="shrink-0 text-sm font-medium text-slate-400">筛选</span>
          <select
            v-model="level"
            class="w-full min-w-0 flex-1 rounded-xl border-0 bg-slate-100 dark:bg-slate-700/60 px-4 py-2 text-sm font-semibold
                   text-slate-700 dark:text-slate-200 outline-none ring-1 ring-transparent
                   transition focus:bg-white dark:focus:bg-slate-800 focus:ring-blue-500 sm:w-auto sm:flex-none"
          >
            <option v-for="l in levels" :key="l" :value="l">{{ l }}</option>
          </select>
        </label>

        <!-- 结果计数 -->
        <span class="ml-auto hidden text-sm text-slate-400 sm:inline">
          {{ filtered.length }} 个结果
        </span>
      </section>

      <!-- ========== 列表三态 ========== -->
      <!-- Loading -->
      <div v-if="loading" class="space-y-6">
        <div
          v-for="n in 4"
          :key="n"
          class="flex animate-pulse gap-7 rounded-2xl bg-white dark:bg-slate-800 p-6 ring-1 ring-slate-200/70 dark:ring-slate-700"
        >
          <div class="w-52 shrink-0">
            <div class="aspect-[4/3] w-full rounded-xl bg-slate-100 dark:bg-slate-700/60"></div>
          </div>
          <div class="flex-1 space-y-4 py-1">
            <div class="h-6 w-1/2 rounded bg-slate-100 dark:bg-slate-700/60"></div>
            <div class="h-4 w-2/3 rounded bg-slate-100 dark:bg-slate-700/60"></div>
            <div class="h-4 w-1/3 rounded bg-slate-100 dark:bg-slate-700/60"></div>
          </div>
        </div>
      </div>

      <!-- 失败 -->
      <div
        v-else-if="errorMsg"
        class="rounded-2xl border border-red-100 dark:border-red-500/30 bg-red-50 dark:bg-red-500/15 px-6 py-16 text-center"
      >
        <p class="font-bold text-red-500 dark:text-red-400">{{ errorMsg }}</p>
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
        v-else-if="filtered.length === 0"
        class="rounded-2xl border border-dashed border-slate-200 dark:border-slate-700 px-6 py-16 text-center"
      >
        <p class="font-bold text-slate-500 dark:text-slate-400">没有符合条件的竞赛</p>
        <p class="mt-1 text-sm text-slate-400">试试切换其他科目或级别</p>
      </div>

      <!-- 正常列表 -->
      <div v-else class="space-y-6">
        <CompetitionCard
          v-for="item in filtered"
          :key="item.id"
          :id="item.id"
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
    </main>

    <!-- 全局回到顶部 -->
    <BackToTop />
  </div>
</template>

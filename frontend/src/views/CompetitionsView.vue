<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import Header from '@/components/Header.vue'
import CompetitionCard from '@/components/CompetitionCard.vue'
import BackToTop from '@/components/BackToTop.vue'
import { getCompetitions, checkPrepared } from '@/services/api'
import { authState } from '@/services/auth'
import { theme } from '@/composables/useTheme'
import { MAJOR_ORDER, CATEGORY_TREE, resolveCategory } from '@/utils/competitionCategory'

const competitions = ref([])
const loading = ref(true)
const errorMsg = ref('')
// 竞赛 ID -> 是否已加入备赛（登录后批量拉取）
const preparedMap = ref({})

const isOfficial = computed(() => theme.value === 'official')

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

/* ================= 左侧筛选竖栏 ================= */
// 平台只收录教育部 84 项核心国赛，「赛事级别」不再区分，只按学科细分方向筛选。
// 大类（工科 / 理科…）本身不参与筛选，只负责展开自己的细分方向；
// 同一时刻只展开一个大类，切换大类会清空上一个大类已选的细分。
const activeMajor = ref('')
const selectedMinors = ref([])

const minorOptions = computed(() => CATEGORY_TREE[activeMajor.value] || [])
const selectedMinorCount = computed(() => selectedMinors.value.length)

function isMinorSelected(minor) {
  return selectedMinors.value.includes(minor)
}

// 点大类 = 展开 / 收起细分方向，不会把「整个工科」加进筛选
function toggleMajorOpen(major) {
  if (activeMajor.value === major) {
    activeMajor.value = ''
    return
  }
  // 换大类时清掉上一个大的细分选择，避免筛到当前展开列表里看不见的方向
  if (selectedMinors.value.length) selectedMinors.value = []
  activeMajor.value = major
}

// 细分方向 toggle：有选 = 只看这些方向，全取消 = 回到全部
function toggleMinor(minor) {
  const index = selectedMinors.value.indexOf(minor)
  if (index >= 0) {
    selectedMinors.value.splice(index, 1)
  } else {
    selectedMinors.value.push(minor)
  }
}

function removeMinor(minor) {
  selectedMinors.value = selectedMinors.value.filter((item) => item !== minor)
}

function clearMajors() {
  selectedMinors.value = []
  activeMajor.value = ''
}

// ---------- 报名状态 ----------
const STATUS_ORDER = ['open', 'upcoming', 'closed', 'unknown']
const STATUS_LABEL = {
  open: '报名中',
  upcoming: '未开始',
  closed: '已截止',
  unknown: '待定'
}
const activeStatus = ref('all')

// 用日期轨判定报名状态，与卡片上的倒计时徽章同一套规则
function statusOf(item) {
  const st = countdownState({
    reg_start_date: item.regStartDate,
    reg_end_date: item.regEndDate,
    contest_start_date: item.contestStartDate
  })
  if (!st) return 'unknown'
  if (st.tone === 'over' || st.tone === 'contest') return 'closed'
  if (st.tone === 'soon') return 'upcoming'
  return 'open'
}

// 学科过滤后的集合：状态计数基于它，保证「选中某方向后，报名中 X 项」是这一方向内的数量
const majorFiltered = computed(() => {
  const base = competitions.value
  if (selectedMinors.value.length === 0) return base
  return base.filter((item) => selectedMinors.value.includes(item.minor))
})

const filtered = computed(() => {
  if (activeStatus.value === 'all') return majorFiltered.value
  return majorFiltered.value.filter((item) => statusOf(item) === activeStatus.value)
})

const statusCounts = computed(() => {
  const counts = { all: majorFiltered.value.length, open: 0, upcoming: 0, closed: 0, unknown: 0 }
  for (const item of majorFiltered.value) counts[statusOf(item)] += 1
  return counts
})

// 当前报名状态过滤下的总池（不受学科选择影响），「全部学科」行显示它
const statusPoolTotal = computed(
  () => competitions.value.filter((item) => activeStatus.value === 'all' || statusOf(item) === activeStatus.value).length
)

// 状态过滤后的池子：大类 / 细分右侧的数字都基于它
const statusPool = computed(() =>
  competitions.value.filter((item) => activeStatus.value === 'all' || statusOf(item) === activeStatus.value)
)

// 每个大类在当前状态过滤下还有多少项
const majorCounts = computed(() => {
  const counts = {}
  for (const major of MAJOR_ORDER) counts[major] = 0
  for (const item of statusPool.value) {
    if (counts[item.major] !== undefined) counts[item.major] += 1
  }
  return counts
})

// 每个细分方向在当前状态过滤下还有多少项
const minorCounts = computed(() => {
  const counts = {}
  for (const item of statusPool.value) {
    if (!item.minor) continue
    counts[item.minor] = (counts[item.minor] || 0) + 1
  }
  return counts
})

function clearAll() {
  clearMajors()
  activeStatus.value = 'all'
}

const hasFilter = computed(
  () => selectedMinors.value.length > 0 || activeStatus.value !== 'all'
)
// ---------- 数据格式化（统一走 utils/timeFormat） ----------
import { formatSignUp, formatContest, countdownState } from '../utils/timeFormat'

// 封面图：后端 API 的 image（由 DB 的 image_url 映射而来）
// 只接受 http(s) 绝对地址，避免相对路径 / 协议相对地址 / 空串打坏卡片占位图
function resolveImage(item) {
  const raw = item.image ?? item.cover ?? item.image_url ?? ''
  const url = typeof raw === 'string' ? raw.trim() : ''
  return /^https?:\/\//i.test(url) ? url : ''
}

function mapToCard(item, index) {
  const rawCategory = item.category || item.subject || ''
  const mapped = resolveCategory(rawCategory)
  return {
    id: item.id ?? index,
    title: item.name || '未命名竞赛',
    category: rawCategory,
    major: mapped.major,
    minor: mapped.minor,
    image: resolveImage(item),
    shortName: item.short_name || '',
    signUpTime: formatSignUp(item),
    contestTime: formatContest(item),
    // 日期轨原始值：只喂给倒计时徽章，缺省时徽章自动不渲染
    regStartDate: item.reg_start_date || '',
    regEndDate: item.reg_end_date || '',
    contestStartDate: item.contest_start_date || ''
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

onMounted(() => {
  loadCompetitions()
})
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto w-[92%] max-w-[1248px] pb-24 pt-28">
      <!-- 页头 -->
      <div class="mb-8">
        <p
          class="mb-3 text-[12px] font-bold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500"
        >
          <span v-if="isOfficial" class="mr-2 inline-block h-[1px] w-5 translate-y-[-4px] bg-[#12294a]"></span>
          竞赛总览 · COMPETITION INDEX
        </p>
        <h1 class="text-3xl font-black tracking-tight text-slate-900 dark:text-slate-100">全部竞赛</h1>
        <p class="mt-2 text-sm text-slate-500 dark:text-slate-400">
          共 {{ competitions.length }} 项竞赛，按报名紧迫度排序：正在报名 → 即将开始 → 已截止
        </p>
      </div>

      <!-- ========== 移动端筛选（竖栏在 lg 以下隐藏） ========== -->
      <div class="mb-6 space-y-3 lg:hidden">
        <div class="flex flex-wrap gap-2">
          <button
            type="button"
            class="shrink-0 rounded-full px-3.5 py-2 text-sm font-semibold transition-colors"
            :class="selectedMinors.length === 0
              ? 'bg-slate-900 text-white dark:bg-white dark:text-slate-900'
              : 'bg-white text-slate-600 ring-1 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700'"
            @click="clearMajors"
          >
            全部学科
          </button>
          <button
            v-for="major in MAJOR_ORDER"
            :key="major"
            type="button"
            class="shrink-0 rounded-full px-3.5 py-2 text-sm font-semibold transition-colors"
            :class="activeMajor === major
              ? 'bg-slate-900 text-white dark:bg-white dark:text-slate-900'
              : 'bg-white text-slate-600 ring-1 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700'"
            @click="toggleMajorOpen(major)"
          >
            {{ major }}<span v-if="activeMajor === major" class="ml-1 opacity-70">▾</span>
          </button>
        </div>
        <div v-if="activeMajor" class="flex flex-wrap gap-2">
          <button
            v-for="minor in minorOptions"
            :key="minor"
            type="button"
            class="shrink-0 rounded-full px-3 py-1.5 text-xs font-bold transition-colors"
            :class="isMinorSelected(minor)
              ? 'bg-blue-600 text-white'
              : 'bg-white text-slate-600 ring-1 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700'"
            @click="toggleMinor(minor)"
          >
            {{ minor }} <span class="tabular-nums opacity-60">{{ minorCounts[minor] || 0 }}</span>
          </button>
        </div>
        <div class="flex flex-wrap gap-2">
          <button
            type="button"
            class="shrink-0 rounded-full px-3 py-1.5 text-xs font-bold transition-colors"
            :class="activeStatus === 'all'
              ? 'bg-slate-900 text-white dark:bg-white dark:text-slate-900'
              : 'bg-white text-slate-600 ring-1 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700'"
            @click="activeStatus = 'all'"
          >
            全部状态
          </button>
          <button
            v-for="st in STATUS_ORDER"
            :key="st"
            type="button"
            class="shrink-0 rounded-full px-3 py-1.5 text-xs font-bold transition-colors"
            :class="activeStatus === st
              ? 'bg-slate-900 text-white dark:bg-white dark:text-slate-900'
              : 'bg-white text-slate-600 ring-1 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700'"
            @click="activeStatus = st"
          >
            {{ STATUS_LABEL[st] }} {{ statusCounts[st] }}
          </button>
        </div>
      </div>
      <!-- ========== 主体：左筛选竖栏 + 右列表 ========== -->
      <div class="lg:grid lg:grid-cols-[248px_minmax(0,1fr)] lg:gap-10 xl:gap-14">
        <!-- 左竖栏 -->
        <aside class="hidden lg:block lg:border-r lg:border-slate-200/80 lg:pr-7 dark:lg:border-slate-700/60">
          <div class="sticky top-24">
            <!-- 01 学科门类 -->
            <section class="border-t border-slate-300/80 pt-4 dark:border-slate-700">
              <div class="mb-3 flex items-baseline justify-between">
                <h2 class="text-[12px] font-bold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                  <span class="mr-1.5 font-mono text-slate-300 dark:text-slate-600">01</span>学科门类
                </h2>
                <button
                  v-if="selectedMinors.length"
                  type="button"
                  class="text-[11px] font-bold text-slate-400 transition-colors hover:text-slate-700 dark:hover:text-slate-200"
                  @click="clearMajors"
                >
                  重置
                </button>
              </div>

              <button
                type="button"
                class="mb-1 flex w-full items-center justify-between rounded-md px-2.5 py-[7px] text-left text-[14px] font-semibold transition-colors"
                :class="selectedMinors.length === 0
                  ? (isOfficial ? 'bg-[#12294a] text-white' : 'bg-blue-600 text-white')
                  : 'text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800'"
                @click="clearMajors"
              >
                <span>全部学科</span>
                <span class="text-xs tabular-nums opacity-60">{{ statusPoolTotal }}</span>
              </button>

              <div v-for="major in MAJOR_ORDER" :key="major" class="mb-0.5">
                <!-- 大类只是「展开细分方向」的开关，点它不会把整个大类塞进筛选 -->
                <button
                  type="button"
                  class="flex w-full items-center gap-1.5 rounded-md px-2.5 py-[7px] text-left text-[14px] font-semibold transition-colors"
                  :class="activeMajor === major
                    ? 'bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-white'
                    : 'text-slate-700 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-800'"
                  :aria-expanded="activeMajor === major"
                  :aria-label="`${activeMajor === major ? '收起' : '展开'}${major}细分方向`"
                  @click="toggleMajorOpen(major)"
                >
                  <span class="truncate">{{ major }}</span>
                  <span
                    class="ml-auto shrink-0 text-xs tabular-nums"
                    :class="activeMajor === major && selectedMinorCount
                      ? (isOfficial ? 'font-bold text-[#12294a] dark:text-white' : 'font-bold text-blue-600 dark:text-blue-300')
                      : 'opacity-50'"
                  >
                    <template v-if="activeMajor === major && selectedMinorCount">{{ selectedMinorCount }}/{{ majorCounts[major] }}</template>
                    <template v-else>{{ majorCounts[major] }}</template>
                  </span>
                  <svg
                    class="h-3 w-3 shrink-0 opacity-50 transition-transform"
                    :class="activeMajor === major ? 'rotate-180' : ''"
                    fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"
                  >
                    <path stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6" />
                  </svg>
                </button>

                <!-- 细分方向：点哪个就只看哪个，可多选 -->
                <div
                  v-if="activeMajor === major"
                  class="mb-1 mt-1.5 grid grid-cols-2 gap-1 border-l border-slate-200 pl-2.5 dark:border-slate-700"
                >
                  <button
                    v-for="minor in minorOptions"
                    :key="minor"
                    type="button"
                    class="flex w-full items-center justify-between gap-1 rounded px-1.5 py-1 text-left text-[12px] font-semibold transition-colors"
                    :class="isMinorSelected(minor)
                      ? (isOfficial
                        ? 'bg-[#12294a] text-white'
                        : 'bg-blue-600 text-white')
                      : (minorCounts[minor] || 0) === 0
                        ? 'cursor-not-allowed bg-slate-50 text-slate-300 dark:bg-slate-800/40 dark:text-slate-600'
                        : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700'"
                    :disabled="(minorCounts[minor] || 0) === 0"
                    @click="toggleMinor(minor)"
                  >
                    <span class="max-w-[132px] truncate" :title="minor">{{ minor }}</span>
                    <span class="text-[10px] tabular-nums opacity-50">{{ minorCounts[minor] || 0 }}</span>
                  </button>
                </div>
              </div>
            </section>

            <!-- 02 报名状态 -->
            <section class="mt-7 border-t border-slate-300/80 pt-4 dark:border-slate-700">
              <h2 class="mb-3 text-[12px] font-bold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                <span class="mr-1.5 font-mono text-slate-300 dark:text-slate-600">02</span>报名状态
              </h2>
              <button
                type="button"
                class="mb-0.5 flex w-full items-center justify-between rounded-md px-2.5 py-[7px] text-left text-[14px] font-semibold transition-colors"
                :class="activeStatus === 'all'
                  ? 'text-slate-900 dark:text-white'
                  : 'text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800'"
                @click="activeStatus = 'all'"
              >
                <span class="flex items-center gap-2">
                  <span
                    class="h-[7px] w-[7px] rounded-full"
                    :class="activeStatus === 'all' ? (isOfficial ? 'bg-[#12294a] dark:bg-white' : 'bg-blue-600') : 'bg-slate-300 dark:bg-slate-600'"
                  ></span>
                  全部状态
                </span>
                <span class="text-xs tabular-nums text-slate-400">{{ statusCounts.all }}</span>
              </button>
              <button
                v-for="st in STATUS_ORDER"
                :key="st"
                type="button"
                class="flex w-full items-center justify-between rounded-md px-2.5 py-[7px] text-left text-[14px] font-semibold transition-colors"
                :class="activeStatus === st
                  ? 'text-slate-900 dark:text-white'
                  : 'text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800'"
                @click="activeStatus = st"
              >
                <span class="flex items-center gap-2">
                  <span
                    class="h-[7px] w-[7px] rounded-full"
                    :class="activeStatus === st ? (isOfficial ? 'bg-[#12294a] dark:bg-white' : 'bg-blue-600') : 'bg-slate-300 dark:bg-slate-600'"
                  ></span>
                  {{ STATUS_LABEL[st] }}
                </span>
                <span class="text-xs tabular-nums text-slate-400">{{ statusCounts[st] }}</span>
              </button>
            </section>

            <!-- 结果 + 清空 -->
            <div class="mt-7 border-t border-slate-300/80 pt-4 dark:border-slate-700">
              <p class="text-[12.5px] font-medium text-slate-500 dark:text-slate-400">
                匹配 <span class="text-[22px] font-black tabular-nums text-slate-900 dark:text-white">{{ filtered.length }}</span> 项
              </p>
              <button
                v-if="hasFilter"
                type="button"
                class="mt-2 text-[12.5px] font-bold text-slate-500 underline decoration-slate-300 underline-offset-4 transition-colors hover:text-slate-900 dark:text-slate-400 dark:hover:text-white"
                @click="clearAll"
              >
                清空全部筛选
              </button>

              <!-- 已选条件 -->
              <div v-if="selectedMinors.length" class="mt-4 flex flex-wrap gap-1.5">
                <button
                  v-for="selection in selectedMinors"
                  :key="selection"
                  type="button"
                  class="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-1 text-[11.5px] font-semibold text-slate-600 transition-colors hover:border-slate-300 hover:text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:text-white"
                  @click="removeMinor(selection)"
                >
                  {{ selection }}
                  <svg class="h-2.5 w-2.5 opacity-50" fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </aside>

        <!-- 右列表 -->
        <div class="min-w-0">
          <!-- Loading -->
          <div v-if="loading" class="space-y-6">
            <div
              v-for="n in 4"
              :key="n"
              class="flex animate-pulse gap-7 rounded-2xl bg-white p-6 ring-1 ring-slate-200/70 dark:bg-slate-800 dark:ring-slate-700"
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
            class="rounded-2xl border border-red-100 bg-red-50 px-6 py-16 text-center dark:border-red-500/30 dark:bg-red-500/15"
          >
            <p class="font-bold text-red-500 dark:text-red-400">{{ errorMsg }}</p>
            <button
              class="mt-6 rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600"
              @click="loadCompetitions"
            >
              重新加载
            </button>
          </div>

          <!-- 筛选后无结果 -->
          <div
            v-else-if="filtered.length === 0"
            class="rounded-2xl border border-dashed border-slate-200 px-6 py-16 text-center dark:border-slate-700"
          >
            <p class="font-bold text-slate-500 dark:text-slate-400">没有符合条件的竞赛</p>
            <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">试试放宽学科方向或报名状态</p>
            <button
              v-if="hasFilter"
              class="mt-5 rounded-full bg-slate-900 px-5 py-2 text-sm font-bold text-white dark:bg-white dark:text-slate-900"
              @click="clearAll"
            >
              清空筛选
            </button>
          </div>

          <!-- 正常列表 -->
          <div v-else class="space-y-6">
            <CompetitionCard
              v-for="item in filtered"
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
        </div>
      </div>
    </main>

    <!-- 全局回到顶部 -->
    <BackToTop />
  </div>
</template>
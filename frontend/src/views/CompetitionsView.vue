<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import Header from '@/components/Header.vue'
import CompetitionCard from '@/components/CompetitionCard.vue'
import BackToTop from '@/components/BackToTop.vue'
import { getCompetitions, checkPrepared } from '@/services/api'
import { authState } from '@/services/auth'
import { MAJOR_ORDER, CATEGORY_TREE, resolveCategory } from '@/utils/competitionCategory'

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

// ===== 筛选栏 =====
// 第一级大类严格限定为四类，第二级细分方向来自统一分类字典
const levels = ['全部级别', '国家级', '省级']

const level = ref('全部级别')

// 科目 Mega Menu 状态
const menuOpen = ref(false)
const activeMajor = ref('')
// 科目多选：数组里可同时存大类（工科）与细分（计算机&信息技术）
// 数组为空 = 展示全部竞赛；有值 = 按数组里的分类做 OR 过滤
const selectedMajors = ref([])
const subjectMenuRef = ref(null)
// 桌面端悬停展开；触屏设备没有 Hover，改为点击展开
const canHover = ref(false)

const LEVEL_FILTER_MAP = {
  国家级: 'national',
  省级: 'provincial'
}

function openMenu(major) {
  activeMajor.value = major
  menuOpen.value = true
}

function closeMenu() {
  menuOpen.value = false
}

function onMajorHover(major) {
  if (canHover.value) openMenu(major)
}

// 桌面端悬停已能打开菜单，点击只负责切换选中状态；触屏端点击同样展开菜单
function onMajorClick(major) {
  toggleSelection(major)
  openMenu(major)
}

function onMenuAreaLeave() {
  if (canHover.value) closeMenu()
}

function isSelected(value) {
  return selectedMajors.value.includes(value)
}

// 大类 / 细分统一 toggle：选中则加入，已选中则移除
function toggleSelection(value) {
  const index = selectedMajors.value.indexOf(value)
  if (index >= 0) {
    selectedMajors.value.splice(index, 1)
  } else {
    selectedMajors.value.push(value)
  }
}

// 点击胶囊上的 X：只移除对应的那一项
function removeSelection(value) {
  selectedMajors.value = selectedMajors.value.filter((item) => item !== value)
}

// 只要该大类本身或其任一细分被选中，就高亮大类按钮
function isMajorActive(major) {
  if (isSelected(major)) return true
  return CATEGORY_TREE[major].some((minor) => isSelected(minor))
}

// 独立的“全部”按钮：清空科目数组，列表自然恢复全量
function selectAll() {
  selectedMajors.value = []
  closeMenu()
}

// 点击菜单外区域自动收起
function onDocumentPointerDown(e) {
  if (!menuOpen.value) return
  const root = subjectMenuRef.value
  if (root && !root.contains(e.target)) closeMenu()
}

const filtered = computed(() => {
  const base = competitions.value.filter((item) => {
    // 市级数据不再纳入列表（与级别筛选保持一致）
    if (item.levelRaw === 'municipal' || item.levelRaw === '市级' || item.level === '市级') return false

    // 级别：优先用原始枚举匹配，兼容 level_label 为“区域级/区域赛”等叫法
    const levelOk =
      level.value === '全部级别' ||
      item.levelRaw === LEVEL_FILTER_MAP[level.value] ||
      item.level === level.value
    return levelOk
  })

  // 数组为空（或点了“全部”）时展示全部；否则按多选分类做 OR 过滤
  if (selectedMajors.value.length === 0) return base

  return base.filter((item) =>
    selectedMajors.value.some((value) => item.major === value || item.minor === value)
  )
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
  regional: '区域级',
  international: '国际级'
}

function mapToCard(item, index) {
  const rawCategory = item.category || item.subject || ''
  const mapped = resolveCategory(rawCategory)
  return {
    id: item.id ?? index,
    title: item.name || '未命名竞赛',
    level: item.level_label || LEVEL_FALLBACK[item.level] || item.level || '',
    levelRaw: item.level || '',
    category: rawCategory,
    major: mapped.major,
    minor: mapped.minor,
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

onMounted(() => {
  canHover.value =
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia('(hover: hover)').matches
      : false
  document.addEventListener('pointerdown', onDocumentPointerDown)
  loadCompetitions()
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown)
})
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
        class="mb-6 rounded-2xl bg-white dark:bg-slate-800 p-4
               ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm"
      >
        <div class="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
          <!-- 科目：一级大类 + Hover/点击展开的二级 Mega Menu -->
          <div
            ref="subjectMenuRef"
            class="relative flex min-w-0 flex-1 flex-wrap items-center gap-2"
            @mouseleave="onMenuAreaLeave"
          >
            <span class="shrink-0 text-sm font-medium text-slate-400">科目</span>

            <button
              type="button"
              class="rounded-full px-3.5 py-2 text-sm font-semibold transition-colors"
              :class="selectedMajors.length === 0
                ? 'bg-slate-900 text-white dark:bg-white dark:text-slate-900'
                : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-700/60 dark:text-slate-300 dark:hover:bg-slate-700'"
              @click="selectAll"
            >
              全部
            </button>

            <button
              v-for="major in MAJOR_ORDER"
              :key="major"
              type="button"
              class="inline-flex items-center gap-1 rounded-full px-3.5 py-2 text-sm font-semibold transition-colors"
              :class="[
                isMajorActive(major)
                  ? 'bg-blue-600 text-white shadow-sm shadow-blue-600/25'
                  : 'bg-slate-100 text-slate-600 hover:bg-blue-50 hover:text-blue-700 dark:bg-slate-700/60 dark:text-slate-300 dark:hover:bg-slate-700 dark:hover:text-white',
                menuOpen && activeMajor === major ? 'ring-2 ring-blue-500/60' : ''
              ]"
              :aria-expanded="menuOpen && activeMajor === major"
              :aria-pressed="isSelected(major)"
              aria-haspopup="true"
              @mouseenter="onMajorHover(major)"
              @click="onMajorClick(major)"
            >
              {{ major }}
              <svg
                class="h-3 w-3 transition-transform"
                :class="menuOpen && activeMajor === major ? 'rotate-180' : ''"
                fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"
              >
                <path stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6" />
              </svg>
            </button>

            <!-- 多选结果：v-for 渲染每个选中项，X 只移除对应的一项 -->
            <div v-if="selectedMajors.length" class="flex w-full flex-wrap items-center gap-2">
              <span class="text-xs font-medium text-slate-400">已选</span>
              <span
                v-for="selection in selectedMajors"
                :key="selection"
                class="inline-flex items-center gap-1 rounded-full bg-blue-50 px-3 py-1.5 text-xs font-bold text-blue-700
                       ring-1 ring-blue-200 dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30"
              >
                {{ selection }}
                <button
                  type="button"
                  class="ml-0.5 flex h-4 w-4 items-center justify-center rounded-full transition-colors
                         hover:bg-blue-200/70 dark:hover:bg-blue-500/30"
                  :aria-label="`移除 ${selection}`"
                  @click.stop="removeSelection(selection)"
                >
                  <svg class="h-3 w-3" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </span>
            </div>

            <!-- 二级悬浮菜单（Mega Menu） -->
            <div
              v-if="menuOpen && activeMajor"
              class="absolute left-0 top-full z-40 w-[min(92vw,44rem)] pt-2"
            >
              <div
                class="rounded-2xl border border-slate-200 bg-white p-4 shadow-xl shadow-slate-900/10
                       dark:border-slate-700 dark:bg-slate-800"
              >
                <div class="mb-3 flex items-center justify-between gap-3">
                  <p class="text-xs font-black uppercase tracking-[0.14em] text-slate-400">
                    {{ activeMajor }} · 细分方向
                  </p>
                  <button
                    type="button"
                    class="rounded-full px-2.5 py-1 text-xs font-bold text-slate-400 transition-colors
                           hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-700 dark:hover:text-slate-200"
                    @click="closeMenu"
                  >
                    收起
                  </button>
                </div>

                <div class="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-4">
                  <button
                    type="button"
                    class="rounded-xl px-3 py-2 text-left text-xs font-semibold transition-colors"
                    :class="isSelected(activeMajor)
                      ? 'bg-blue-600 text-white'
                      : 'bg-slate-50 text-slate-600 hover:bg-blue-50 hover:text-blue-700 dark:bg-slate-700/40 dark:text-slate-300 dark:hover:bg-slate-700 dark:hover:text-white'"
                    @click="toggleSelection(activeMajor)"
                  >
                    全部{{ activeMajor }}
                  </button>
                  <button
                    v-for="minor in CATEGORY_TREE[activeMajor]"
                    :key="minor"
                    type="button"
                    class="truncate rounded-xl px-3 py-2 text-left text-xs font-semibold transition-colors"
                    :class="isSelected(minor)
                      ? 'bg-blue-600 text-white'
                      : 'bg-slate-50 text-slate-600 hover:bg-blue-50 hover:text-blue-700 dark:bg-slate-700/40 dark:text-slate-300 dark:hover:bg-slate-700 dark:hover:text-white'"
                    @click="toggleSelection(minor)"
                  >
                    {{ minor }}
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- 级别 -->
          <label class="flex items-center gap-2 sm:flex-none">
            <span class="shrink-0 text-sm font-medium text-slate-400">级别</span>
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
          <span class="text-sm text-slate-400 sm:ml-auto">
            {{ filtered.length }} 个结果
          </span>
        </div>
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

      <!-- 已筛选但无匹配结果 -->
      <div
        v-else-if="filtered.length === 0"
        class="rounded-2xl border border-dashed border-slate-200 dark:border-slate-700 px-6 py-16 text-center"
      >
        <p class="font-bold text-slate-500 dark:text-slate-400">没有符合条件的竞赛</p>
        <p class="mt-1 text-sm text-slate-400">试试更换其他科目或级别</p>
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

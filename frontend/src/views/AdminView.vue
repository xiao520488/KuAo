<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Header from '@/components/Header.vue'
import {
  getAdminCandidates,
  getAdminFeedback,
  getAdminCompetitions,
  getChangelogs,
  createAdminChangelog,
  updateAdminChangelog,
  deleteAdminChangelog,
  getFactQueue,
  fetchFactCandidates,
  applyFactCandidate,
  rejectFactCandidate,
  markFactUnknown,
  saveCompetitionFacts,
  approveAdminCandidate,
  rejectAdminCandidate,
  markAdminFeedbackRead,
  deleteAdminCompetition
} from '@/services/api'
import { showToast } from '@/composables/useToast'

/**
 * 管理后台
 * ------------------------------------------------------------------
 * 布局：左侧侧边栏（候选赛事 / 用户反馈 / 赛事管理 / 更新日志）+ 右侧内容区，
 *      沿用全站的 Tailwind 暗色 / 极简风，不引入任何 UI 组件库。
 *
 * 权限：路由 meta.requiresAdmin 已在 router/index.js 拦截；
 *      后端每个接口还各自挂了 get_current_admin，前端守卫只是体验层，
 *      真正的拦截在服务端（403），这里对 401/403 也做了兜底提示。
 */

/* ---------------------------- 状态 ---------------------------- */

const route = useRoute()
const router = useRouter()

// 当前面板支持 ?tab=feedback 深链，刷新 / 前进后退后停在原面板
const activeTab = ref(
  ['candidates', 'feedback', 'competitions', 'changelogs', 'facts'].includes(route.query.tab)
    ? route.query.tab
    : 'candidates'
)

const candidates = ref([])
const candidatesTotal = ref(0)
const candidatesLoading = ref(false)
const candidatesError = ref('')

const feedbacks = ref([])
const feedbackUnread = ref(0)
const feedbackLoading = ref(false)
const feedbackError = ref('')

// 赛事管理：主库分页列表
const competitions = ref([])
const competitionsTotal = ref(0)
const competitionsLoading = ref(false)
const competitionsError = ref('')
const competitionsPage = ref(0)
const COMPETITIONS_PAGE_SIZE = 10

// 更新日志：发布表单 + 已发布列表
const changelogs = ref([])
const changelogsLoading = ref(false)
const changelogsError = ref('')

// 赛事核对：官网抽取的待确认队列 + 手动校正日期
const factQueue = ref([])
const factLoading = ref(false)
const factError = ref('')
const factFetching = ref(false)
const factBusyId = ref(null)
const factIdsInput = ref('')
const factSourceUrl = ref('')
const factsEditOpen = ref(false)
const factsEditItem = ref(null)
const factsSaving = ref(false)
const factsEditError = ref('')
const factsForm = ref({
  reg_start_date: '',
  reg_end_date: '',
  contest_start_date: '',
  contest_end_date: '',
  source_url: ''
})

/** 今天（本地时区）的 YYYY-MM-DD，作为发布日期默认值 */
function todayISO() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

const publishForm = ref({
  version: '',
  release_date: todayISO(),
  content_markdown: ''
})
const publishing = ref(false)
const publishError = ref('')

// 编辑态：非空表示正在改某条已有日志（表单切换为「保存修改」）
const editingId = ref(null)
const deletingId = ref(null)
const clDeleteOpen = ref(false)
const clDeleteItem = ref(null)

// 删除赛事二次确认：不可恢复，必须显式确认
const deleteOpen = ref(false)
const deleteItem = ref(null)
const deleteBusy = ref(false)

// 正在处理的行（按钮 loading / 防重复点击）
const busyId = ref(null)

// 二次确认弹窗：审核「通过 / 拒绝」会写入主库，必须先确认
const confirmOpen = ref(false)
const confirmAction = ref('') // approve | reject
const confirmItem = ref(null)
const confirmBusy = ref(false)

const confirmTone = computed(() => (confirmAction.value === 'reject' ? 'reject' : 'approve'))
const confirmTitle = computed(() =>
  confirmTone.value === 'reject' ? '确认拒绝这个赛事？' : '确认审核通过这个赛事？'
)
const confirmDesc = computed(() =>
  confirmTone.value === 'reject'
    ? '拒绝后该赛事仅在候选表里标记为已拒绝，不会进入正式赛事列表。'
    : '通过后该赛事会立即写入正式赛事库，前台马上可被搜索到。'
)
const confirmOkText = computed(() =>
  confirmTone.value === 'reject' ? '确认拒绝' : '确认通过'
)

/* ---------------------------- 工具 ---------------------------- */

/** 从后端错误对象里取出可读文案 */
function errorText(err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    return detail.message
  }
  if (typeof err?.message === 'string' && err.message.trim()) return err.message
  return fallback
}

/** '2026-11-09 10:20:31' -> '2026-11-09 10:20'；解析失败则原样返回 */
function formatTime(raw) {
  if (!raw) return '—'
  const text = String(raw).replace('T', ' ').replace('Z', '')
  return text.slice(0, 16)
}

/** 抓取时间只显示到月份 */
function formatMonth(raw) {
  const text = formatTime(raw)
  return text.length >= 7 ? text.slice(0, 7) : text
}

/** 来源标识 -> 中文名（主库存的是 saikr:list / edu:roster 这类带后缀的标识） */
const SOURCE_LABELS = {
  saikr: '赛氪',
  'edu-roster': '教育部门名录',
  manual: '人工录入'
}
function sourceLabel(source) {
  if (!source) return '未知'
  if (SOURCE_LABELS[source]) return SOURCE_LABELS[source]
  if (source.startsWith('saikr')) return '赛氪'
  if (source.startsWith('edu')) return '教育部门名录'
  if (source.startsWith('manual')) return '人工录入'
  return source
}

/* ---------------------------- 数据加载 ---------------------------- */

async function loadCandidates() {
  candidatesLoading.value = true
  candidatesError.value = ''
  try {
    const data = await getAdminCandidates({ limit: 200 })
    candidates.value = data.items
    candidatesTotal.value = data.total
  } catch (err) {
    candidatesError.value = errorText(err, '候选赛事加载失败')
    if (err?.response?.status === 403) candidatesError.value = '当前账号没有管理员权限'
  } finally {
    candidatesLoading.value = false
  }
}

async function loadFeedback() {
  feedbackLoading.value = true
  feedbackError.value = ''
  try {
    const data = await getAdminFeedback({ limit: 200 })
    feedbacks.value = data.items
    feedbackUnread.value = data.unread
  } catch (err) {
    feedbackError.value = errorText(err, '用户反馈加载失败')
    if (err?.response?.status === 403) feedbackError.value = '当前账号没有管理员权限'
  } finally {
    feedbackLoading.value = false
  }
}

async function loadCompetitions() {
  competitionsLoading.value = true
  competitionsError.value = ''
  try {
    const data = await getAdminCompetitions({
      limit: COMPETITIONS_PAGE_SIZE,
      offset: competitionsPage.value * COMPETITIONS_PAGE_SIZE
    })
    competitions.value = data.items
    competitionsTotal.value = data.total
  } catch (err) {
    competitionsError.value = errorText(err, '赛事列表加载失败')
    if (err?.response?.status === 403) competitionsError.value = '当前账号没有管理员权限'
  } finally {
    competitionsLoading.value = false
  }
}

const competitionsPages = computed(() =>
  Math.max(1, Math.ceil(competitionsTotal.value / COMPETITIONS_PAGE_SIZE))
)

/* ---------------------------- 更新日志 ---------------------------- */

async function loadChangelogs() {
  changelogsLoading.value = true
  changelogsError.value = ''
  try {
    const data = await getChangelogs({ limit: 50 })
    changelogs.value = data.items
  } catch (err) {
    changelogsError.value = errorText(err, '更新日志加载失败')
  } finally {
    changelogsLoading.value = false
  }
}

/** 发布 / 保存：校验三项必填后落库，成功后清空表单并刷新右侧列表 */
async function onPublish() {
  if (publishing.value) return

  const version = publishForm.value.version.trim()
  const releaseDate = publishForm.value.release_date.trim()
  const content = publishForm.value.content_markdown.trim()

  if (!version) {
    publishError.value = '请填写版本号'
    return
  }
  if (!releaseDate) {
    publishError.value = '请选择发布日期'
    return
  }
  if (!content) {
    publishError.value = '请填写更新内容'
    return
  }

  publishing.value = true
  publishError.value = ''
  try {
    const payload = {
      version,
      release_date: releaseDate,
      content_markdown: content
    }
    if (editingId.value) {
      await updateAdminChangelog(editingId.value, payload)
      showToast('已保存')
    } else {
      await createAdminChangelog(payload)
      showToast('已发布')
    }
    resetForm()
    await loadChangelogs()
  } catch (err) {
    publishError.value = errorText(err, editingId.value ? '保存失败，请重试' : '发布失败，请重试')
  } finally {
    publishing.value = false
  }
}

/** 清空表单并退出编辑态 */
function resetForm() {
  editingId.value = null
  publishForm.value = {
    version: '',
    release_date: todayISO(),
    content_markdown: ''
  }
  publishError.value = ''
}

/** 进入编辑态：把该条内容回填到表单 */
function startEdit(item) {
  if (publishing.value) return
  editingId.value = item.id
  publishError.value = ''
  publishForm.value = {
    version: item.version || '',
    release_date: item.release_date || todayISO(),
    content_markdown: item.content_markdown || ''
  }
}

function cancelEdit() {
  if (publishing.value) return
  resetForm()
}

/** 打开删除确认（删除不可恢复，先确认） */
function askDeleteChangelog(item) {
  if (deletingId.value) return
  clDeleteItem.value = item
  clDeleteOpen.value = true
}

function cancelDeleteChangelog() {
  if (deletingId.value) return
  clDeleteOpen.value = false
  clDeleteItem.value = null
}

async function confirmDeleteChangelog() {
  const item = clDeleteItem.value
  if (!item || deletingId.value) return

  deletingId.value = item.id
  try {
    await deleteAdminChangelog(item.id)
    showToast('已删除')
    if (editingId.value === item.id) resetForm()
    clDeleteOpen.value = false
    clDeleteItem.value = null
    await loadChangelogs()
  } catch (err) {
    showToast(errorText(err, '删除失败，请重试'))
  } finally {
    deletingId.value = null
  }
}

function goCompetitionsPage(next) {
  const target = Math.min(Math.max(0, next), competitionsPages.value - 1)
  if (target === competitionsPage.value) return
  competitionsPage.value = target
  loadCompetitions()
}

function switchTab(tab) {
  activeTab.value = tab
  // 同步到地址栏（replace，不污染历史栈）
  router.replace({ query: { ...route.query, tab } })
  if (tab === 'candidates' && !candidates.value.length) loadCandidates()
  if (tab === 'feedback' && !feedbacks.value.length) loadFeedback()
  if (tab === 'competitions' && !competitions.value.length) loadCompetitions()
  if (tab === 'changelogs' && !changelogs.value.length) loadChangelogs()
  if (tab === 'facts' && !factQueue.value.length) loadFacts()
}

function refresh() {
  if (activeTab.value === 'candidates') loadCandidates()
  else if (activeTab.value === 'feedback') loadFeedback()
  else if (activeTab.value === 'changelogs') loadChangelogs()
  else if (activeTab.value === 'facts') loadFacts()
  else loadCompetitions()
}

/* ---------------------------- 审核操作 ---------------------------- */
/* 通过 / 拒绝都会写主库，统一走「先弹确认、再调接口」两步。 */

/** 打开确认弹窗（不直接发请求） */
function askAudit(action, item) {
  if (busyId.value || confirmBusy.value) return
  confirmAction.value = action
  confirmItem.value = item
  confirmOpen.value = true
}

function cancelConfirm() {
  if (confirmBusy.value) return
  confirmOpen.value = false
  confirmItem.value = null
}

/** 确认后才真正落库 */
async function confirmAudit() {
  const item = confirmItem.value
  const action = confirmAction.value
  if (!item || confirmBusy.value) return

  confirmBusy.value = true
  busyId.value = item.id
  try {
    if (action === 'approve') {
      // 通过后会同步抓赛氪详情 + AI 提纯，最长约 1 分钟，先给个进度提示
      showToast('审批中：正在补全简称与赛氪详情（约 20~60 秒）…')
      const data = await approveAdminCandidate(item.id)
      showToast(data?.message || '已通过')
    } else {
      await rejectAdminCandidate(item.id)
      showToast('已拒绝')
    }
    candidates.value = candidates.value.filter((row) => row.id !== item.id)
    candidatesTotal.value = Math.max(0, candidatesTotal.value - 1)
    confirmOpen.value = false
    confirmItem.value = null
  } catch (err) {
    showToast(errorText(err, '操作失败，请重试'))
  } finally {
    confirmBusy.value = false
    busyId.value = null
  }
}

/* ---------------------------- 删除赛事 ---------------------------- */
/* 永久删除（连带 awards / ideas / insights / 备赛计划），必须先弹自定义确认框。 */

function askDelete(item) {
  if (busyId.value || deleteBusy.value) return
  deleteItem.value = item
  deleteOpen.value = true
}

function cancelDelete() {
  if (deleteBusy.value) return
  deleteOpen.value = false
  deleteItem.value = null
}

async function confirmDelete() {
  const item = deleteItem.value
  if (!item || deleteBusy.value) return
  deleteBusy.value = true
  busyId.value = item.id
  try {
    const data = await deleteAdminCompetition(item.id)
    showToast(data?.message || `已删除《${item.name}》`)
    deleteOpen.value = false
    deleteItem.value = null
    // 删掉当前页最后一条时回退一页，避免停在空页上
    if (competitions.value.length === 1 && competitionsPage.value > 0) {
      competitionsPage.value -= 1
    }
    await loadCompetitions()
  } catch (err) {
    showToast(errorText(err, '删除失败，请重试'))
  } finally {
    deleteBusy.value = false
    busyId.value = null
  }
}

/** Esc 关闭弹窗，和站内其他弹窗保持一致 */
function onGlobalKeydown(event) {
  if (event.key !== 'Escape') return
  if (deleteOpen.value) cancelDelete()
  else if (confirmOpen.value) cancelConfirm()
}
onMounted(() => window.addEventListener('keydown', onGlobalKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKeydown))

/* ---------------------------- 反馈操作 ---------------------------- */

async function onMarkRead(item) {
  if (busyId.value) return
  busyId.value = item.id
  try {
    await markAdminFeedbackRead(item.id)
    item.is_read = true
    feedbackUnread.value = Math.max(0, feedbackUnread.value - 1)
    showToast('已标记为已读')
  } catch (err) {
    showToast(errorText(err, '操作失败，请重试'))
  } finally {
    busyId.value = null
  }
}

/* ---------------------------- 赛事核对 ---------------------------- */
/* 抽取结果只进队列，采纳才写主表；日期必须能在官网页面上找到，否则抽取直接报错。 */

async function loadFacts() {
  factLoading.value = true
  factError.value = ''
  try {
    const data = await getFactQueue({ status: 'pending', limit: 50 })
    factQueue.value = data.items
  } catch (err) {
    factError.value = errorText(err, '待确认队列加载失败')
  } finally {
    factLoading.value = false
  }
}

/** 抓一轮官网（同步，最多 5 条，单条约 30-60 秒） */
async function runFactFetch() {
  if (factFetching.value) return
  factFetching.value = true
  factError.value = ''
  try {
    showToast('正在抓官网 + 抽取日期，约 30-60 秒…')
    const ids = factIdsInput.value
      .split(/[\s,，]+/)
      .map((raw) => Number(raw))
      .filter((id) => Number.isInteger(id) && id > 0)
    const payload = { limit: 5, competition_ids: ids }
    // 指定赛事 1 条时才允许覆盖来源页（批量时同一个链接没有意义）
    if (ids.length === 1 && factSourceUrl.value.trim()) {
      payload.source_url = factSourceUrl.value.trim()
    }
    const data = await fetchFactCandidates(payload)
    showToast(data?.message || '已入队')
    const failed = (data?.results || []).filter((row) => !row.ok)
    if (failed.length) {
      factError.value = failed
        .map((row) => `${row.name || row.competition_id}：${row.error}`)
        .join('；')
    }
    await loadFacts()
  } catch (err) {
    factError.value = errorText(err, '抽取失败，请稍后重试')
  } finally {
    factFetching.value = false
  }
}

async function adoptFact(item) {
  if (factBusyId.value) return
  factBusyId.value = item.id
  try {
    await applyFactCandidate(item.id)
    showToast('已采纳，前台立即生效')
    await loadFacts()
  } catch (err) {
    factError.value = errorText(err, '采纳失败')
  } finally {
    factBusyId.value = null
  }
}

async function ignoreFact(item) {
  if (factBusyId.value) return
  factBusyId.value = item.id
  try {
    await rejectFactCandidate(item.id)
    await loadFacts()
  } catch (err) {
    factError.value = errorText(err, '操作失败')
  } finally {
    factBusyId.value = null
  }
}

async function markUnknown(item) {
  if (factBusyId.value) return
  factBusyId.value = item.id
  try {
    await markFactUnknown(item.id)
    showToast('已标记为官网未公布')
    await loadFacts()
  } catch (err) {
    factError.value = errorText(err, '操作失败')
  } finally {
    factBusyId.value = null
  }
}

/* ---- 手动校正日期（赛事管理列表的「日期」按钮） ---- */

function openFactsEditor(item) {
  factsEditItem.value = item
  factsForm.value = {
    reg_start_date: item.reg_start_date || '',
    reg_end_date: item.reg_end_date || '',
    contest_start_date: item.contest_start_date || '',
    contest_end_date: item.contest_end_date || '',
    source_url: item.official_url || ''
  }
  factsEditError.value = ''
  factsEditOpen.value = true
}

function cancelFactsEdit() {
  if (factsSaving.value) return
  factsEditOpen.value = false
  factsEditItem.value = null
}

async function saveFactsEdit() {
  const item = factsEditItem.value
  if (!item || factsSaving.value) return
  factsSaving.value = true
  factsEditError.value = ''
  try {
    await saveCompetitionFacts(item.id, { ...factsForm.value })
    showToast('日期已保存')
    factsEditOpen.value = false
    factsEditItem.value = null
    await loadCompetitions()
  } catch (err) {
    factsEditError.value = errorText(err, '保存失败，请检查日期格式')
  } finally {
    factsSaving.value = false
  }
}

/* ---------------------------- 侧边栏 ---------------------------- */

const navItems = computed(() => [
  {
    id: 'candidates',
    label: '候选赛事',
    hint: '待审核',
    count: candidatesTotal.value
  },
  {
    id: 'feedback',
    label: '用户反馈',
    hint: '未读',
    count: feedbackUnread.value
  },
  {
    id: 'competitions',
    label: '赛事管理',
    hint: '主库',
    count: competitionsTotal.value
  },
  {
    id: 'changelogs',
    label: '更新日志管理',
    hint: '发布',
    count: changelogs.value.length
  },
  {
    id: 'facts',
    label: '赛事核对',
    hint: '待确认',
    count: factQueue.value.length
  }
])

const currentTitle = computed(() => {
  if (activeTab.value === 'candidates') return '候选赛事审核'
  if (activeTab.value === 'feedback') return '用户反馈'
  if (activeTab.value === 'changelogs') return '更新日志管理'
  if (activeTab.value === 'facts') return '赛事核对'
  return '赛事管理'
})

const currentDesc = computed(() => {
  if (activeTab.value === 'candidates') {
    return '从采集库里捞出来的赛事，通过后进入正式赛事列表，拒绝则仅在候选表里标记。'
  }
  if (activeTab.value === 'feedback') {
    return '用户在「个人中心」提交的反馈，标为已读后不再计入未读角标。'
  }
  if (activeTab.value === 'changelogs') {
    return '发布后立即出现在前台「更新日志」弹窗里；正文支持 Markdown，版本号不可重复。'
  }
  if (activeTab.value === 'facts') {
    return '抓官网抽出来的日期先进这里，点「采纳」才写进正式库；找不到日期的赛事，可在「赛事管理」里手动改。'
  }
  return '正式赛事库里的全部比赛，可在这里永久删除（连带获奖作品、备赛灵感与 AI 洞察，不可恢复）。'
})

const currentLoading = computed(() => {
  if (activeTab.value === 'candidates') return candidatesLoading.value
  if (activeTab.value === 'feedback') return feedbackLoading.value
  if (activeTab.value === 'changelogs') return changelogsLoading.value
  if (activeTab.value === 'facts') return factLoading.value
  return competitionsLoading.value
})

onMounted(() => {
  // 四个面板都先拉一次：侧边栏的待审 / 未读 / 赛事总数 / 日志数角标需要这些数字
  loadCandidates()
  loadFeedback()
  loadCompetitions()
  loadChangelogs()
  loadFacts()
})
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto w-[88%] max-w-7xl pb-24 pt-24 sm:pt-28">
      <!-- 页头 -->
      <header class="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-slate-900 dark:text-slate-100 sm:text-3xl">
            管理后台
          </h1>
          <p class="mt-2 text-sm text-slate-500 dark:text-slate-400">
            赛事候选审核 · 用户反馈处理 · 更新日志发布
          </p>
        </div>
        <button
          type="button"
          class="rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800
                 px-5 py-2 text-sm font-semibold text-slate-600 dark:text-slate-300
                 transition-colors hover:border-slate-300 dark:hover:border-slate-600
                 hover:text-slate-900 dark:hover:text-white disabled:opacity-50"
          :disabled="currentLoading"
          @click="refresh"
        >
          {{ currentLoading ? '刷新中…' : '刷新' }}
        </button>
      </header>

      <div class="grid gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
        <!-- 左侧侧边栏 -->
        <aside>
          <nav
            class="flex gap-2 overflow-x-auto rounded-2xl border border-slate-200 dark:border-slate-700
                   bg-white dark:bg-slate-800 p-2 shadow-sm lg:flex-col lg:gap-1 lg:overflow-visible"
          >
            <button
              v-for="item in navItems"
              :key="item.id"
              type="button"
              class="flex shrink-0 items-center justify-between gap-2 rounded-xl px-4 py-3 text-left
                     text-sm font-semibold transition-colors lg:w-full"
              :class="activeTab === item.id
                ? 'bg-blue-50 dark:bg-blue-500/15 text-blue-600 dark:text-blue-400'
                : 'text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700/60'"
              @click="switchTab(item.id)"
            >
              <span class="whitespace-nowrap">{{ item.label }}</span>
              <span
                class="min-w-6 rounded-full px-1.5 py-0.5 text-center text-xs font-bold"
                :class="activeTab === item.id
                  ? 'bg-blue-600 text-white dark:bg-blue-500'
                  : 'bg-slate-100 text-slate-500 dark:bg-slate-700/60 dark:text-slate-300'"
              >
                {{ item.count }}
              </span>
            </button>
          </nav>
        </aside>

        <!-- 右侧内容区 -->
        <section
          class="min-w-0 rounded-2xl border border-slate-200 dark:border-slate-700
                 bg-white dark:bg-slate-800 p-5 shadow-sm sm:p-6"
        >
          <div class="mb-4">
            <h2 class="text-lg font-bold text-slate-900 dark:text-slate-100">{{ currentTitle }}</h2>
            <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">{{ currentDesc }}</p>
          </div>

          <!-- ============ 候选赛事 ============ -->
          <template v-if="activeTab === 'candidates'">
            <div
              v-if="candidatesError"
              class="mb-4 rounded-xl border border-red-100 dark:border-red-500/30 bg-red-50 dark:bg-red-500/15
                     px-4 py-3 text-sm font-semibold text-red-600 dark:text-red-400"
            >
              {{ candidatesError }}
            </div>

            <!-- 骨架屏 -->
            <div v-if="candidatesLoading" class="space-y-2">
              <div
                v-for="n in 5"
                :key="n"
                class="h-12 animate-pulse rounded-xl bg-slate-100 dark:bg-slate-700/60"
              ></div>
            </div>

            <div
              v-else-if="!candidates.length"
              class="rounded-xl border border-dashed border-slate-200 dark:border-slate-700
                     px-4 py-14 text-center text-sm text-slate-500 dark:text-slate-400"
            >
              <template v-if="candidatesError">数据没加载出来，点右上角「刷新」重试</template>
              <template v-else>没有待审核的候选赛事，全部处理完了 🎉</template>
            </div>

            <template v-else>
              <div class="overflow-x-auto">
                <table class="w-full min-w-[720px] border-collapse text-left text-sm">
                  <thead>
                    <tr class="border-b border-slate-200 dark:border-slate-700 text-xs uppercase tracking-wide
                               text-slate-500 dark:text-slate-400">
                      <th class="px-3 py-3 font-bold">赛事名称</th>
                      <th class="px-3 py-3 font-bold">级别</th>
                      <th class="px-3 py-3 font-bold">来源</th>
                      <th class="px-3 py-3 font-bold">抓取月份</th>
                      <th class="px-3 py-3 text-right font-bold">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="item in candidates"
                      :key="item.id"
                      class="border-b border-slate-100 dark:border-slate-700/60
                             transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/30"
                    >
                      <td class="px-3 py-3">
                        <p class="font-semibold text-slate-900 dark:text-slate-100">{{ item.name }}</p>
                        <a
                          v-if="item.source_url"
                          :href="item.source_url"
                          target="_blank"
                          rel="noopener noreferrer"
                          class="mt-0.5 block max-w-[420px] truncate text-xs text-blue-600 dark:text-blue-400
                                 hover:underline"
                        >
                          {{ item.source_url }}
                        </a>
                      </td>
                      <td class="whitespace-nowrap px-3 py-3">
                        <span
                          class="rounded-full bg-slate-100 dark:bg-slate-700/60 px-2.5 py-1
                                 text-xs font-semibold text-slate-600 dark:text-slate-300"
                        >
                          {{ item.level_label || '未标注' }}
                        </span>
                      </td>
                      <td class="whitespace-nowrap px-3 py-3 text-slate-600 dark:text-slate-300">
                        {{ sourceLabel(item.source) }}
                      </td>
                      <td class="whitespace-nowrap px-3 py-3 text-slate-600 dark:text-slate-300">
                        {{ formatMonth(item.created_at) }}
                      </td>
                      <td class="whitespace-nowrap px-3 py-3 text-right">
                        <div class="inline-flex gap-2">
                          <button
                            type="button"
                            class="rounded-full bg-blue-600 px-3.5 py-1.5 text-xs font-bold text-white
                                   transition-colors hover:bg-blue-700 disabled:opacity-50"
                            :disabled="busyId === item.id"
                            @click="askAudit('approve', item)"
                          >
                            {{ busyId === item.id ? '处理中…' : '通过' }}
                          </button>
                          <button
                            type="button"
                            class="rounded-full border border-slate-200 dark:border-slate-600 px-3.5 py-1.5
                                   text-xs font-bold text-slate-600 dark:text-slate-300
                                   transition-colors hover:border-red-300 hover:text-red-600
                                   dark:hover:border-red-500/40 dark:hover:text-red-400
                                   disabled:opacity-50"
                            :disabled="busyId === item.id"
                            @click="askAudit('reject', item)"
                          >
                            拒绝
                          </button>
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p class="mt-3 text-xs text-slate-500 dark:text-slate-400">
                共 {{ candidates.length }} 条待审（后端统计 {{ candidatesTotal }} 条）
              </p>
            </template>
          </template>

          <!-- ============ 赛事管理 ============ -->
          <template v-else-if="activeTab === 'competitions'">
            <div
              v-if="competitionsError"
              class="mb-4 rounded-xl border border-red-100 dark:border-red-500/30 bg-red-50 dark:bg-red-500/15
                     px-4 py-3 text-sm font-semibold text-red-600 dark:text-red-400"
            >
              {{ competitionsError }}
            </div>

            <div v-if="competitionsLoading" class="space-y-2">
              <div
                v-for="n in 5"
                :key="n"
                class="h-12 animate-pulse rounded-xl bg-slate-100 dark:bg-slate-700/60"
              ></div>
            </div>

            <div
              v-else-if="!competitions.length"
              class="rounded-xl border border-dashed border-slate-200 dark:border-slate-700
                     px-4 py-14 text-center text-sm text-slate-500 dark:text-slate-400"
            >
              <template v-if="competitionsError">数据没加载出来，点右上角「刷新」重试</template>
              <template v-else>赛事库里暂时没有比赛</template>
            </div>

            <template v-else>
              <div class="overflow-x-auto">
                <table class="w-full min-w-[760px] border-collapse text-left text-sm">
                  <thead>
                    <tr class="border-b border-slate-200 dark:border-slate-700 text-xs uppercase tracking-wide
                               text-slate-500 dark:text-slate-400">
                      <th class="px-3 py-3 font-bold">赛事名称</th>
                      <th class="px-3 py-3 font-bold">级别</th>
                      <th class="px-3 py-3 font-bold">来源</th>
                      <th class="px-3 py-3 font-bold">获奖</th>
                      <th class="px-3 py-3 font-bold">入库时间</th>
                      <th class="px-3 py-3 text-right font-bold">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="item in competitions"
                      :key="item.id"
                      class="border-b border-slate-100 dark:border-slate-700/60
                             transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/30"
                    >
                      <td class="px-3 py-3">
                        <router-link
                          :to="`/competition/${item.id}`"
                          target="_blank"
                          class="font-semibold text-slate-900 hover:text-blue-600
                                 dark:text-slate-100 dark:hover:text-blue-400"
                        >
                          {{ item.name }}
                        </router-link>
                        <span
                          v-if="item.short_name"
                          class="ml-2 rounded-full bg-slate-100 dark:bg-slate-700/60 px-2 py-0.5
                                 text-xs font-semibold text-slate-500 dark:text-slate-300"
                        >
                          {{ item.short_name }}
                        </span>
                      </td>
                      <td class="whitespace-nowrap px-3 py-3">
                        <span
                          class="rounded-full bg-slate-100 dark:bg-slate-700/60 px-2.5 py-1
                                 text-xs font-semibold text-slate-600 dark:text-slate-300"
                        >
                          {{ item.level_label || '未标注' }}
                        </span>
                      </td>
                      <td class="whitespace-nowrap px-3 py-3 text-slate-600 dark:text-slate-300">
                        {{ sourceLabel(item.source) }}
                      </td>
                      <td class="whitespace-nowrap px-3 py-3 text-slate-600 dark:text-slate-300">
                        {{ item.awards_count }}
                      </td>
                      <td class="whitespace-nowrap px-3 py-3 text-slate-600 dark:text-slate-300">
                        {{ formatMonth(item.created_at) }}
                      </td>
                      <td class="whitespace-nowrap px-3 py-3 text-right">
                        <button
                          type="button"
                          class="mr-2 rounded-full border border-slate-200 dark:border-slate-600 px-3.5 py-1.5
                                 text-xs font-bold text-slate-600 dark:text-slate-300
                                 transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60"
                          @click="openFactsEditor(item)"
                        >
                          日期
                        </button>
                        <button
                          type="button"
                          class="rounded-full border border-red-200 dark:border-red-500/40 px-3.5 py-1.5
                                 text-xs font-bold text-red-600 dark:text-red-400
                                 transition-colors hover:bg-red-600 hover:text-white
                                 dark:hover:bg-red-600 dark:hover:text-white disabled:opacity-50"
                          :disabled="busyId === item.id"
                          @click="askDelete(item)"
                        >
                          {{ busyId === item.id ? '删除中…' : '删除' }}
                        </button>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <div class="mt-4 flex flex-wrap items-center justify-between gap-3">
                <p class="text-xs text-slate-500 dark:text-slate-400">
                  共 {{ competitionsTotal }} 条 · 第 {{ competitionsPage + 1 }} / {{ competitionsPages }} 页
                </p>
                <div class="inline-flex items-center gap-2">
                  <button
                    type="button"
                    class="rounded-full border border-slate-200 dark:border-slate-600 px-3.5 py-1.5
                           text-xs font-bold text-slate-600 dark:text-slate-300
                           transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60 disabled:opacity-40"
                    :disabled="competitionsPage === 0 || competitionsLoading"
                    @click="goCompetitionsPage(competitionsPage - 1)"
                  >
                    上一页
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-slate-200 dark:border-slate-600 px-3.5 py-1.5
                           text-xs font-bold text-slate-600 dark:text-slate-300
                           transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60 disabled:opacity-40"
                    :disabled="competitionsPage >= competitionsPages - 1 || competitionsLoading"
                    @click="goCompetitionsPage(competitionsPage + 1)"
                  >
                    下一页
                  </button>
                </div>
              </div>
            </template>
          </template>

          <!-- ============ 赛事核对 ============ -->
          <template v-else-if="activeTab === 'facts'">
            <div class="flex flex-wrap items-center gap-3">
              <button
                type="button"
                class="rounded-full bg-blue-600 px-5 py-2.5 text-sm font-bold text-white
                       transition-colors hover:bg-blue-700 disabled:opacity-50"
                :disabled="factFetching"
                @click="runFactFetch"
              >
                {{ factFetching ? '抽取中…' : '抓官网抽 5 条' }}
              </button>
              <p class="text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                抽取结果只进队列，点「采纳」才写正式库；日期必须能在官网页面上找到，
                找不到会直接报错（不会编）。
              </p>
            </div>

            <div class="mt-3 grid gap-2 sm:grid-cols-[180px_1fr]">
              <input
                v-model="factIdsInput"
                type="text"
                placeholder="赛事 ID（可选，如 10）"
                class="rounded-xl border border-slate-200 dark:border-slate-600
                       bg-white dark:bg-slate-900 px-3 py-2 text-sm text-slate-700
                       dark:text-slate-200"
              />
              <input
                v-model="factSourceUrl"
                type="url"
                placeholder="来源页链接（可选，仅指定 1 条赛事时生效；官网首页没有日期时填通知页）"
                class="rounded-xl border border-slate-200 dark:border-slate-600
                       bg-white dark:bg-slate-900 px-3 py-2 text-sm text-slate-700
                       dark:text-slate-200"
              />
            </div>

            <div
              v-if="factError"
              class="mt-4 rounded-xl border border-red-100 dark:border-red-500/30 bg-red-50 dark:bg-red-500/15
                     px-4 py-3 text-sm font-semibold text-red-600 dark:text-red-400"
            >
              {{ factError }}
            </div>

            <div v-if="factLoading" class="mt-4 space-y-2">
              <div
                v-for="n in 3"
                :key="n"
                class="h-20 animate-pulse rounded-2xl bg-slate-100 dark:bg-slate-700/60"
              ></div>
            </div>

            <div
              v-else-if="!factQueue.length"
              class="mt-4 rounded-xl border border-dashed border-slate-200 dark:border-slate-700
                     px-4 py-14 text-center text-sm text-slate-500 dark:text-slate-400"
            >
              队列是空的：点上面的按钮抓一轮官网
            </div>

            <div v-else class="mt-4 space-y-3">
              <article
                v-for="item in factQueue"
                :key="item.id"
                class="rounded-2xl border border-slate-200 dark:border-slate-700
                       bg-white dark:bg-slate-800/60 p-4"
              >
                <header class="flex flex-wrap items-start justify-between gap-2">
                  <div class="min-w-0">
                    <p class="break-words font-bold text-slate-900 dark:text-slate-100">
                      {{ item.competition_name }}
                    </p>
                    <p class="mt-1 break-all text-xs text-slate-500 dark:text-slate-400">
                      <a
                        v-if="item.source_url"
                        :href="item.source_url"
                        target="_blank"
                        rel="noopener"
                        class="underline hover:text-blue-600"
                      >{{ item.source_url }}</a>
                      · 抽取于 {{ item.fetched_at }}
                    </p>
                  </div>
                  <span
                    class="shrink-0 rounded-full bg-amber-50 dark:bg-amber-500/15 px-2.5 py-1
                           text-xs font-bold text-amber-600 dark:text-amber-400"
                  >
                    待确认 {{ item.diffs.length }} 项
                  </span>
                </header>

                <ul class="mt-3 space-y-1.5 text-sm">
                  <li
                    v-for="d in item.diffs"
                    :key="d.field"
                    class="flex flex-wrap items-center gap-2"
                  >
                    <span class="w-16 shrink-0 text-xs font-bold text-slate-500 dark:text-slate-400">
                      {{ d.label }}
                    </span>
                    <span class="text-slate-400 line-through">{{ d.current || '空' }}</span>
                    <span class="text-slate-400">→</span>
                    <span class="font-bold text-emerald-600 dark:text-emerald-400">{{ d.incoming }}</span>
                  </li>
                </ul>

                <p
                  v-if="item.evidence"
                  class="mt-3 rounded-lg bg-slate-50 dark:bg-slate-700/40 px-3 py-2
                         text-xs leading-relaxed text-slate-500 dark:text-slate-400"
                >
                  原文：{{ item.evidence }}
                </p>

                <div class="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    class="rounded-full bg-emerald-600 px-4 py-1.5 text-xs font-bold text-white
                           transition-colors hover:bg-emerald-700 disabled:opacity-50"
                    :disabled="factBusyId === item.id"
                    @click="adoptFact(item)"
                  >
                    全部采纳
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-slate-200 dark:border-slate-600 px-4 py-1.5
                           text-xs font-bold text-slate-600 dark:text-slate-300
                           transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60 disabled:opacity-50"
                    :disabled="factBusyId === item.id"
                    @click="markUnknown(item)"
                  >
                    官网未公布
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-slate-200 dark:border-slate-600 px-4 py-1.5
                           text-xs font-bold text-slate-500 dark:text-slate-400
                           transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60 disabled:opacity-50"
                    :disabled="factBusyId === item.id"
                    @click="ignoreFact(item)"
                  >
                    忽略
                  </button>
                  <router-link
                    :to="`/competition/${item.competition_id}`"
                    target="_blank"
                    class="text-xs font-bold text-blue-600 hover:underline dark:text-blue-400"
                  >
                    查看赛事 →
                  </router-link>
                </div>
              </article>
            </div>
          </template>

          <!-- ============ 用户反馈 ============ -->
          <template v-else-if="activeTab === 'feedback'">
            <div
              v-if="feedbackError"
              class="mb-4 rounded-xl border border-red-100 dark:border-red-500/30 bg-red-50 dark:bg-red-500/15
                     px-4 py-3 text-sm font-semibold text-red-600 dark:text-red-400"
            >
              {{ feedbackError }}
            </div>

            <div v-if="feedbackLoading" class="space-y-2">
              <div
                v-for="n in 4"
                :key="n"
                class="h-16 animate-pulse rounded-xl bg-slate-100 dark:bg-slate-700/60"
              ></div>
            </div>

            <div
              v-else-if="!feedbacks.length"
              class="rounded-xl border border-dashed border-slate-200 dark:border-slate-700
                     px-4 py-14 text-center text-sm text-slate-500 dark:text-slate-400"
            >
              <template v-if="feedbackError">数据没加载出来，点右上角「刷新」重试</template>
              <template v-else>还没有用户反馈</template>
            </div>

            <ul v-else class="space-y-3">
              <li
                v-for="item in feedbacks"
                :key="item.id"
                class="rounded-xl border border-slate-200 dark:border-slate-700 px-4 py-3
                       transition-colors"
                :class="item.is_read
                  ? 'bg-white dark:bg-slate-800'
                  : 'bg-blue-50/50 dark:bg-blue-500/10 border-blue-100 dark:border-blue-500/30'"
              >
                <div class="flex flex-wrap items-center gap-2 text-xs">
                  <span class="font-bold text-slate-700 dark:text-slate-200">
                    {{ item.username || '已注销用户' }}
                  </span>
                  <span class="text-slate-400 dark:text-slate-500">·</span>
                  <span class="text-slate-500 dark:text-slate-400">{{ formatTime(item.created_at) }}</span>
                  <span
                    class="ml-auto rounded-full px-2 py-0.5 font-bold"
                    :class="item.is_read
                      ? 'bg-slate-100 text-slate-500 dark:bg-slate-700/60 dark:text-slate-300'
                      : 'bg-blue-600 text-white dark:bg-blue-500'"
                  >
                    {{ item.is_read ? '已读' : '未读' }}
                  </span>
                </div>

                <p class="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed
                          text-slate-700 dark:text-slate-200">
                  {{ item.content }}
                </p>

                <div v-if="!item.is_read" class="mt-3">
                  <button
                    type="button"
                    class="rounded-full border border-slate-200 dark:border-slate-600 px-3.5 py-1.5
                           text-xs font-bold text-slate-600 dark:text-slate-300
                           transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60
                           disabled:opacity-50"
                    :disabled="busyId === item.id"
                    @click="onMarkRead(item)"
                  >
                    {{ busyId === item.id ? '处理中…' : '标记已读' }}
                  </button>
                </div>
              </li>
            </ul>

            <p v-if="feedbacks.length" class="mt-3 text-xs text-slate-500 dark:text-slate-400">
              共 {{ feedbacks.length }} 条，未读 {{ feedbackUnread }} 条
            </p>
          </template>

          <!-- ============ 更新日志管理 ============ -->
          <template v-else-if="activeTab === 'changelogs'">
            <!-- 发布表单 -->
            <form
              class="rounded-xl border border-slate-200 dark:border-slate-700
                     bg-slate-50/60 dark:bg-slate-900/40 p-4"
              @submit.prevent="onPublish"
            >
              <div class="mb-3 flex items-center justify-between gap-2">
                <p class="text-xs font-bold text-slate-600 dark:text-slate-300">
                  {{ editingId ? '正在编辑这条日志' : '发布新日志' }}
                </p>
                <button
                  v-if="editingId"
                  type="button"
                  class="rounded-full border border-slate-200 dark:border-slate-600 px-3 py-1
                         text-xs font-bold text-slate-600 dark:text-slate-300
                         transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60"
                  :disabled="publishing"
                  @click="cancelEdit"
                >
                  取消编辑
                </button>
              </div>
              <div class="grid gap-3 sm:grid-cols-[160px_minmax(0,1fr)]">
                <label class="block">
                  <span class="text-xs font-bold text-slate-600 dark:text-slate-300">版本号</span>
                  <input
                    v-model="publishForm.version"
                    type="text"
                    maxlength="40"
                    placeholder="v1.0.0"
                    class="mt-1.5 w-full rounded-xl border border-slate-200 dark:border-slate-600
                           bg-white dark:bg-slate-800 px-3 py-2 text-sm font-semibold
                           text-slate-800 dark:text-slate-100 outline-none
                           placeholder:text-slate-400 focus:border-blue-400
                           focus:ring-2 focus:ring-blue-500/20"
                  />
                </label>
                <label class="block">
                  <span class="text-xs font-bold text-slate-600 dark:text-slate-300">发布日期</span>
                  <input
                    v-model="publishForm.release_date"
                    type="date"
                    class="mt-1.5 w-full rounded-xl border border-slate-200 dark:border-slate-600
                           bg-white dark:bg-slate-800 px-3 py-2 text-sm font-semibold
                           text-slate-800 dark:text-slate-100 outline-none
                           focus:border-blue-400 focus:ring-2 focus:ring-blue-500/20"
                  />
                </label>
              </div>

              <label class="mt-3 block">
                <span class="flex items-center justify-between gap-2">
                  <span class="text-xs font-bold text-slate-600 dark:text-slate-300">
                    更新内容（Markdown）
                  </span>
                  <span class="text-[11px] font-medium text-slate-400 dark:text-slate-500">
                    {{ publishForm.content_markdown.length }} / 20000
                  </span>
                </span>
                <textarea
                  v-model="publishForm.content_markdown"
                  rows="6"
                  maxlength="20000"
                  placeholder="## 新增&#10;- 语义检索支持自然语言提问&#10;&#10;## 修复&#10;- 列表页返回丢失筛选条件"
                  class="mt-1.5 w-full resize-y rounded-xl border border-slate-200 dark:border-slate-600
                         bg-white dark:bg-slate-800 px-3 py-2 font-mono text-[13px] leading-6
                         text-slate-800 dark:text-slate-100 outline-none
                         placeholder:text-slate-400 focus:border-blue-400
                         focus:ring-2 focus:ring-blue-500/20"
                ></textarea>
              </label>

              <p
                v-if="publishError"
                class="mt-2 text-sm font-semibold text-rose-600 dark:text-rose-400"
                role="alert"
              >
                {{ publishError }}
              </p>

              <div class="mt-3 flex justify-end gap-2">
                <button
                  v-if="editingId"
                  type="button"
                  class="rounded-full border border-slate-200 dark:border-slate-600 px-5 py-2
                         text-sm font-bold text-slate-600 dark:text-slate-300
                         transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60
                         disabled:opacity-50"
                  :disabled="publishing"
                  @click="cancelEdit"
                >
                  取消
                </button>
                <button
                  type="submit"
                  class="rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white
                         shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700
                         disabled:cursor-not-allowed disabled:opacity-60"
                  :disabled="publishing"
                >
                  {{ publishing ? (editingId ? '保存中…' : '发布中…') : (editingId ? '保存修改' : '发布') }}
                </button>
              </div>
            </form>

            <!-- 已发布列表 -->
            <div
              v-if="changelogsError"
              class="mt-4 rounded-xl border border-red-100 dark:border-red-500/30 bg-red-50 dark:bg-red-500/15
                     px-4 py-3 text-sm font-semibold text-red-600 dark:text-red-400"
            >
              {{ changelogsError }}
            </div>

            <div v-if="changelogsLoading" class="mt-4 space-y-2">
              <div
                v-for="n in 3"
                :key="n"
                class="h-14 animate-pulse rounded-xl bg-slate-100 dark:bg-slate-700/60"
              ></div>
            </div>

            <div
              v-else-if="!changelogs.length"
              class="mt-4 rounded-xl border border-dashed border-slate-200 dark:border-slate-700
                     px-4 py-12 text-center text-sm text-slate-500 dark:text-slate-400"
            >
              还没有发布过更新日志
            </div>

            <ul v-else class="mt-4 space-y-2">
              <li
                v-for="item in changelogs"
                :key="item.id"
                class="rounded-xl border border-slate-200 dark:border-slate-700 px-4 py-3"
              >
                <div class="flex flex-wrap items-center gap-2">
                  <span class="rounded-full bg-blue-600 px-2 py-0.5 text-xs font-bold text-white">
                    {{ item.version }}
                  </span>
                  <span class="text-xs font-semibold text-slate-500 dark:text-slate-400">
                    {{ item.release_date }}
                  </span>
                  <span class="ml-auto text-[11px] text-slate-400 dark:text-slate-500">
                    创建于 {{ formatTime(item.created_at) }}
                  </span>
                </div>
                <p class="mt-2 line-clamp-2 whitespace-pre-wrap break-words text-xs leading-6
                          text-slate-600 dark:text-slate-300">
                  {{ item.content_markdown }}
                </p>
                <div class="mt-2 flex justify-end gap-2">
                  <button
                    type="button"
                    class="rounded-full border border-slate-200 dark:border-slate-600 px-3 py-1
                           text-xs font-bold text-slate-600 dark:text-slate-300
                           transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/60
                           disabled:opacity-50"
                    :disabled="publishing || deletingId === item.id"
                    @click="startEdit(item)"
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-slate-200 dark:border-slate-600 px-3 py-1
                           text-xs font-bold text-rose-600 dark:text-rose-400
                           transition-colors hover:border-rose-200 hover:bg-rose-50
                           dark:hover:bg-rose-500/15 disabled:opacity-50"
                    :disabled="publishing || deletingId === item.id"
                    @click="askDeleteChangelog(item)"
                  >
                    {{ deletingId === item.id ? '删除中…' : '删除' }}
                  </button>
                </div>
              </li>
            </ul>

            <p v-if="changelogs.length" class="mt-3 text-xs text-slate-500 dark:text-slate-400">
              共 {{ changelogs.length }} 条，前台按发布日期倒序展示
            </p>
          </template>
        </section>
      </div>
    </main>

    <!-- ============ 二次确认弹窗（审核写主库，防误触） ============ -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        enter-to-class="opacity-100"
        leave-active-class="transition duration-100 ease-in"
        leave-from-class="opacity-100"
        leave-to-class="opacity-0"
      >
        <div
          v-if="confirmOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4 backdrop-blur-sm"
          @click.self="cancelConfirm"
        >
          <div
            role="dialog"
            aria-modal="true"
            class="w-full max-w-[440px] rounded-2xl border border-slate-200 bg-white p-6
                   shadow-xl dark:border-slate-700 dark:bg-slate-800"
          >
            <h2 class="text-lg font-bold text-slate-900 dark:text-slate-100">
              {{ confirmTitle }}
            </h2>

            <p
              class="mt-2 break-words rounded-xl bg-slate-50 px-3 py-2 text-sm font-semibold
                     text-slate-700 dark:bg-slate-700/50 dark:text-slate-200"
            >
              《{{ confirmItem?.name }}》
            </p>

            <p class="mt-3 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
              {{ confirmDesc }}
            </p>

            <div class="mt-6 flex justify-end gap-2">
              <button
                type="button"
                class="rounded-full border border-slate-200 px-4 py-2 text-sm font-bold
                       text-slate-600 transition-colors hover:bg-slate-50
                       disabled:opacity-50 dark:border-slate-600 dark:text-slate-300
                       dark:hover:bg-slate-700/60"
                :disabled="confirmBusy"
                @click="cancelConfirm"
              >
                取消
              </button>
              <button
                type="button"
                class="rounded-full px-4 py-2 text-sm font-bold text-white transition-colors
                       disabled:opacity-50"
                :class="confirmTone === 'reject'
                  ? 'bg-red-600 hover:bg-red-700'
                  : 'bg-blue-600 hover:bg-blue-700'"
                :disabled="confirmBusy"
                @click="confirmAudit"
              >
                {{ confirmBusy ? '提交中…' : confirmOkText }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ============ 删除赛事二次确认（不可恢复，红色警示） ============ -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        enter-to-class="opacity-100"
        leave-active-class="transition duration-100 ease-in"
        leave-from-class="opacity-100"
        leave-to-class="opacity-0"
      >
        <div
          v-if="deleteOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 px-4 backdrop-blur-sm"
          @click.self="cancelDelete"
        >
          <div
            role="dialog"
            aria-modal="true"
            class="w-full max-w-[460px] rounded-2xl border border-red-200 bg-white p-6
                   shadow-xl dark:border-red-500/40 dark:bg-slate-800"
          >
            <div class="flex items-start gap-3">
              <span
                class="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full
                       bg-red-100 text-lg font-black text-red-600 dark:bg-red-500/20 dark:text-red-400"
              >
                !
              </span>
              <div class="min-w-0">
                <h2 class="text-lg font-bold text-slate-900 dark:text-slate-100">
                  确认要永久删除吗？
                </h2>
                <p
                  class="mt-2 break-words rounded-xl bg-slate-50 px-3 py-2 text-sm font-semibold
                         text-slate-700 dark:bg-slate-700/50 dark:text-slate-200"
                >
                  《{{ deleteItem?.name }}》
                </p>
                <p class="mt-3 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
                  此操作不可恢复！将同时删除该赛事的
                  获奖作品 {{ deleteItem?.awards_count || 0 }} 条，以及关联的备赛灵感、
                  AI 洞察与用户备赛计划。
                </p>
              </div>
            </div>

            <div class="mt-6 flex justify-end gap-2">
              <button
                type="button"
                class="rounded-full border border-slate-200 px-4 py-2 text-sm font-bold
                       text-slate-600 transition-colors hover:bg-slate-50
                       disabled:opacity-50 dark:border-slate-600 dark:text-slate-300
                       dark:hover:bg-slate-700/60"
                :disabled="deleteBusy"
                @click="cancelDelete"
              >
                取消
              </button>
              <button
                type="button"
                class="rounded-full bg-red-600 px-4 py-2 text-sm font-bold text-white
                       transition-colors hover:bg-red-700 disabled:opacity-50"
                :disabled="deleteBusy"
                @click="confirmDelete"
              >
                {{ deleteBusy ? '删除中…' : '永久删除' }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ============ 更新日志删除确认 ============ -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        enter-to-class="opacity-100"
        leave-active-class="transition duration-100 ease-in"
        leave-from-class="opacity-100"
        leave-to-class="opacity-0"
      >
        <div
          v-if="clDeleteOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4 backdrop-blur-sm"
          @click.self="cancelDeleteChangelog"
        >
          <div
            role="dialog"
            aria-modal="true"
            class="w-full max-w-[420px] rounded-2xl border border-slate-200 bg-white p-6
                   shadow-xl dark:border-slate-700 dark:bg-slate-800"
          >
            <h2 class="text-lg font-bold text-slate-900 dark:text-slate-100">
              确认删除这条更新日志？
            </h2>

            <p class="mt-2 break-words rounded-xl bg-slate-50 px-3 py-2 text-sm font-semibold
                      text-slate-700 dark:bg-slate-700/50 dark:text-slate-200">
              {{ clDeleteItem?.version }} · {{ clDeleteItem?.release_date }}
            </p>

            <p class="mt-3 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
              删除后前台「更新日志」弹窗里立即不再展示，且不可恢复。
            </p>

            <div class="mt-6 flex justify-end gap-2">
              <button
                type="button"
                class="rounded-full border border-slate-200 px-4 py-2 text-sm font-bold
                       text-slate-600 transition-colors hover:bg-slate-50
                       disabled:opacity-50 dark:border-slate-600 dark:text-slate-300
                       dark:hover:bg-slate-700/60"
                :disabled="deletingId !== null"
                @click="cancelDeleteChangelog"
              >
                取消
              </button>
              <button
                type="button"
                class="rounded-full bg-red-600 px-4 py-2 text-sm font-bold text-white
                       transition-colors hover:bg-red-700 disabled:opacity-50"
                :disabled="deletingId !== null"
                @click="confirmDeleteChangelog"
              >
                {{ deletingId !== null ? '删除中…' : '确认删除' }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- 手动校正赛事日期 -->
    <Teleport to="body">
      <Transition name="fade">
        <div
          v-if="factsEditOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 px-4 py-8"
          @click.self="cancelFactsEdit"
        >
          <div
            class="w-full max-w-[520px] rounded-2xl border border-slate-200 bg-white p-6
                   shadow-xl dark:border-slate-700 dark:bg-slate-800"
          >
            <h2 class="text-lg font-bold text-slate-900 dark:text-slate-100">
              校正赛事日期
            </h2>
            <p class="mt-1 break-words text-sm text-slate-500 dark:text-slate-400">
              {{ factsEditItem?.name }}
            </p>

            <div v-if="factsEditError" class="mt-3 rounded-xl bg-red-50 dark:bg-red-500/15 px-3 py-2
                                              text-sm font-semibold text-red-600 dark:text-red-400">
              {{ factsEditError }}
            </div>

            <div class="mt-4 grid gap-3 sm:grid-cols-2">
              <label class="block text-xs font-bold text-slate-500 dark:text-slate-400">
                报名开始
                <input v-model="factsForm.reg_start_date" type="date"
                       class="mt-1 w-full rounded-xl border border-slate-200 dark:border-slate-600
                              bg-white dark:bg-slate-900 px-3 py-2 text-sm text-slate-700
                              dark:text-slate-200" />
              </label>
              <label class="block text-xs font-bold text-slate-500 dark:text-slate-400">
                报名截止
                <input v-model="factsForm.reg_end_date" type="date"
                       class="mt-1 w-full rounded-xl border border-slate-200 dark:border-slate-600
                              bg-white dark:bg-slate-900 px-3 py-2 text-sm text-slate-700
                              dark:text-slate-200" />
              </label>
              <label class="block text-xs font-bold text-slate-500 dark:text-slate-400">
                比赛开始
                <input v-model="factsForm.contest_start_date" type="date"
                       class="mt-1 w-full rounded-xl border border-slate-200 dark:border-slate-600
                              bg-white dark:bg-slate-900 px-3 py-2 text-sm text-slate-700
                              dark:text-slate-200" />
              </label>
              <label class="block text-xs font-bold text-slate-500 dark:text-slate-400">
                比赛结束
                <input v-model="factsForm.contest_end_date" type="date"
                       class="mt-1 w-full rounded-xl border border-slate-200 dark:border-slate-600
                              bg-white dark:bg-slate-900 px-3 py-2 text-sm text-slate-700
                              dark:text-slate-200" />
              </label>
            </div>

            <label class="mt-3 block text-xs font-bold text-slate-500 dark:text-slate-400">
              信息来源（官网链接）
              <input v-model="factsForm.source_url" type="url" placeholder="https://"
                     class="mt-1 w-full rounded-xl border border-slate-200 dark:border-slate-600
                            bg-white dark:bg-slate-900 px-3 py-2 text-sm text-slate-700
                            dark:text-slate-200" />
            </label>

            <p class="mt-3 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
              留空表示清空该日期；保存后前台会显示「信息核对于今天」。
            </p>

            <div class="mt-6 flex justify-end gap-2">
              <button
                type="button"
                class="rounded-full border border-slate-200 px-4 py-2 text-sm font-bold
                       text-slate-600 transition-colors hover:bg-slate-50
                       disabled:opacity-50 dark:border-slate-600 dark:text-slate-300
                       dark:hover:bg-slate-700/60"
                :disabled="factsSaving"
                @click="cancelFactsEdit"
              >
                取消
              </button>
              <button
                type="button"
                class="rounded-full bg-blue-600 px-4 py-2 text-sm font-bold text-white
                       transition-colors hover:bg-blue-700 disabled:opacity-50"
                :disabled="factsSaving"
                @click="saveFactsEdit"
              >
                {{ factsSaving ? '保存中…' : '保存' }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

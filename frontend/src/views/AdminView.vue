<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Header from '@/components/Header.vue'
import {
  getAdminCandidates,
  getAdminFeedback,
  approveAdminCandidate,
  rejectAdminCandidate,
  markAdminFeedbackRead
} from '@/services/api'
import { showToast } from '@/composables/useToast'

/**
 * 管理后台
 * ------------------------------------------------------------------
 * 布局：左侧侧边栏（候选赛事 / 用户反馈）+ 右侧内容区，沿用全站的
 *      Tailwind 暗色 / 极简风，不引入任何 UI 组件库。
 *
 * 权限：路由 meta.requiresAdmin 已在 router/index.js 拦截；
 *      后端每个接口还各自挂了 get_current_admin，前端守卫只是体验层，
 *      真正的拦截在服务端（403），这里对 401/403 也做了兜底提示。
 */

/* ---------------------------- 状态 ---------------------------- */

const route = useRoute()
const router = useRouter()

// 当前面板支持 ?tab=feedback 深链，刷新 / 前进后退后停在原面板
const activeTab = ref(route.query.tab === 'feedback' ? 'feedback' : 'candidates')

const candidates = ref([])
const candidatesTotal = ref(0)
const candidatesLoading = ref(false)
const candidatesError = ref('')

const feedbacks = ref([])
const feedbackUnread = ref(0)
const feedbackLoading = ref(false)
const feedbackError = ref('')

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

/** 来源标识 -> 中文名 */
const SOURCE_LABELS = {
  saikr: '赛氪',
  'edu-roster': '教育部门名录',
  manual: '人工录入'
}
function sourceLabel(source) {
  if (!source) return '未知'
  return SOURCE_LABELS[source] || source
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

function switchTab(tab) {
  activeTab.value = tab
  // 同步到地址栏（replace，不污染历史栈）
  router.replace({ query: { ...route.query, tab } })
  if (tab === 'candidates' && !candidates.value.length) loadCandidates()
  if (tab === 'feedback' && !feedbacks.value.length) loadFeedback()
}

function refresh() {
  if (activeTab.value === 'candidates') loadCandidates()
  else loadFeedback()
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

/** Esc 关闭弹窗，和站内其他弹窗保持一致 */
function onGlobalKeydown(event) {
  if (event.key === 'Escape' && confirmOpen.value) cancelConfirm()
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
  }
])

const currentTitle = computed(() =>
  activeTab.value === 'candidates' ? '候选赛事审核' : '用户反馈'
)

const currentDesc = computed(() =>
  activeTab.value === 'candidates'
    ? '从采集库里捞出来的赛事，通过后进入正式赛事列表，拒绝则仅在候选表里标记。'
    : '用户在「个人中心」提交的反馈，标为已读后不再计入未读角标。'
)

onMounted(() => {
  // 两个面板的数据都先拉一次：侧边栏的未读 / 待审角标需要两个数字
  if (activeTab.value === 'candidates') {
    loadCandidates()
    loadFeedback()
  } else {
    loadFeedback()
    loadCandidates()
  }
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
            赛事候选审核 · 用户反馈处理
          </p>
        </div>
        <button
          type="button"
          class="rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800
                 px-5 py-2 text-sm font-semibold text-slate-600 dark:text-slate-300
                 transition-colors hover:border-slate-300 dark:hover:border-slate-600
                 hover:text-slate-900 dark:hover:text-white disabled:opacity-50"
          :disabled="activeTab === 'candidates' ? candidatesLoading : feedbackLoading"
          @click="refresh"
        >
          {{ (activeTab === 'candidates' ? candidatesLoading : feedbackLoading) ? '刷新中…' : '刷新' }}
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

          <!-- ============ 用户反馈 ============ -->
          <template v-else>
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
  </div>
</template>

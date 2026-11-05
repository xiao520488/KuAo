<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Header from '@/components/Header.vue'
import { authState, fetchMe, logout, restoreSession } from '@/services/auth'
import { getAiQuota, getMyPreparations } from '@/services/api'

const router = useRouter()

/* ------------------------------------------------------------------ */
/* 数据状态                                                            */
/* ------------------------------------------------------------------ */

const loading = ref(true)
const errorMsg = ref('')

// 备赛数量：来自 GET /api/preparations 的 total
const prepTotal = ref(0)
// AI 配额：{ used, limit, remaining }，remaining 可能为 null（不限量）
const quota = ref({ used: 0, limit: 0, remaining: null })

const user = computed(() => authState.user || {})

// 接口字段兼容多种命名，任一存在即可展示
const displayName = computed(
  () => user.value.username || user.value.name || user.value.email || '当前用户'
)

// 头像占位：取昵称首字符
const avatarText = computed(() => {
  const name = displayName.value.trim()
  return name ? name.charAt(0).toUpperCase() : '?'
})

const joinedAt = computed(() => {
  const raw = user.value.created_at || user.value.createdAt || user.value.join_time
  if (!raw) return ''
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return String(raw)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate()
  ).padStart(2, '0')}`
})

// 配额进度：limit 为 0 视为未限制，不画进度条
const quotaUnlimited = computed(() => !quota.value.limit || quota.value.remaining === null)
const quotaUsedPercent = computed(() => {
  const { used, limit } = quota.value
  if (!limit) return 0
  return Math.min(100, Math.round((used / limit) * 100))
})
const quotaRemainingText = computed(() =>
  quotaUnlimited.value ? '不限' : String(quota.value.remaining)
)

/* ------------------------------------------------------------------ */
/* 加载                                                                */
/* ------------------------------------------------------------------ */

async function loadProfile() {
  loading.value = true
  errorMsg.value = ''

  try {
    // 并行拉取，任一失败不阻塞其他数据的展示
    const [prepRes, quotaRes] = await Promise.allSettled([
      getMyPreparations(),
      getAiQuota()
    ])

    if (prepRes.status === 'fulfilled') {
      prepTotal.value = prepRes.value.total
    }
    if (quotaRes.status === 'fulfilled') {
      quota.value = quotaRes.value
    }

    // 用户资料：先走 restoreSession（带缓存），拿不到再强制拉一次
    let me = null
    try {
      me = await restoreSession()
      if (!me) me = await fetchMe()
    } catch {
      // 资料接口失败不影响看板，authState.user 可能为空，用兜底文案
    }

    if (prepRes.status === 'rejected' && quotaRes.status === 'rejected') {
      errorMsg.value = '数据加载失败，请稍后重试'
    }
  } catch {
    errorMsg.value = '数据加载失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

onMounted(loadProfile)

/* ------------------------------------------------------------------ */
/* 操作                                                                */
/* ------------------------------------------------------------------ */

function onLogout() {
  logout()
  router.push({ name: 'home' })
}
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto w-[85%] max-w-5xl pb-24 pt-24 sm:pt-28">
      <!-- 页头 -->
      <header class="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-slate-900 dark:text-slate-100 sm:text-3xl">个人中心</h1>
          <p class="mt-2 text-sm text-slate-500 dark:text-slate-400">查看账号信息、备赛数据与快捷入口</p>
        </div>
        <button
          type="button"
          class="rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-5 py-2 text-sm font-semibold text-slate-600 dark:text-slate-300
                 transition-colors hover:border-slate-300 dark:hover:border-slate-600 hover:text-slate-900 dark:hover:text-white"
          @click="loadProfile"
          :disabled="loading"
        >
          {{ loading ? '刷新中…' : '刷新数据' }}
        </button>
      </header>

      <!-- 错误提示 -->
      <div
        v-if="errorMsg"
        class="mb-6 rounded-2xl border border-red-100 dark:border-red-500/30 bg-red-50 dark:bg-red-500/15 px-6 py-4 text-sm font-semibold text-red-600 dark:text-red-400"
      >
        {{ errorMsg }}
      </div>

      <!-- 账号信息卡 -->
      <section class="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-6 shadow-sm sm:p-8">
        <div class="flex flex-wrap items-center gap-5">
          <!-- 头像 -->
          <div
            class="flex h-16 w-16 flex-shrink-0 items-center justify-center rounded-full
                   bg-gradient-to-br from-slate-800 to-slate-600 text-2xl font-bold text-white shadow-sm"
          >
            {{ avatarText }}
          </div>

          <div class="min-w-0 flex-1">
            <template v-if="loading && !authState.user">
              <div class="h-5 w-40 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
              <div class="mt-2 h-4 w-56 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
            </template>
            <template v-else>
              <h2 class="truncate text-lg font-bold text-slate-900 dark:text-slate-100">{{ displayName }}</h2>
              <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
                <span v-if="user.email">{{ user.email }}</span>
                <span v-if="joinedAt">
                  <span v-if="user.email"> · </span>加入于 {{ joinedAt }}
                </span>
                <span v-if="!user.email && !joinedAt">已登录账号</span>
              </p>
            </template>
          </div>

          <button
            type="button"
            class="rounded-full border border-slate-200 dark:border-slate-700 px-5 py-2 text-sm font-semibold text-slate-600 dark:text-slate-300
                   transition-colors hover:border-red-200 hover:bg-red-50 dark:hover:bg-red-500/15 hover:text-red-600"
            @click="onLogout"
          >
            退出登录
          </button>
        </div>
      </section>

      <!-- 数据看板 -->
      <section class="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <!-- 备赛中 -->
        <div class="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-6 shadow-sm">
          <p class="text-sm font-medium text-slate-500 dark:text-slate-400">备赛中</p>
          <p class="mt-3 text-3xl font-bold text-slate-900 dark:text-slate-100">
            <span v-if="loading" class="inline-block h-8 w-12 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></span>
            <template v-else>{{ prepTotal }}</template>
          </p>
          <p class="mt-1 text-xs text-slate-400">项竞赛已加入备赛列表</p>
        </div>

        <!-- AI 剩余次数 -->
        <div class="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-6 shadow-sm">
          <p class="text-sm font-medium text-slate-500 dark:text-slate-400">AI 今日剩余次数</p>
          <p class="mt-3 text-3xl font-bold text-slate-900 dark:text-slate-100">
            <span v-if="loading" class="inline-block h-8 w-12 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></span>
            <template v-else>{{ quotaRemainingText }}</template>
          </p>
          <div v-if="!quotaUnlimited" class="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700/60">
            <div
              class="h-full rounded-full bg-slate-800 transition-all"
              :style="{ width: quotaUsedPercent + '%' }"
            ></div>
          </div>
          <p class="mt-1 text-xs text-slate-400">
            {{ quotaUnlimited ? '当前账号不限次数' : `已用 ${quota.used} / ${quota.limit}` }}
          </p>
          <p class="mt-3 flex items-start gap-1.5 rounded-lg bg-slate-50 dark:bg-slate-900 px-3 py-2 text-[11px] font-semibold leading-relaxed text-slate-500 dark:text-slate-400 ring-1 ring-inset ring-slate-200 dark:ring-slate-700">
            <svg class="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-slate-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="9" />
              <path stroke-linecap="round" d="M12 8v.01M12 11v5" />
            </svg>
            <span>额度每日刷新，次日 00:00 重置</span>
          </p>
        </div>

        <!-- 账号状态 -->
        <div class="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-6 shadow-sm">
          <p class="text-sm font-medium text-slate-500 dark:text-slate-400">账号状态</p>
          <p class="mt-3 flex items-center gap-2 text-xl font-bold text-slate-900 dark:text-slate-100">
            <span class="inline-block h-2.5 w-2.5 rounded-full bg-emerald-500"></span>
            正常
          </p>
          <p class="mt-1 text-xs text-slate-400">已登录，可正常使用全部功能</p>
        </div>
      </section>

    </main>
  </div>
</template>

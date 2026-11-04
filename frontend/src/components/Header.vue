<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

// 当前路由，用于动态判断导航高亮
const route = useRoute()
const router = useRouter()

/* ---------------- 顶部全局搜索 ---------------- */
const keyword = ref('')      // 顶部搜索框输入值
const searching = ref(false) // 防重复提交

/**
 * 顶部搜索：回车触发，跳转首页并携带 q 参数
 * 真正的检索交由 HomeView 监听 route.query.q 完成
 */
function onSearch() {
  const q = keyword.value.trim()
  if (!q || searching.value) return

  searching.value = true
  router
    .push({ path: '/', query: { q } })
    .finally(() => {
      searching.value = false
      keyword.value = '' // 跳转后清空，避免残留
    })
}

// 每项携带 path，active 由当前路由计算得出（不再写死）
const navItems = [
  { label: '首页', path: '/' },
  { label: '竞赛', path: '/competitions' },
  { label: '备赛', path: '/preparation' }
]

// 判断某项是否处于高亮：
// - 首页 '/' 需精确匹配，避免所有路由都被当成首页
// - 其它路径按前缀匹配，兼容子路由（如 /competitions/123）
function isActive(item) {
  const current = route.path || '/'
  if (item.path === '/') return current === '/'
  return current === item.path || current.startsWith(item.path + '/')
}

// 供模板使用的带高亮状态的列表
const navList = computed(() =>
  navItems.map((item) => ({ ...item, active: isActive(item) }))
)
</script>

<template>
  <header
    class="fixed top-0 inset-x-0 z-50 h-16
           bg-white/60 backdrop-blur-xl backdrop-saturate-150
           border-b border-white/40 shadow-sm shadow-slate-900/5"
  >
    <div class="mx-auto h-full max-w-7xl px-6 flex items-center justify-between">
      <!-- 左：Logo -->
      <router-link to="/" class="text-2xl font-extrabold tracking-tight text-slate-900 select-none">
        库<span class="text-blue-600">奥</span>
      </router-link>

      <!-- 中：居中菜单 -->
      <nav class="hidden md:flex items-center gap-8 absolute left-1/2 -translate-x-1/2">
        <router-link
          v-for="item in navList"
          :key="item.label"
          :to="item.path"
          class="text-base font-bold transition-colors"
          :class="item.active
            ? 'text-blue-600'
            : 'text-slate-600 hover:text-slate-900'"
        >
          {{ item.label }}
        </router-link>
      </nav>

      <!-- 中右：全局搜索（胶囊状） -->
      <div class="hidden lg:block flex-1 max-w-xs mx-6">
        <div
          class="group flex items-center gap-2 rounded-full bg-slate-100/80
                 px-4 py-2 ring-1 ring-slate-200/70 transition-all
                 focus-within:bg-white focus-within:ring-blue-400/60
                 focus-within:shadow-md focus-within:shadow-blue-500/10"
        >
          <!-- 搜索 Icon -->
          <svg
            class="h-4 w-4 shrink-0 text-slate-400 transition-colors group-focus-within:text-blue-500"
            fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
          >
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 11-13 0 6.5 6.5 0 0113 0z" />
          </svg>

          <input
            v-model="keyword"
            type="text"
            placeholder="搜索竞赛…"
            class="w-full min-w-0 bg-transparent text-sm font-medium text-slate-800
                   placeholder:text-slate-400 focus:outline-none"
            :disabled="searching"
            @keyup.enter="onSearch"
          />
        </div>
      </div>

      <!-- 右：登录/注册 -->
      <div class="flex items-center gap-3">
        <button
          class="px-4 py-2 text-sm font-bold text-slate-600 hover:text-slate-900 transition-colors"
        >
          登录
        </button>
        <button
          class="px-5 py-2 text-sm font-bold text-white rounded-full
                 bg-blue-600 hover:bg-blue-700 shadow-md shadow-blue-600/25
                 transition-colors"
        >
          注册
        </button>
      </div>
    </div>
  </header>
</template>

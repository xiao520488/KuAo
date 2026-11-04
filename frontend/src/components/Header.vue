<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { authState, logout } from '@/services/auth'
import { authModalState, openAuthModal } from '@/composables/useAuthModal'

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

/* ---------------- 回到首页（Logo / 首页共用） ---------------- */
/**
 * 统一重置逻辑：回到首页初始态 + 平滑回顶部
 * 1) 已在首页 → 只清掉 ?q= 等查询参数，让 HomeView 的 watch 恢复默认热门列表
 *    （此时用 replace 而非 push，避免在历史栈里堆一条无意义的记录）
 * 2) 在其他页面 → 正常跳转 / 并清空查询参数
 * 3) 无论哪条路径，最后都平滑滚到页面最顶部
 */
function goHome() {
  const onHome = route.path === '/'

  if (onHome) {
    const hasQuery = Object.keys(route.query).length > 0
    // 无 query 时不重复导航，直接滚动即可（避免冗余导航报错）
    if (hasQuery) {
      router.replace({ path: '/', query: {} }).then(scrollToTop).catch(() => scrollToTop())
      return
    }
    scrollToTop()
    return
  }

  router.push({ path: '/', query: {} }).then(scrollToTop).catch(() => scrollToTop())
}

/** 平滑滚动到页面最顶部（兼容不支持 smooth 的老浏览器） */
function scrollToTop() {
  if (typeof window === 'undefined') return
  if (typeof window.scrollTo === 'function') {
    try {
      window.scrollTo({ top: 0, behavior: 'smooth' })
      return
    } catch (e) {
      // 部分老浏览器不支持对象参数，落到下面的降级写法
    }
    window.scrollTo(0, 0)
  }
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
  if (item.path === '/') {
    return current === '/'
  }
  return current === item.path || current.startsWith(item.path + '/')
}

// 供模板使用的带高亮状态的列表
const navList = computed(() =>
  navItems.map((item) => ({ ...item, active: isActive(item) }))
)

/* ---------------- 登录 / 注册 ---------------- */
// 弹窗宿主已上移到 App.vue，Header 只负责触发打开

const user = computed(() => authState.user)
const isLoggedIn = computed(() => !!authState.user)

function openAuth(mode = 'login') {
  openAuthModal(mode)
}

// 登录/注册成功后的提示（轻量 toast）
const toast = ref('')
let toastTimer = null
function showToast(msg) {
  toast.value = msg
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => (toast.value = ''), 2200)
}

// 弹窗宿主在 App.vue，这里改为监听登录态变化来做本地提示
// 首次恢复会话（刷新页面）时不打扰用户，只提示「本次操作」带来的登录
watch(
  () => authState.user,
  (u, old) => {
    if (u && !old) showToast(`欢迎，${u?.username || '用户'}`)
  }
)

/* ---------------- 用户下拉菜单 ---------------- */
const menuOpen = ref(false)
const userMenuRef = ref(null)

function toggleMenu() {
  menuOpen.value = !menuOpen.value
}

function onDocClick(e) {
  if (!menuOpen.value) return
  if (userMenuRef.value && !userMenuRef.value.contains(e.target)) {
    menuOpen.value = false
  }
}

async function onLogout() {
  menuOpen.value = false
  await logout()
  showToast('已退出登录')
  router.push('/')
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  clearTimeout(toastTimer)
})
</script>

<template>
  <header
    class="fixed top-0 inset-x-0 z-50 h-16
           bg-white/60 backdrop-blur-xl backdrop-saturate-150
           border-b border-white/40 shadow-sm shadow-slate-900/5"
  >
    <div class="mx-auto h-full max-w-7xl px-6 flex items-center justify-between">
      <!-- 左：Logo -->
      <button
        type="button"
        class="text-2xl font-extrabold tracking-tight text-slate-900 select-none
               cursor-pointer bg-transparent border-0 p-0
               focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded-lg"
        aria-label="回到首页"
        @click="goHome"
      >
        库<span class="text-blue-600">奥</span>
      </button>

      <!-- 中：居中菜单 -->
      <nav class="hidden md:flex items-center gap-8 absolute left-1/2 -translate-x-1/2">
        <template v-for="item in navList" :key="item.label">
          <!-- 首页：走统一重置逻辑（清 query + 平滑回顶） -->
          <button
            v-if="item.path === '/'"
            type="button"
            class="text-base font-bold transition-colors cursor-pointer bg-transparent border-0 p-0
                   focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded"
            :class="item.active
              ? 'text-blue-600'
              : 'text-slate-600 hover:text-slate-900'"
            @click="goHome"
          >
            {{ item.label }}
          </button>

          <!-- 其他导航项：保持原有 router-link 行为 -->
          <router-link
            v-else
            :to="item.path"
            class="text-base font-bold transition-colors"
            :class="item.active
              ? 'text-blue-600'
              : 'text-slate-600 hover:text-slate-900'"
          >
            {{ item.label }}
          </router-link>
        </template>
      </nav>

      <!-- 中右：全局搜索（胶囊状） -->
      <div class="hidden lg:block flex-1 max-w-xs mx-6">
        <div
          class="group flex items-center gap-2 rounded-full bg-slate-100/80
                 px-4 py-2 ring-1 ring-slate-200/70 transition-all
                 focus-within:bg-white focus-within:ring-blue-500/40"
        >
          <svg class="h-4 w-4 shrink-0 text-slate-400" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="2" stroke-linecap="round"
               stroke-linejoin="round">
            <path
              stroke-linecap="round"
              d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 11-13 0 6.5 6.5 0 0113 0z" />
          </svg>

          <input
            v-model="keyword"
            type="text"
            placeholder="搜索..."
            class="w-full min-w-0 bg-transparent text-sm font-medium text-slate-800
                   placeholder:text-slate-400 focus:outline-none"
            :disabled="searching"
            @keyup.enter="onSearch"
          />
        </div>
      </div>

      <!-- 右：登录/注册 或 用户菜单 -->
      <div class="flex items-center gap-3">
        <!-- 未登录 -->
        <template v-if="!isLoggedIn">
          <button
            class="px-4 py-2 text-sm font-bold text-slate-600 hover:text-slate-900 transition-colors"
            @click="openAuth('login')"
          >
            登录
          </button>
          <button
            class="px-5 py-2 text-sm font-bold text-white rounded-full
                   bg-blue-600 hover:bg-blue-700 shadow-md shadow-blue-600/25
                   transition-colors"
            @click="openAuth('register')"
          >
            注册
          </button>
        </template>

        <!-- 已登录：头像 + 下拉 -->
        <div v-else ref="userMenuRef" class="relative">
          <button
            class="flex items-center gap-2 rounded-full pl-1 pr-3 py-1
                   hover:bg-slate-100 transition-colors"
            @click.stop="toggleMenu"
          >
            <span
              class="grid h-8 w-8 place-items-center rounded-full bg-blue-600
                     text-sm font-bold text-white select-none"
            >
              {{ (user?.username || 'U').charAt(0).toUpperCase() }}
            </span>
            <span class="max-w-[8rem] truncate text-sm font-bold text-slate-700">
              {{ user?.username }}
            </span>
            <svg
              class="h-4 w-4 text-slate-400 transition-transform"
              :class="menuOpen ? 'rotate-180' : ''"
              fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6" />
            </svg>
          </button>

          <!-- 下拉面板 -->
          <Transition
            enter-active-class="transition duration-150 ease-out"
            enter-from-class="opacity-0 -translate-y-1"
            enter-to-class="opacity-100 translate-y-0"
            leave-active-class="transition duration-100 ease-in"
            leave-from-class="opacity-100 translate-y-0"
            leave-to-class="opacity-0 -translate-y-1"
          >
            <div
              v-if="menuOpen"
              class="absolute right-0 mt-2 w-44 overflow-hidden rounded-xl
                     bg-white p-1.5 shadow-xl shadow-slate-900/10 ring-1 ring-slate-900/5"
            >
              <div class="px-3 py-2">
                <p class="truncate text-sm font-bold text-slate-800">{{ user?.username }}</p>
                <p class="truncate text-xs text-slate-400">{{ user?.email || '已登录' }}</p>
              </div>
              <div class="my-1 h-px bg-slate-100" />
              <router-link
                to="/preparation"
                class="block rounded-lg px-3 py-2 text-sm font-medium text-slate-600
                       hover:bg-slate-50 hover:text-slate-900"
                @click="menuOpen = false"
              >
                我的备赛
              </router-link>
              <button
                class="block w-full rounded-lg px-3 py-2 text-left text-sm font-medium
                       text-red-500 hover:bg-red-50"
                @click="onLogout"
              >
                退出登录
              </button>
            </div>
          </Transition>
        </div>
      </div>
    </div>

    <!-- 轻量提示 -->
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-2"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-2"
    >
      <div
        v-if="toast"
        class="fixed left-1/2 top-20 z-[110] -translate-x-1/2 rounded-full
               bg-slate-900/90 px-4 py-2 text-sm font-bold text-white shadow-lg"
      >
        {{ toast }}
      </div>
    </Transition>
  </header>
</template>

# -*- coding: utf-8 -*-
"""Header.vue：新增手机端汉堡菜单 + 侧边抽屉（全部用纯 ASCII 锚点定位）"""
import io

PATH = 'src/components/Header.vue'
text = io.open(PATH, encoding='utf-8').read()


def rep(old, new, tag):
    global text
    assert text.count(old) == 1, 'anchor not unique: ' + tag
    text = text.replace(old, new, 1)
    print('[ok] ' + tag)


# ---------- 1) script：移动端抽屉状态与逻辑 ----------
rep(
    """onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)""",
    """/* ---------------- mobile drawer ---------------- */
const mobileOpen = ref(false)

function toggleMobile() {
  mobileOpen.value = !mobileOpen.value
}

function closeMobile() {
  mobileOpen.value = false
}

// \u62bd\u5c49\u5185\u70b9\u5bfc\u822a\uff1a\u8df3\u8f6c\u540e\u5173\u95ed
function onMobileNav(item) {
  closeMobile()
  if (item.path === '/') {
    goHome()
  } else if (route.path !== item.path) {
    router.push(item.path).then(scrollToTop).catch(() => scrollToTop())
  }
}

// \u62bd\u5c49\u5185\u641c\u7d22\uff1a\u590d\u7528\u9876\u90e8\u641c\u7d22\u903b\u8f91\uff0c\u5e76\u5173\u95ed\u62bd\u5c49
function onMobileSearch() {
  const q = keyword.value.trim()
  if (!q || searching.value) return
  closeMobile()
  onSearch()
}

// \u62bd\u5c49\u6253\u5f00\u65f6\u9501\u5b9a\u9875\u9762\u6eda\u52a8
watch(mobileOpen, (open) => {
  if (typeof document === 'undefined') return
  document.body.style.overflow = open ? 'hidden' : ''
})

// \u8def\u7531\u53d8\u5316\u65f6\u81ea\u52a8\u5173\u95ed\u62bd\u5c49
watch(() => route.fullPath, () => {
  if (mobileOpen.value) mobileOpen.value = false
})

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)""",
    'script-logic',
)

rep(
    """  clearTimeout(toastTimer)
})""",
    """  clearTimeout(toastTimer)
  if (typeof document !== 'undefined') document.body.style.overflow = ''
})""",
    'unmount-cleanup',
)

# ---------- 2) 右侧容器改为更紧凑间距 ----------
rep(
    '<div class="flex items-center gap-3">',
    '<div class="flex items-center gap-2 sm:gap-3">',
    'right-container',
)

# ---------- 3) 右侧容器闭合后插入汉堡按钮 + 遮罩 + 抽屉 ----------
# 定位：Header 模板最末尾的 toast Transition 之前
rep(
    """    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-2"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-2"
    >
      <div
        v-if="toast\"""",
    """      <!-- mobile hamburger (below md) -->
      <button
        type="button"
        class="md:hidden grid h-10 w-10 shrink-0 place-items-center rounded-xl
               text-slate-700 hover:bg-slate-100 transition-colors"
        :aria-expanded="mobileOpen ? 'true' : 'false'"
        aria-label="Menu"
        @click.stop="toggleMobile"
      >
        <svg
          v-if="!mobileOpen"
          class="h-6 w-6"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M4 6h16M4 12h16M4 18h16" />
        </svg>
        <svg
          v-else
          class="h-6 w-6"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
    </div>

    <!-- mobile drawer mask -->
    <Transition
      enter-active-class="transition-opacity duration-200"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition-opacity duration-150"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="mobileOpen"
        class="fixed inset-0 z-40 bg-slate-900/30 backdrop-blur-sm md:hidden"
        @click="closeMobile"
      />
    </Transition>

    <!-- mobile drawer panel -->
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 -translate-y-3"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 -translate-y-3"
    >
      <div
        v-if="mobileOpen"
        class="fixed inset-x-0 top-16 z-40 mx-3 rounded-2xl border border-slate-200
               bg-white p-4 shadow-xl shadow-slate-900/10 md:hidden"
      >
        <div
          class="flex items-center gap-2 rounded-full bg-slate-100/80
                 px-4 py-2 ring-1 ring-slate-200/70
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
            placeholder="\u641c\u7d22\u7ade\u8d5b..."
            class="w-full min-w-0 bg-transparent text-sm text-slate-700
                   placeholder:text-slate-400 focus:outline-none"
            @keyup.enter="onMobileSearch"
          />
        </div>

        <nav class="mt-3 flex flex-col">
          <button
            v-for="item in mobileNavList"
            :key="item.path"
            type="button"
            class="rounded-xl px-4 py-3 text-left text-base font-bold transition-colors"
            :class="item.active
              ? 'bg-blue-50 text-blue-600'
              : 'text-slate-700 hover:bg-slate-100'"
            @click="onMobileNav(item)"
          >
            {{ item.label }}
          </button>
        </nav>

        <div class="mt-3 border-t border-slate-200 pt-3">
          <template v-if="!isLoggedIn">
            <button
              class="w-full rounded-xl px-4 py-3 text-left text-base font-bold
                     text-slate-700 hover:bg-slate-100 transition-colors"
              @click="closeMobile(); openAuth('login')"
            >
              \u767b\u5f55
            </button>
            <button
              class="mt-1 w-full rounded-xl px-4 py-3 text-left text-base font-bold
                     text-blue-600 hover:bg-blue-50 transition-colors"
              @click="closeMobile(); openAuth('register')"
            >
              \u6ce8\u518c
            </button>
          </template>
          <template v-else>
            <router-link
              to="/preparation"
              class="block w-full rounded-xl px-4 py-3 text-left text-base font-bold
                     text-slate-700 hover:bg-slate-100 transition-colors"
              @click="closeMobile"
            >
              \u6211\u7684\u5907\u8d5b
            </router-link>
            <button
              class="mt-1 w-full rounded-xl px-4 py-3 text-left text-base font-bold
                     text-red-500 hover:bg-red-50 transition-colors"
              @click="closeMobile(); onLogout()"
            >
              \u9000\u51fa\u767b\u5f55
            </button>
          </template>
        </div>
      </div>
    </Transition>

    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-2"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-2"
    >
      <div
        v-if="toast\"""",
    'hamburger+drawer',
)

# ---------- 4) mobileNavList ----------
rep(
    """const navList = computed(() =>
  navItems.map((item) => ({ ...item, active: isActive(item) }))
)""",
    """const navList = computed(() =>
  navItems.map((item) => ({ ...item, active: isActive(item) }))
)

// mobile drawer shares the same nav data (with active highlight)
const mobileNavList = navList""",
    'mobileNavList',
)

io.open(PATH, 'w', encoding='utf-8', newline='').write(text)
print('ALL DONE')

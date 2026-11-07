<script setup>
import { computed, onBeforeUnmount, onMounted } from 'vue'
import AuthModal from '@/components/AuthModal.vue'
import AboutModal from '@/components/AboutModal.vue'
import Toast from '@/components/Toast.vue'
import { authModalState, openAuthModal, closeAuthModal } from '@/composables/useAuthModal'
import { aboutModalState, closeAboutModal, openAboutModal } from '@/composables/useAboutModal'
import { showToast } from '@/composables/useToast'

/**
 * 根组件：路由出口 + 全局弹窗/轻提示宿主
 *
 * 为什么 AuthModal / Toast 放在这里而不是 Header：
 * - Header 只存在于具体页面里，而「未登录访问受保护路由」会被守卫阻断导航，
 *   此时 router-view 不渲染任何页面，Header 也就不存在，
 *   弹窗没有宿主组件可显示（表现为「被拦截但没弹窗」）。
 * - 挂到根组件后，弹窗与页面生命周期解耦，任何路由状态下都能弹出。
 */
const showAuth = computed({
  get: () => authModalState.visible,
  set: (v) => (v ? openAuthModal(authModalState.mode) : closeAuthModal())
})

const authMode = computed(() => authModalState.mode)

/** 「关于我们」弹窗：同样挂在根组件，保证任意路由下都能打开 */
const showAbout = computed({
  get: () => aboutModalState.visible,
  set: (v) => (v ? openAboutModal() : closeAboutModal())
})

/**
 * AI 每日配额用尽（后端返回 429）时的全局提示
 *
 * 文案由 api.js 的响应拦截器从后端 detail 透传而来，
 * 这里只做兜底：万一 detail 为空，给一句通用提示，绝不硬编码业务文案。
 */
const DEFAULT_QUOTA_MESSAGE = '今日 AI 次数已用完，请明天再试'

function handleQuotaExceeded(event) {
  const text = event?.detail?.message
  showToast((typeof text === 'string' && text.trim()) || DEFAULT_QUOTA_MESSAGE)
}

onMounted(() => {
  window.addEventListener('ai:quota-exceeded', handleQuotaExceeded)
})

onBeforeUnmount(() => {
  window.removeEventListener('ai:quota-exceeded', handleQuotaExceeded)
})
</script>

<template>
  <div class="min-h-full flex flex-col bg-slate-50 dark:bg-slate-900 text-slate-800 dark:text-slate-100">
    <router-view />

    <!-- 全局登录/注册弹窗：固定在根组件，不依赖具体页面 -->
    <AuthModal v-model="showAuth" :mode="authMode" />

    <!-- 全局「关于我们」弹窗：Header 用户下拉菜单触发 -->
    <AboutModal v-model="showAbout" />

    <!-- 全局轻提示：监听 ai:quota-exceeded 等事件后展示 -->
    <Toast />
  </div>
</template>

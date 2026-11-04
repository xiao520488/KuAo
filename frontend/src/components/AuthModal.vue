<script setup>
import { computed, ref, watch, nextTick, onBeforeUnmount } from 'vue'
import { login, register } from '@/services/auth'

/**
 * 登录 / 注册弹窗
 * 用法：<AuthModal v-model="showAuth" :mode="'login'" @success="..." />
 * - 通过 v-model 控制显隐
 * - mode 控制初始 Tab（'login' | 'register'）
 * - 登录/注册成功后 emit('success', user)
 */
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  mode: { type: String, default: 'login' }
})

const emit = defineEmits(['update:modelValue', 'success'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

/* ---------------- 表单状态 ---------------- */
const activeTab = ref('login') // 'login' | 'register'
const username = ref('')
const password = ref('')
const confirmPassword = ref('') // 仅注册时使用（前端本地校验，不提交后端）
const submitting = ref(false)
const errorMsg = ref('')
const usernameInput = ref(null)

const isRegister = computed(() => activeTab.value === 'register')

/* ---------------- 生命周期联动 ---------------- */
// 打开弹窗时重置表单并聚焦
watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      activeTab.value = props.mode === 'register' ? 'register' : 'login'
      resetForm()
      await nextTick()
      usernameInput.value?.focus()
    }
  }
)

// 切换 Tab 时清空错误，保留已填的用户名（体验更顺）
watch(activeTab, () => {
  errorMsg.value = ''
  password.value = ''
  confirmPassword.value = ''
})

function resetForm() {
  username.value = ''
  password.value = ''
  confirmPassword.value = ''
  errorMsg.value = ''
  submitting.value = false
}

function switchTab(tab) {
  if (submitting.value) return
  activeTab.value = tab
}

function close() {
  if (submitting.value) return
  visible.value = false
}

/* ---------------- 提交 ---------------- */
function validate() {
  const u = username.value.trim()
  if (!u) return '请输入用户名'
  if (!password.value) return '请输入密码'
  if (isRegister.value && password.value.length < 6) return '密码至少 6 位'
  if (isRegister.value && password.value !== confirmPassword.value) return '两次输入的密码不一致'
  return ''
}

async function onSubmit() {
  if (submitting.value) return

  errorMsg.value = validate()
  if (errorMsg.value) return

  submitting.value = true
  try {
    const payload = { username: username.value.trim(), password: password.value }
    const user = isRegister.value ? await register(payload) : await login(payload)
    visible.value = false
    emit('success', user)
  } catch (err) {
    errorMsg.value = err?.message || '操作失败，请稍后重试'
  } finally {
    submitting.value = false
  }
}

/* ---------------- ESC 关闭 + 锁定滚动 ---------------- */
function onKeydown(e) {
  if (e.key === 'Escape' && props.modelValue) close()
}

if (typeof window !== 'undefined') {
  window.addEventListener('keydown', onKeydown)
}

watch(
  () => props.modelValue,
  (open) => {
    if (typeof document === 'undefined') return
    document.body.style.overflow = open ? 'hidden' : ''
  }
)

onBeforeUnmount(() => {
  if (typeof window !== 'undefined') window.removeEventListener('keydown', onKeydown)
  if (typeof document !== 'undefined') document.body.style.overflow = ''
})
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="visible"
        class="fixed inset-0 z-[100] flex items-center justify-center p-4"
        role="dialog"
        aria-modal="true"
      >
        <!-- 遮罩 -->
        <div class="absolute inset-0 bg-slate-900/40 backdrop-blur-sm" @click="close" />

        <!-- 面板 -->
        <div
          class="relative w-full max-w-md rounded-2xl bg-white p-7
                 shadow-2xl shadow-slate-900/20 ring-1 ring-slate-900/5"
        >
          <!-- 关闭 -->
          <button
            type="button"
            class="absolute right-4 top-4 rounded-full p-1.5 text-slate-400
                   hover:bg-slate-100 hover:text-slate-600 transition-colors"
            aria-label="关闭"
            @click="close"
          >
            <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>

          <!-- 标题 -->
          <h2 class="text-xl font-extrabold tracking-tight text-slate-900">
            {{ isRegister ? '创建账号' : '欢迎回来' }}
          </h2>
          <p class="mt-1 text-sm text-slate-500">
            {{ isRegister ? '注册后即可使用 AI 备赛助手' : '登录后继续你的备赛计划' }}
          </p>

          <!-- Tab 切换 -->
          <div class="mt-5 grid grid-cols-2 gap-1 rounded-xl bg-slate-100 p-1">
            <button
              type="button"
              class="rounded-lg py-2 text-sm font-bold transition-colors"
              :class="!isRegister ? 'bg-white text-blue-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'"
              @click="switchTab('login')"
            >
              登录
            </button>
            <button
              type="button"
              class="rounded-lg py-2 text-sm font-bold transition-colors"
              :class="isRegister ? 'bg-white text-blue-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'"
              @click="switchTab('register')"
            >
              注册
            </button>
          </div>

          <!-- 表单 -->
          <form class="mt-5 space-y-4" @submit.prevent="onSubmit">
            <div>
              <label class="mb-1.5 block text-sm font-bold text-slate-700">用户名</label>
              <input
                ref="usernameInput"
                v-model="username"
                type="text"
                autocomplete="username"
                placeholder="请输入用户名"
                class="w-full rounded-xl border border-slate-200 bg-slate-50/60 px-4 py-2.5
                       text-sm font-medium text-slate-800 placeholder:text-slate-400
                       outline-none transition-all
                       focus:border-blue-400 focus:bg-white focus:ring-2 focus:ring-blue-500/20"
                :disabled="submitting"
              />
            </div>

            <div>
              <label class="mb-1.5 block text-sm font-bold text-slate-700">密码</label>
              <input
                v-model="password"
                type="password"
                :autocomplete="isRegister ? 'new-password' : 'current-password'"
                placeholder="请输入密码"
                class="w-full rounded-xl border border-slate-200 bg-slate-50/60 px-4 py-2.5
                       text-sm font-medium text-slate-800 placeholder:text-slate-400
                       outline-none transition-all
                       focus:border-blue-400 focus:bg-white focus:ring-2 focus:ring-blue-500/20"
                :disabled="submitting"
              />
            </div>

            <div v-if="isRegister">
              <label class="mb-1.5 block text-sm font-bold text-slate-700">确认密码</label>
              <input
                v-model="confirmPassword"
                type="password"
                autocomplete="new-password"
                placeholder="请再次输入密码"
                class="w-full rounded-xl border border-slate-200 bg-slate-50/60 px-4 py-2.5
                       text-sm font-medium text-slate-800 placeholder:text-slate-400
                       outline-none transition-all
                       focus:border-blue-400 focus:bg-white focus:ring-2 focus:ring-blue-500/20"
                :disabled="submitting"
              />
            </div>

            <!-- 错误提示 -->
            <p v-if="errorMsg" class="flex items-start gap-1.5 text-sm font-medium text-red-500">
              <svg class="mt-0.5 h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round"
                      d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
              </svg>
              <span>{{ errorMsg }}</span>
            </p>

            <!-- 提交 -->
            <button
              type="submit"
              class="w-full rounded-xl bg-blue-600 py-2.5 text-sm font-bold text-white
                     shadow-md shadow-blue-600/25 transition-colors
                     hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
              :disabled="submitting"
            >
              <span v-if="submitting" class="inline-flex items-center gap-2">
                <svg class="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                </svg>
                处理中…
              </span>
              <span v-else>{{ isRegister ? '注册并登录' : '登录' }}</span>
            </button>
          </form>

          <!-- 底部切换 -->
          <p class="mt-4 text-center text-sm text-slate-500">
            {{ isRegister ? '已有账号？' : '还没有账号？' }}
            <button
              type="button"
              class="font-bold text-blue-600 hover:text-blue-700"
              @click="switchTab(isRegister ? 'login' : 'register')"
            >
              {{ isRegister ? '去登录' : '立即注册' }}
            </button>
          </p>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

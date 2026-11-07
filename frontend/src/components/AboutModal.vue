<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { submitFeedback } from '@/services/api'

/**
 * 全局「关于我们」弹窗
 * 用法：<AboutModal v-model="showAbout" />
 * 内容包含平台介绍、暂未实现的规划，以及底部反馈提交区。
 */
const props = defineProps({
  modelValue: { type: Boolean, default: false }
})

const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

/* ---------------- 反馈表单状态 ---------------- */
const feedbackText = ref('')
const submitting = ref(false)
const feedbackError = ref('')
const feedbackDone = ref(false)
const textareaRef = ref(null)

const MAX_FEEDBACK_LENGTH = 1000

const canSubmit = computed(
  () => !submitting.value && feedbackText.value.trim().length > 0
)

function resetFeedback() {
  feedbackText.value = ''
  submitting.value = false
  feedbackError.value = ''
  feedbackDone.value = false
}

// 每次打开都回到干净的初始态，避免上一次的成功提示串场
watch(
  () => props.modelValue,
  async (open) => {
    if (!open) return
    resetFeedback()
    await nextTick()
    textareaRef.value?.focus?.()
  }
)

function close() {
  if (submitting.value) return
  visible.value = false
}

/** 用户重新输入时，撤掉上一次的成功 / 错误提示 */
function onFeedbackInput() {
  feedbackDone.value = false
  feedbackError.value = ''
}

/** 提交反馈：成功后就地提示「感谢你的反馈」并清空输入框 */
async function onSubmit() {
  if (submitting.value) return

  const text = feedbackText.value.trim()
  if (!text) {
    feedbackError.value = '请输入反馈内容'
    return
  }

  submitting.value = true
  feedbackError.value = ''
  feedbackDone.value = false

  try {
    await submitFeedback(text)
    feedbackDone.value = true
    feedbackText.value = ''
  } catch (err) {
    feedbackError.value = err?.message || '提交失败，请稍后重试'
  } finally {
    // 无论成功 / 失败都解锁按钮，避免弹窗长时间无法再次提交
    submitting.value = false
  }
}

/* ---------------- ESC 关闭 + 锁定页面滚动 ---------------- */
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
        aria-labelledby="about-modal-title"
      >
        <!-- 遮罩 -->
        <div class="absolute inset-0 bg-slate-900/40 backdrop-blur-sm" @click="close" />

        <!-- 面板 -->
        <div
          class="relative flex max-h-[88vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl
                 bg-white shadow-2xl shadow-slate-900/20 ring-1 ring-slate-900/5
                 dark:bg-slate-800 dark:ring-white/10"
        >
          <!-- 头部 -->
          <div class="shrink-0 border-b border-slate-100 px-6 pb-4 pt-5 dark:border-slate-700">
            <div class="flex items-start gap-3">
              <span class="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-600 text-base font-black text-white">
                库
              </span>
              <div class="min-w-0 flex-1">
                <h2 id="about-modal-title" class="text-lg font-extrabold tracking-tight text-slate-900 dark:text-slate-100">
                  关于我们
                </h2>
                <p class="mt-0.5 text-xs font-medium text-slate-400 dark:text-slate-500">
                  库奥 · KuAo
                </p>
              </div>
              <button
                type="button"
                class="rounded-full p-1.5 text-slate-500 transition-colors hover:bg-slate-100
                       hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-700/60 dark:hover:text-slate-200"
                aria-label="关闭"
                @click="close"
              >
                <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>

          <!-- 正文（可滚动） -->
          <div class="min-h-0 flex-1 overflow-y-auto px-6 py-5">
            <p class="text-sm leading-7 text-slate-600 dark:text-slate-300">
              感谢你使用库奥，这是我和我的Agent搭建的一个小平台，耗时一个星期，肯定有很多瑕疵，但后面会不断迭代。
            </p>

            <div class="mt-5 rounded-2xl bg-slate-50 px-4 py-4 dark:bg-slate-900/60">
              <p class="text-sm font-bold text-slate-700 dark:text-slate-200">
                目前还有一些想法暂未做：
              </p>
              <ol class="mt-2.5 list-decimal space-y-1.5 pl-5 text-sm leading-6 text-slate-600 dark:text-slate-300">
                <li>竞赛按报名时间远近排序；</li>
                <li>临近报名的竞赛卡片右侧加倒计时；</li>
                <li>把UI设计得更好；</li>
                <li>把用户系统做得更完善。</li>
              </ol>
            </div>

            <!-- 反馈提交 -->
            <form class="mt-6" @submit.prevent="onSubmit">
              <div class="flex items-center justify-between gap-3">
                <h3 class="text-sm font-bold text-slate-800 dark:text-slate-100">反馈提交</h3>
                <span class="text-[11px] font-medium text-slate-400 dark:text-slate-500">
                  {{ feedbackText.length }}/{{ MAX_FEEDBACK_LENGTH }}
                </span>
              </div>
              <p class="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                任何建议、Bug 或想做的新功能都可以告诉我们，感谢你的帮助。
              </p>

              <textarea
                ref="textareaRef"
                v-model="feedbackText"
                rows="4"
                :maxlength="MAX_FEEDBACK_LENGTH"
                :disabled="submitting"
                placeholder="说说你的建议或遇到的问题…"
                class="mt-3 w-full resize-none rounded-xl border border-slate-200 bg-slate-50/60 px-4 py-3
                       text-sm font-medium text-slate-800 outline-none transition-all placeholder:text-slate-400
                       focus:border-blue-400 focus:bg-white focus:ring-2 focus:ring-blue-500/20
                       disabled:cursor-not-allowed disabled:opacity-60
                       dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-100
                       dark:placeholder:text-slate-500 dark:focus:border-blue-500 dark:focus:bg-slate-800
                       dark:focus:ring-blue-400/30"
                @input="onFeedbackInput"
                @keydown.ctrl.enter.prevent="onSubmit"
              ></textarea>

              <!-- 成功提示 -->
              <p
                v-if="feedbackDone"
                class="mt-2.5 flex items-center gap-1.5 text-sm font-bold text-emerald-600 dark:text-emerald-400"
                role="status"
              >
                <svg class="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M20 6L9 17l-5-5" />
                </svg>
                感谢你的反馈
              </p>

              <!-- 错误提示 -->
              <p
                v-if="feedbackError"
                class="mt-2.5 flex items-start gap-1.5 text-sm font-medium text-rose-600 dark:text-rose-400"
                role="alert"
              >
                <svg class="mt-0.5 h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round"
                        d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                </svg>
                <span>{{ feedbackError }}</span>
              </p>

              <div class="mt-3 flex justify-end">
                <button
                  type="submit"
                  class="inline-flex h-10 items-center gap-2 rounded-full bg-blue-600 px-6 text-sm font-bold
                         text-white shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700
                         disabled:cursor-not-allowed disabled:opacity-60 dark:hover:bg-blue-600"
                  :disabled="!canSubmit"
                >
                  <svg v-if="submitting" class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                  </svg>
                  {{ submitting ? '提交中…' : '提交反馈' }}
                </button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

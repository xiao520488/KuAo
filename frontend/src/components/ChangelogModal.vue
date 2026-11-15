<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { getChangelogs } from '@/services/api'
import { renderMarkdown } from '@/services/markdown'

/**
 * 全局「库奥 · 更新日志」弹窗（原 AboutModal）
 * 用法：<ChangelogModal v-model="showChangelog" />
 *
 * 数据来自 GET /api/changelogs（后台 AdminView「更新日志管理」发布），
 * 正文按 Markdown 渲染：marked 转 HTML + DOMPurify 净化后 v-html。
 * 每次打开都重新拉一次，后台刚发布的内容立刻可见。
 */
const props = defineProps({
  modelValue: { type: Boolean, default: false }
})

const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

/* ---------------- 数据 ---------------- */
const items = ref([])
const loading = ref(false)
const error = ref('')

/** 列表项：把 Markdown 提前渲染成 HTML，模板里直接 v-html */
const rendered = computed(() =>
  items.value.map((item) => ({
    ...item,
    html: renderMarkdown(item.content_markdown || '')
  }))
)

async function loadLogs() {
  loading.value = true
  error.value = ''
  try {
    const data = await getChangelogs({ limit: 50 })
    items.value = Array.isArray(data?.items) ? data.items : []
  } catch (err) {
    error.value = err?.message || '更新日志加载失败'
  } finally {
    loading.value = false
  }
}

// 打开时拉数据（有缓存也刷新，保证刚发布的版本能看到）
watch(
  () => props.modelValue,
  (open) => {
    if (open) loadLogs()
  }
)

/* ---------------- ESC 关闭 + 锁定页面滚动 ---------------- */
function close() {
  visible.value = false
}

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
        aria-labelledby="changelog-modal-title"
      >
        <!-- 遮罩 -->
        <div class="absolute inset-0 bg-slate-900/40 backdrop-blur-sm" @click="close" />

        <!-- 面板 -->
        <div
          class="relative flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl
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
                <h2 id="changelog-modal-title" class="text-lg font-extrabold tracking-tight text-slate-900 dark:text-slate-100">
                  库奥 · 更新日志
                </h2>
                <p class="mt-0.5 text-xs font-medium text-slate-400 dark:text-slate-500">
                  每次迭代都记录在这里
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
            <!-- 加载骨架 -->
            <div v-if="loading" class="space-y-3">
              <div class="h-5 w-28 animate-pulse rounded-full bg-slate-100 dark:bg-slate-700/60"></div>
              <div class="h-3 w-full animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
              <div class="h-3 w-4/5 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
              <div class="h-3 w-2/3 animate-pulse rounded bg-slate-100 dark:bg-slate-700/60"></div>
            </div>

            <!-- 出错 -->
            <div
              v-else-if="error"
              class="rounded-xl border border-red-100 bg-red-50 px-4 py-3 text-sm font-medium text-red-600
                     dark:border-red-500/30 dark:bg-red-500/15 dark:text-red-400"
              role="alert"
            >
              {{ error }}
            </div>

            <!-- 空态 -->
            <div
              v-else-if="!rendered.length"
              class="rounded-xl border border-dashed border-slate-200 px-4 py-14 text-center
                     text-sm font-medium text-slate-500 dark:border-slate-700 dark:text-slate-400"
            >
              暂无更新
            </div>

            <!-- 日志列表 -->
            <div v-else class="space-y-7">
              <article
                v-for="(item, index) in rendered"
                :key="item.id"
                class="relative"
              >
                <!-- 版本行 -->
                <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span
                    class="inline-flex items-center rounded-full bg-blue-600 px-2.5 py-1
                           text-xs font-bold tracking-wide text-white shadow-sm shadow-blue-600/25"
                  >
                    {{ item.version }}
                  </span>
                  <span class="text-xs font-semibold text-slate-400 dark:text-slate-500">
                    {{ item.release_date }}
                  </span>
                  <span
                    v-if="index === 0"
                    class="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5
                           text-[11px] font-bold text-emerald-600
                           dark:bg-emerald-500/15 dark:text-emerald-400"
                  >
                    最新
                  </span>
                </div>

                <!-- Markdown 正文 -->
                <div
                  class="prose prose-slate mt-2 max-w-none text-sm dark:prose-invert
                         prose-headings:font-bold prose-h1:text-lg prose-h2:text-base
                         prose-h3:text-sm prose-p:text-sm prose-p:leading-7
                         prose-li:text-sm prose-li:my-0.5 prose-ul:my-2 prose-ol:my-2
                         prose-headings:mt-4 prose-headings:mb-2
                         prose-pre:rounded-xl prose-pre:text-xs
                         prose-code:text-blue-600 dark:prose-code:text-blue-400
                         prose-a:text-blue-600 dark:prose-a:text-blue-400"
                  v-html="item.html"
                ></div>

                <!-- 分隔线 -->
                <div
                  v-if="index < rendered.length - 1"
                  class="mt-6 border-t border-dashed border-slate-200 dark:border-slate-700"
                ></div>
              </article>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
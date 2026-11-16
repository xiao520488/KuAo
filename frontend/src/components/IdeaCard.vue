<script setup>
/**
 * 选题灵感卡片（灵感工作台 / 我的收藏 共用）
 *
 * 展开状态必须由「每张卡片自己」持有：
 * 早期把详情折叠放在父组件的共享变量里，点一张会让整列卡片的箭头一起翻转。
 * 这里用组件内部的 open，列表里有多少张卡片就有多少份独立状态，互不影响。
 */
import { ref } from 'vue'

const props = defineProps({
  item: { type: Object, required: true }
})

const emit = defineEmits(['toggle-favorite'])

// 只属于当前这张卡片
const open = ref(false)

function toggleOpen() {
  open.value = !open.value
}
</script>

<template>
  <article
    class="flex flex-col rounded-2xl bg-white p-5 ring-1 ring-slate-200/70 shadow-sm transition-all
           hover:shadow-lg hover:shadow-slate-900/10 dark:bg-slate-800 dark:ring-slate-700"
  >
    <span class="mb-3 w-fit rounded-full bg-violet-50 px-2.5 py-1 text-xs font-bold text-violet-700 ring-1 ring-violet-200 dark:bg-violet-500/15 dark:text-violet-300 dark:ring-violet-500/30">
      {{ item.tag }}
    </span>

    <h3 class="text-base font-black leading-snug text-slate-900 dark:text-slate-100">
      {{ item.title }}
    </h3>
    <p v-if="item.text" class="mt-1.5 text-sm leading-relaxed text-slate-700 dark:text-slate-200">
      {{ item.text }}
    </p>

    <div v-if="item.detailHtml" class="mt-3 rounded-xl bg-slate-50 px-3 py-2 dark:bg-slate-900/60">
      <button
        type="button"
        class="flex w-full items-center justify-between gap-2 text-left text-xs font-bold text-blue-600 dark:text-blue-400"
        :aria-expanded="open ? 'true' : 'false'"
        @click="toggleOpen"
      >
        <span>{{ open ? '收起方案细节' : '展开方案细节' }}</span>
        <svg
          class="h-4 w-4 shrink-0 transition-transform duration-200"
          :class="open ? 'rotate-180' : ''"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      <div v-show="open" class="markdown-body mt-2 text-sm" v-html="item.detailHtml"></div>
    </div>

    <p v-if="item.novelty" class="mt-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
      <span class="font-bold text-slate-700 dark:text-slate-200">差异化：</span>{{ item.novelty }}
    </p>
    <p v-if="item.risk" class="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
      <span class="font-bold text-slate-700 dark:text-slate-200">风险：</span>{{ item.risk }}
    </p>
    <p v-if="item.refs && item.refs.length" class="mt-2 text-[11px] font-medium text-slate-500 dark:text-slate-400">
      参考样本 ID：{{ item.refs.join('、') }}
    </p>

    <div class="mt-auto flex items-center justify-end pt-4">
      <button
        type="button"
        class="inline-flex items-center gap-2 rounded-full px-4 py-1.5 text-xs font-bold transition-colors"
        :class="item.favorite
          ? 'bg-amber-50 text-amber-700 ring-1 ring-amber-200 dark:bg-amber-500/15 dark:text-amber-300 dark:ring-amber-500/30'
          : 'text-slate-500 ring-1 ring-slate-200 hover:text-slate-900 hover:ring-slate-300 dark:text-slate-400 dark:ring-slate-700 dark:hover:text-white dark:hover:ring-slate-600'"
        @click="emit('toggle-favorite', props.item)"
      >
        <svg class="h-4 w-4" :fill="item.favorite ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M11.48 3.5l2.31 4.68 5.16.75-3.73 3.64.88 5.14-4.62-2.43-4.62 2.43.88-5.14L3.01 8.93l5.16-.75z" />
        </svg>
        {{ item.favorite ? '已收藏' : '收藏' }}
      </button>
    </div>
  </article>
</template>

<style scoped>
/* 卡内方案细节是 v-html 注入，后代选择器必须 :deep()，否则匹配不到内部节点 */
.markdown-body {
  font-size: 13px;
  line-height: 1.7;
  color: #334155;
}
:deep(.markdown-body > *:first-child) { margin-top: 0; }
:deep(.markdown-body > *:last-child) { margin-bottom: 0; }
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  font-weight: 700;
  color: #0f172a;
  line-height: 1.35;
  margin: 1em 0 0.5em;
}
.markdown-body :deep(h1) { font-size: 1.3em; }
.markdown-body :deep(h2) { font-size: 1.15em; }
.markdown-body :deep(h3) { font-size: 1.05em; }
.markdown-body :deep(h4) { font-size: 1em; }
.markdown-body :deep(p) { margin: 0.5em 0; }
.markdown-body :deep(ul),
.markdown-body :deep(ol) { margin: 0.5em 0; padding-left: 1.4em; }
.markdown-body :deep(ul) { list-style: disc; }
.markdown-body :deep(ol) { list-style: decimal; }
.markdown-body :deep(li) { margin: 0.2em 0; }
.markdown-body :deep(strong) { font-weight: 700; color: #0f172a; }
.markdown-body :deep(a) { color: #2563eb; text-decoration: underline; }
.markdown-body :deep(code) {
  background: #e2e8f0;
  border-radius: 4px;
  padding: 0.1em 0.35em;
  font-size: 0.9em;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.markdown-body :deep(pre) {
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 10px;
  padding: 12px 14px;
  overflow-x: auto;
  margin: 0.8em 0;
}
.markdown-body :deep(pre code) { background: transparent; padding: 0; color: inherit; }
.markdown-body :deep(blockquote) {
  border-left: 3px solid #93c5fd;
  color: #475569;
  margin: 0.7em 0;
  padding: 0.2em 0 0.2em 1em;
}
.markdown-body :deep(table) { width: 100%; border-collapse: collapse; margin: 0.7em 0; font-size: 0.95em; }
.markdown-body :deep(th),
.markdown-body :deep(td) { border: 1px solid #e2e8f0; padding: 6px 10px; text-align: left; }
.markdown-body :deep(th) { background: #f1f5f9; font-weight: 700; }
.markdown-body :deep(hr) { border: 0; border-top: 1px solid #e2e8f0; margin: 1.1em 0; }

/* 暗色模式 */
:global(.dark) .markdown-body { color: #cbd5e1; }
:global(.dark) .markdown-body :deep(h1),
:global(.dark) .markdown-body :deep(h2),
:global(.dark) .markdown-body :deep(h3),
:global(.dark) .markdown-body :deep(h4),
:global(.dark) .markdown-body :deep(strong) { color: #f1f5f9; }
:global(.dark) .markdown-body :deep(a) { color: #60a5fa; }
:global(.dark) .markdown-body :deep(code) { background: #334155; color: #e2e8f0; }
:global(.dark) .markdown-body :deep(pre) { background: #020617; color: #e2e8f0; border: 1px solid #334155; }
:global(.dark) .markdown-body :deep(pre code) { background: transparent; color: inherit; }
:global(.dark) .markdown-body :deep(blockquote) { border-left-color: #3b82f6; color: #94a3b8; }
:global(.dark) .markdown-body :deep(th),
:global(.dark) .markdown-body :deep(td) { border-color: #334155; }
:global(.dark) .markdown-body :deep(th) { background: #1e293b; color: #f1f5f9; }
:global(.dark) .markdown-body :deep(hr) { border-top-color: #334155; }
</style>
<script setup>
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { authState } from '../services/auth'
import { addPreparation, removePreparation } from '../services/api'
import { openAuthModal } from '../composables/useAuthModal'

// 通用竞赛卡片：整卡/详情 -> 详情页，备赛 -> 备赛列表页
const props = defineProps({
  // 竞赛 ID，用于跳转详情页
  id: { type: [String, Number], default: null },
  title: { type: String, default: '竞赛名称' },
  image: { type: String, default: '' },
  // 报名时间
  signUpTime: { type: String, default: '待公布' },
  // 比赛时间
  contestTime: { type: String, default: '待公布' },
  // 竞赛级别标签，可选展示
  level: { type: String, default: '' },
  // 竞赛分类（学科/方向），可选展示
  category: { type: String, default: '' },
  // 是否隐藏"加入备赛"按钮（备赛列表页使用）
  hidePrepBtn: { type: Boolean, default: false },
  // 是否显示极简"移出备赛"按钮（备赛列表页使用，互斥于加入按钮）
  showRemoveBtn: { type: Boolean, default: false },
  // 当前用户是否已加入备赛（由父组件批量拉取后下发）
  prepared: { type: Boolean, default: false }
})

const emit = defineEmits(['preparation-change'])

const router = useRouter()

// 整卡点击 -> 竞赛详情
function goDetail() {
  if (props.id !== null && props.id !== undefined) {
    router.push({ name: 'competition-detail', params: { id: props.id } })
  }
}

// 本地备赛状态：初始由父组件下发，之后本地乐观更新
const isPrepared = ref(props.prepared)
const pending = ref(false)

watch(
  () => props.prepared,
  (v) => {
    isPrepared.value = v
  }
)

// 加入 / 移出备赛（未登录先弹登录框）
async function togglePreparation() {
  if (props.id === null || props.id === undefined) return

  if (!authState.user) {
    openAuthModal('login')
    return
  }

  if (pending.value) return
  pending.value = true

  const next = !isPrepared.value
  try {
    if (next) {
      await addPreparation(props.id)
    } else {
      await removePreparation(props.id)
    }
    isPrepared.value = next
    emit('preparation-change', { id: props.id, prepared: next })
  } catch (err) {
    // 401 已由全局拦截器统一处理；其余错误保持原状态
    console.error('[CompetitionCard] toggle preparation failed:', err)
  } finally {
    pending.value = false
  }
}

// 备赛页专用：仅移出备赛，成功后由父组件刷新列表
async function removeOnly() {
  if (props.id === null || props.id === undefined) return

  if (!authState.user) {
    openAuthModal('login')
    return
  }

  if (pending.value) return
  pending.value = true

  try {
    await removePreparation(props.id)
    isPrepared.value = false
    emit('preparation-change', { id: props.id, prepared: false })
  } catch (err) {
    // 401 已由全局拦截器统一处理；其余错误保持原状态
    console.error('[CompetitionCard] remove preparation failed:', err)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <article
    role="link"
    tabindex="0"
    class="card-in group flex gap-7 overflow-hidden rounded-2xl bg-white dark:bg-slate-800 p-6 cursor-pointer
           ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm
           transition-all duration-200
           hover:shadow-lg hover:shadow-slate-900/10 hover:ring-blue-200"
    @click="goDetail"
    @keydown.enter.prevent="goDetail"
  >
    <!-- 左：固定宽高比图片占位 -->
    <div class="relative w-52 shrink-0">
      <div class="aspect-[4/3] w-full overflow-hidden rounded-xl bg-slate-100 dark:bg-slate-700/60">
        <img
          v-if="image"
          :src="image"
          alt=""
          class="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
        />
        <!-- 无图时的占位 -->
        <div
          v-else
          class="flex h-full w-full items-center justify-center
                 bg-gradient-to-br from-slate-100 to-slate-200"
        >
          <svg class="h-8 w-8 text-slate-300" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 0l2.909 2.909M18 6.75h.008v.008H18V6.75z" />
          </svg>
        </div>
      </div>
    </div>

    <!-- 右：内容区 -->
    <div class="flex min-w-0 flex-1 flex-col">
      <!-- 标题 -->
      <div class="flex items-start gap-2">
        <h3
          class="truncate text-xl font-bold text-slate-900 dark:text-slate-100 cursor-pointer
                 transition-colors duration-300 group-hover:text-blue-600"
          @click.stop="goDetail()"
        >
          {{ title }}
        </h3>
        <span
          v-if="level"
          class="mt-1 shrink-0 rounded-full bg-blue-50 dark:bg-blue-500/15 px-3 py-0.5
                 text-xs font-bold text-blue-600 dark:text-blue-400"
        >
          {{ level }}
        </span>
        <span
          v-if="category"
          class="mt-1 shrink-0 rounded-full bg-slate-100 dark:bg-slate-700/60 px-3 py-0.5 text-xs font-medium text-slate-500 dark:text-slate-400"
        >
          {{ category }}
        </span>
      </div>

      <!-- 中间：时间信息 -->
      <div class="mt-4 space-y-2 text-base text-slate-500 dark:text-slate-400">
        <p class="flex items-center gap-2">
          <svg class="h-[18px] w-[18px] shrink-0 text-slate-400" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M12 6v6l4 2m6-2a10 10 0 1 1-20 0 10 10 0 0 1 20 0z" />
          </svg>
          <span class="font-medium text-slate-400">报名时间</span>
          <span class="font-semibold text-slate-700 dark:text-slate-200">{{ signUpTime }}</span>
        </p>
        <p class="flex items-center gap-2">
          <svg class="h-[18px] w-[18px] shrink-0 text-slate-400" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M8 7V3m8 4V3M3.75 9h16.5M4.5 21h15a1.5 1.5 0 001.5-1.5V6a1.5 00-1.5-1.5h-15A1.5 003 6v13.5A1.5 004.5 21z" />
          </svg>
          <span class="font-medium text-slate-400">比赛时间</span>
          <span class="font-semibold text-slate-700 dark:text-slate-200">{{ contestTime }}</span>
        </p>
      </div>

      <!-- 右下角按钮组 -->
      <div class="mt-auto flex items-center justify-end gap-3 pt-5" @click.stop>
        <button
          type="button"
          class="rounded-full px-6 py-2 text-sm font-bold text-slate-600 dark:text-slate-300
                 ring-1 ring-slate-200 dark:ring-slate-700 hover:text-slate-900 dark:hover:text-white hover:ring-slate-300 dark:hover:ring-slate-600
                 transition-colors"
          @click="goDetail"
        >
          详情
        </button>
        <button
          v-if="!hidePrepBtn && !showRemoveBtn"
          type="button"
          :disabled="pending"
          class="rounded-full px-6 py-2 text-sm font-bold transition-colors disabled:opacity-60
                 disabled:cursor-not-allowed"
          :class="
            isPrepared
              ? 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 ring-1 ring-emerald-200 dark:ring-emerald-500/30 hover:bg-emerald-100'
              : 'bg-blue-600 text-white shadow-sm shadow-blue-600/25 hover:bg-blue-700 dark:hover:bg-blue-600'
          "
          @click="togglePreparation"
        >
          <span v-if="pending">处理中…</span>
          <span v-else-if="isPrepared">已加入备赛</span>
          <span v-else>加入备赛</span>
        </button>

        <!-- 备赛页极简移出：小垃圾桶图标 + 悬停转为危险色 -->
        <button
          v-if="showRemoveBtn"
          type="button"
          :disabled="pending"
          title="移出备赛"
          aria-label="移出备赛"
          class="inline-flex items-center gap-1.5 rounded-full px-3 py-2 text-sm font-medium
                 text-slate-400 ring-1 ring-slate-200 dark:ring-slate-700 transition-colors
                 hover:bg-rose-50 dark:hover:bg-rose-500/10 hover:text-rose-600 hover:ring-rose-200
                 disabled:opacity-50 disabled:cursor-not-allowed"
          @click.stop="removeOnly"
        >
          <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 00-7.5" />
          </svg>
          <span v-if="pending">移出中…</span>
          <span v-else>移出备赛</span>
        </button>
      </div>
    </div>
  </article>
</template>

<style scoped>
/* 卡片入场：缩小 + 淡入 → 正常尺寸
   首页通过 .card-in 复用本动画并叠加 animationDelay 做交错；
   动画本身定义在卡片内部，父组件无需关心实现细节。
   both = 动画开始前就应用 from 状态（延迟期间保持透明），结束后保持 to 状态 */
@keyframes cardPopIn {
  from {
    opacity: 0;
    transform: scale(0.94) translateY(16px);
  }
  to {
    opacity: 1;
    transform: scale(1) translateY(0);
  }
}

.card-in {
  animation: cardPopIn 0.55s cubic-bezier(0.22, 1, 0.36, 1) both;
  will-change: transform, opacity;
}

/* 系统开启「减弱动态效果」时直接呈现终态 */
@media (prefers-reduced-motion: reduce) {
  .card-in {
    animation: none;
    opacity: 1;
    transform: none;
  }
}
</style>

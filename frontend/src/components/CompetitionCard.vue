<script setup>
import { ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { authState } from '../services/auth'
import { addPreparation, removePreparation } from '../services/api'
import { openAuthModal } from '../composables/useAuthModal'
import { categoryTag } from '@/utils/competitionCategory'
import { coverFallback } from '@/utils/coverFallback'
import { countdownState } from '@/utils/timeFormat'

// 通用竞赛卡片：整卡/详情 -> 详情页，备赛 -> 备赛列表页
const props = defineProps({
  // 竞赛 ID，用于跳转详情页
  id: { type: [String, Number], default: null },
  title: { type: String, default: '竞赛名称' },
  image: { type: String, default: '' },
  // AI 提取的 2 字赛事简称：仅在没有真实封面 / 图片加载失败时展示
  shortName: { type: String, default: '' },
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
  prepared: { type: Boolean, default: false },
  // 日期轨原始值（'YYYY-MM-DD' / 'MM-DD'），仅用于右上角倒计时徽章；
  // 展示文案仍走 signUpTime / contestTime，二者互不影响。
  regStartDate: { type: String, default: '' },
  regEndDate: { type: String, default: '' },
  contestStartDate: { type: String, default: '' }
})

const emit = defineEmits(['preparation-change'])

const router = useRouter()

// 卡片标签统一展示“大类 · 细分”，原始长分类作为 title 提示
const categoryLabel = computed(() => categoryTag(props.category))

// 右上角倒计时：只在拿到精确日期时出现，纯月份数据保持安静（不猜日子）
const countdown = computed(() =>
  countdownState({
    reg_start_date: props.regStartDate,
    reg_end_date: props.regEndDate,
    contest_start_date: props.contestStartDate
  })
)

const COUNTDOWN_TONE = {
  // 7 天内截止：最刺眼的一档
  urgent: 'bg-gradient-to-r from-rose-500 to-orange-500 text-white shadow-rose-500/40',
  // 报名进行中
  open: 'bg-gradient-to-r from-amber-400 to-orange-500 text-white shadow-orange-500/40',
  // 还没开报
  soon: 'bg-gradient-to-r from-sky-500 to-blue-600 text-white shadow-blue-500/30',
  // 等开赛
  contest: 'bg-gradient-to-r from-indigo-500 to-violet-600 text-white shadow-indigo-500/30',
  // 已截止：灰掉，不抢注意力
  over: 'bg-white/85 text-slate-500 ring-1 ring-slate-300/70 dark:bg-slate-700/85 dark:text-slate-300 dark:ring-slate-600 shadow-slate-900/10'
}

const countdownClass = computed(() => (countdown.value ? COUNTDOWN_TONE[countdown.value.tone] : ''))

// 封面兜底：无图 / 图片加载失败时，用名称哈希色相 + 2 字简称占位（布局尺寸不变）
// 真实图片永远优先：只有 image 为空或 @error 后才渲染 fallback。
const imageFailed = ref(false)
const showImage = computed(() => Boolean(props.image) && !imageFailed.value)
const fallback = computed(() => coverFallback(props.title, props.shortName))

function onImageError() {
  imageFailed.value = true
}

// 父组件换了图片地址（如分页/刷新）时重新尝试加载
watch(
  () => props.image,
  () => {
    imageFailed.value = false
  }
)

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
    class="card-in group flex flex-col gap-5 overflow-hidden rounded-2xl bg-white dark:bg-slate-800 p-6 cursor-pointer
           sm:flex-row sm:gap-7
           ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm
           transition-all duration-200
           hover:shadow-lg hover:shadow-slate-900/10 hover:ring-blue-200"
    @click="goDetail"
    @keydown.enter.prevent="goDetail"
  >
    <!-- 左：16:9 封面位（随卡片宽度铺开，海报感）；真实图片一律 cover 裁切居中，绝不拉伸 -->
    <div class="relative w-full shrink-0 sm:w-[42%] sm:max-w-[430px] sm:self-center">
      <div class="cover-media aspect-[16/9] w-full overflow-hidden rounded-xl bg-slate-100 dark:bg-slate-700/60">
        <img
          v-if="showImage"
          :src="image"
          alt=""
          class="h-full w-full object-cover object-center transition-transform duration-300 group-hover:scale-105"
          @error="onImageError"
        />
        <!-- 无图 / 加载失败：按名称哈希生成淡彩渐变 + 2 字简称（无简称则首字） -->
        <div
          v-else
          class="cover-fallback"
          :style="{ '--cover-hue': fallback.hue }"
          aria-hidden="true"
        >
          <span
            class="cover-fallback__glyph"
            :class="{ 'cover-fallback__glyph--short': fallback.isShortName }"
          >{{ fallback.glyph }}</span>
        </div>
      </div>

      <!-- 倒计时徽章：浮在封面右上角（移动端即卡片右上角） -->
      <span
        v-if="countdown"
        class="countdown-badge pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1.5
               rounded-full px-3 py-1.5 text-xs font-bold tracking-wide shadow-lg backdrop-blur
               supports-[backdrop-filter]:bg-clip-padding"
        :class="countdownClass"
        :title="countdown.tone === 'over' ? '报名已结束' : '根据平台采集的官方日期实时计算'"
      >
        <span
          v-if="countdown.tone === 'urgent' || countdown.tone === 'open'"
          class="countdown-dot"
          :class="countdown.tone === 'urgent' ? 'countdown-dot--hot' : ''"
          aria-hidden="true"
        />
        <svg
          v-else
          class="h-3.5 w-3.5 shrink-0"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          stroke-width="2"
          aria-hidden="true"
        >
          <path stroke-linecap="round" stroke-linejoin="round"
                d="M12 6v6l4 2m6-2a10 10 0 1 1-20 0 10 10 0 0 1 20 0z" />
        </svg>
        <span class="tabular-nums">{{ countdown.text }}</span>
      </span>
    </div>

    <!-- 右：内容区 -->
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
          :title="category"
          class="mt-1 shrink-0 rounded-full bg-slate-100 dark:bg-slate-700/60 px-3 py-0.5 text-xs font-medium text-slate-500 dark:text-slate-400"
        >
          {{ categoryLabel }}
        </span>
      </div>

      <!-- 中间：时间信息 -->
      <div class="mt-4 space-y-2 text-base text-slate-500 dark:text-slate-400">
        <p class="flex items-center gap-2">
          <svg class="h-[18px] w-[18px] shrink-0 text-slate-500 dark:text-slate-400" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M12 6v6l4 2m6-2a10 10 0 1 1-20 0 10 10 0 0 1 20 0z" />
          </svg>
          <span class="font-medium text-slate-500 dark:text-slate-400">报名时间</span>
          <span class="font-semibold text-slate-700 dark:text-slate-200">{{ signUpTime }}</span>
        </p>
        <p class="flex items-center gap-2">
          <svg class="h-[18px] w-[18px] shrink-0 text-slate-500 dark:text-slate-400" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M8 7V3m8 4V3M3.75 9h16.5M4.5 21h15a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5h-15A1.5 1.5 0 003 6v13.5A1.5 1.5 0 004.5 21z" />
          </svg>
          <span class="font-medium text-slate-500 dark:text-slate-400">比赛时间</span>
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
                 text-slate-500 dark:text-slate-400 ring-1 ring-slate-200 dark:ring-slate-700 transition-colors
                 hover:bg-rose-50 dark:hover:bg-rose-500/10 hover:text-rose-600 hover:ring-rose-200
                 disabled:opacity-50 disabled:cursor-not-allowed"
          @click.stop="removeOnly"
        >
          <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
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

/* ---------------- 倒计时徽章 ---------------- */
.countdown-badge {
  /* 轻微上浮 + 描边高光，让胶囊从封面上“浮”起来 */
  box-shadow:
    0 8px 20px -6px rgb(15 23 42 / 0.35),
    inset 0 1px 0 rgb(255 255 255 / 0.35);
  animation: badgeIn 0.45s cubic-bezier(0.22, 1, 0.36, 1) both;
}

@keyframes badgeIn {
  from {
    opacity: 0;
    transform: translateY(-6px) scale(0.92);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

/* 呼吸光点：报名进行中就是“活着”的信号 */
.countdown-dot {
  position: relative;
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 9999px;
  background: currentColor;
}

.countdown-dot::after {
  content: '';
  position: absolute;
  inset: -3px;
  border-radius: 9999px;
  background: currentColor;
  opacity: 0.55;
  animation: dotPulse 1.6s ease-out infinite;
}

.countdown-dot--hot::after {
  animation-duration: 1s;
}

@keyframes dotPulse {
  0% {
    transform: scale(0.6);
    opacity: 0.6;
  }
  70% {
    transform: scale(1.9);
    opacity: 0;
  }
  100% {
    transform: scale(1.9);
    opacity: 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .countdown-badge {
    animation: none;
  }
  .countdown-dot::after {
    animation: none;
    opacity: 0.25;
  }
}
</style>

<script setup>
import { useRouter } from 'vue-router'

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
  // 是否隐藏"备赛"按钮（备赛列表页使用）
  hidePrepBtn: { type: Boolean, default: false }
})

const router = useRouter()

// 整卡点击 -> 竞赛详情
function goDetail() {
  if (props.id !== null && props.id !== undefined) {
    router.push({ name: 'competition-detail', params: { id: props.id } })
  }
}

// 备赛按钮 -> 备赛列表页
function goPreparation() {
  router.push({ name: 'preparation' })
}
</script>

<template>
  <article
    role="link"
    tabindex="0"
    class="group flex gap-7 rounded-2xl bg-white p-6 cursor-pointer
           ring-1 ring-slate-200/70 shadow-sm
           transition-all duration-200
           hover:shadow-lg hover:shadow-slate-900/10 hover:ring-blue-200"
    @click="goDetail"
    @keydown.enter.prevent="goDetail"
  >
    <!-- 左：固定宽高比图片占位 -->
    <div class="relative w-52 shrink-0">
      <div class="aspect-[4/3] w-full overflow-hidden rounded-xl bg-slate-100">
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
                  d="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M18 6.75h.008v.008H18V6.75z" />
          </svg>
        </div>
      </div>
    </div>

    <!-- 右：内容区 -->
    <div class="flex min-w-0 flex-1 flex-col">
      <!-- 标题 -->
      <div class="flex items-start gap-2">
        <h3 class="truncate text-xl font-bold text-slate-900">
          {{ title }}
        </h3>
        <span
          v-if="level"
          class="mt-1 shrink-0 rounded-full bg-blue-50 px-3 py-0.5
                 text-xs font-bold text-blue-600"
        >
          {{ level }}
        </span>
        <span
          v-if="category"
          class="mt-1 shrink-0 rounded-full bg-slate-100 px-3 py-0.5 text-xs font-medium text-slate-500"
        >
          {{ category }}
        </span>
      </div>

      <!-- 中间：时间信息 -->
      <div class="mt-4 space-y-2 text-base text-slate-500">
        <p class="flex items-center gap-2">
          <svg class="h-[18px] w-[18px] shrink-0 text-slate-400" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M12 6v6l4 2m6-2a10 10 0 11-20 0 10 10 0 0120 0z" />
          </svg>
          <span class="font-medium text-slate-400">报名时间</span>
          <span class="font-semibold text-slate-700">{{ signUpTime }}</span>
        </p>
        <p class="flex items-center gap-2">
          <svg class="h-[18px] w-[18px] shrink-0 text-slate-400" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M8 7V3m8 4V3M3.75 9h16.5M4.5 21h15a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5h-15A1.5 1.5 0 003 6v13.5A1.5 1.5 0 004.5 21z" />
          </svg>
          <span class="font-medium text-slate-400">比赛时间</span>
          <span class="font-semibold text-slate-700">{{ contestTime }}</span>
        </p>
      </div>

      <!-- 右下角按钮组 -->
      <div class="mt-auto flex items-center justify-end gap-3 pt-5" @click.stop>
        <button
          type="button"
          class="rounded-full px-6 py-2 text-sm font-bold text-slate-600
                 ring-1 ring-slate-200 hover:text-slate-900 hover:ring-slate-300
                 transition-colors"
          @click="goDetail"
        >
          详情
        </button>
        <button
          v-if="!hidePrepBtn"
          type="button"
          class="rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white
                 shadow-sm shadow-blue-600/25 hover:bg-blue-700 transition-colors"
          @click="goPreparation"
        >
          备赛
        </button>
      </div>
    </div>
  </article>
</template>

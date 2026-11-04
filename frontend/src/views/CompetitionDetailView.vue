<script setup>
import { ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import Header from '@/components/Header.vue'
import { getCompetitionDetail } from '@/services/api'

const route = useRoute()

const detail = ref(null)
const loading = ref(true)
const errorMsg = ref('')

// 五个详情区块（先放静态占位文案，后续接真实数据替换 content 即可）
const sections = [
  {
    index: '一',
    title: '时间安排',
    content: '报名开始与截止时间、校内选拔时间、初赛与决赛时间节点，将在官方日程公布后同步更新。'
  },
  {
    index: '二',
    title: '参赛对象',
    content: '面向全日制在校本科生、研究生，可个人参赛或以团队形式报名，具体组队人数要求详见竞赛章程。'
  },
  {
    index: '三',
    title: '内容范围',
    content: '竞赛内容涵盖基础知识、综合应用与创新实践三部分，具体考核范围以后续发布的竞赛大纲为准。'
  },
  {
    index: '四',
    title: '考试安排',
    content: '包含初赛（线上/线下）、复赛与决赛环节，考试形式、时长与考场安排将在报名结束后通知。'
  },
  {
    index: '五',
    title: '奖励',
    content: '设置一等奖、二等奖、三等奖及优秀奖若干，获奖者可获得证书、奖品及综合素质加分。'
  }
]

// 从后端详情接口获取数据
async function loadDetail(id) {
  loading.value = true
  errorMsg.value = ''
  detail.value = null

  try {
    const data = await getCompetitionDetail(id)
    // 先展开原始数据，再用格式化字段兜底，避免 ...data 覆盖已归一化的值
    detail.value = {
      ...data,
      title: data.name || data.title || '未命名竞赛',
      signUpTime: data.sign_up_time || data.signUpTime || '待公布',
      contestTime: data.contest_time || data.contestTime || '待公布',
      image: data.image || data.cover || ''
    }
  } catch (err) {
    errorMsg.value = err?.message || '加载竞赛详情失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => loadDetail(route.params.id))

// 支持同组件内切换不同 id（例如返回列表再点另一张卡片）
watch(
  () => route.params.id,
  (id) => {
    if (id !== undefined && route.name === 'competition-detail') loadDetail(id)
  }
)
</script>

<template>
  <div class="min-h-screen bg-slate-50">
    <Header />

    <main class="mx-auto w-full max-w-6xl px-4 pb-24 pt-28 sm:px-6">
      <!-- 加载中 -->
      <div v-if="loading" class="space-y-6">
        <div class="h-64 w-full animate-pulse rounded-2xl bg-slate-200"></div>
        <div class="h-10 w-2/3 animate-pulse rounded-lg bg-slate-200"></div>
        <div class="h-5 w-1/2 animate-pulse rounded bg-slate-200"></div>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="errorMsg"
        class="rounded-2xl border border-dashed border-rose-200 bg-rose-50/40 px-6 py-16 text-center"
      >
        <p class="font-bold text-rose-500">{{ errorMsg }}</p>
        <button
          class="mt-6 rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white
                 shadow-sm shadow-blue-600/25 hover:bg-blue-700 transition-colors"
          @click="loadDetail(route.params.id)"
        >
          重新加载
        </button>
      </div>

      <!-- 正常内容 -->
      <template v-else>
        <!-- 1. 全宽竞赛大图占位 -->
<div class="mt-8 w-full"><div class="w-full h-64 bg-slate-50 border-2 border-dashed border-slate-300 rounded-2xl flex items-center justify-center overflow-hidden relative"><img v-if="detail?.image" :src="detail.image" class="w-full h-full object-cover" /><svg v-else class="w-16 h-16 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg></div></div>

        <!-- 2. 信息区：超大加粗标题 + 时间 -->
        <section class="mt-10 pb-2">
          <h1 class="text-3xl font-black leading-tight tracking-tight text-slate-900 sm:text-4xl">
            {{ detail?.title }}
          </h1>

          <div class="mt-6 flex flex-wrap items-center gap-x-10 gap-y-3 text-base">
            <p class="flex items-center gap-2">
              <span class="font-medium text-slate-400">报名时间</span>
              <span class="font-bold text-slate-800">{{ detail?.signUpTime }}</span>
            </p>
            <p class="flex items-center gap-2">
              <span class="font-medium text-slate-400">比赛时间</span>
              <span class="font-bold text-slate-800">{{ detail?.contestTime }}</span>
            </p>
          </div>
        </section>

        <!-- 3. 详情区：分割线 + 五个区块 -->
        <section class="mt-10 border-t border-slate-200 pt-8">
          <h2 class="text-xl font-bold text-slate-900">竞赛详情：</h2>

          <div class="mt-6 space-y-6">
            <div
              v-for="sec in sections"
              :key="sec.index"
              class="rounded-2xl bg-white p-6 ring-1 ring-slate-200/70 shadow-sm"
            >
              <h3 class="text-lg font-bold text-slate-900">
                {{ sec.index }}、{{ sec.title }}
              </h3>
              <p class="mt-3 text-sm leading-7 text-slate-600">
                {{ sec.content }}
              </p>
            </div>
          </div>
        </section>
      </template>
    </main>
  </div>
</template>

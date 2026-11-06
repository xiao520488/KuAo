<script setup>
import { ref, onMounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Header from '@/components/Header.vue'
import { getCompetitionDetail } from '@/services/api'

const route = useRoute()

const detail = ref(null)
const loading = ref(true)
const errorMsg = ref('')

// 四个基础详情区块（先放静态占位文案，后续接真实数据替换 content 即可）
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
  }
]

// 三个卡片区块（先放硬编码占位文案，后续接真实数据时替换 items 即可）
// tone 决定卡片的底色与描边，标题与序号统一用蓝色高亮
const featureCards = [
  {
    key: 'form',
    title: '参赛形式',
    tone: 'blue',
    items: [
      '个人赛 / 团队赛均可报名，团队赛每队 2–5 人，需指定 1 名队长。',
      '支持跨年级、跨专业组队，同一赛项每人仅可加入 1 支队伍。',
      '线上报名、线上提交，无需现场报到；决赛环节视赛项另行通知。'
    ]
  },
  {
    key: 'content',
    title: '竞赛内容',
    tone: 'teal',
    items: [
      '基础知识：考察赛项相关学科的核心概念、基本原理与常用方法。',
      '综合应用：围绕真实场景设计任务，考察方案设计与问题拆解能力。',
      '创新实践：提交原创作品或解决方案，按创新性与完成度综合评分。'
    ]
  },
  {
    key: 'submission',
    title: '作品提交要求',
    tone: 'amber',
    items: [
      '提交时间：以竞赛日程公布的截止时间为准，逾期系统自动关闭提交入口。',
      '文件格式：作品说明书为 PDF，源文件打包为 ZIP，单个附件不超过 200MB。',
      '命名规范：「赛项名称-队伍名称-作品名称」，压缩包内需附作品清单。'
    ]
  }
]

// 卡片配色：不同区块用不同底色/描边区分（暗色模式同步适配）
const cardTones = {
  blue: 'border-blue-200/80 bg-blue-50/70 dark:border-blue-400/25 dark:bg-blue-500/[0.08]',
  teal: 'border-teal-200/80 bg-teal-50/70 dark:border-teal-400/25 dark:bg-teal-500/[0.08]',
  amber: 'border-amber-200/80 bg-amber-50/70 dark:border-amber-400/25 dark:bg-amber-500/[0.08]'
}

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

// 参赛全流程攻略：Markdown 渲染 + XSS 净化
const renderedGuide = computed(() => {
  if (!detail.value?.full_process_guide) return ''
  return DOMPurify.sanitize(marked.parse(detail.value.full_process_guide))
})
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto w-full max-w-6xl px-4 pb-24 pt-28 sm:px-6">
      <!-- 加载中 -->
      <div v-if="loading" class="space-y-6">
        <div class="h-64 w-full animate-pulse rounded-2xl bg-slate-200 dark:bg-slate-700"></div>
        <div class="h-10 w-2/3 animate-pulse rounded-lg bg-slate-200 dark:bg-slate-700"></div>
        <div class="h-5 w-1/2 animate-pulse rounded bg-slate-200 dark:bg-slate-700"></div>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="errorMsg"
        class="rounded-2xl border border-dashed border-rose-200 dark:border-rose-500/30 bg-rose-50/40 dark:bg-rose-500/10 px-6 py-16 text-center"
      >
        <p class="font-bold text-rose-500">{{ errorMsg }}</p>
        <button
          class="mt-6 rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white
                 shadow-sm shadow-blue-600/25 hover:bg-blue-700 dark:hover:bg-blue-600 transition-colors"
          @click="loadDetail(route.params.id)"
        >
          重新加载
        </button>
      </div>

      <!-- 正常内容 -->
      <template v-else>
        <!-- 1. 全宽竞赛大图占位 -->
<div class="mt-8 w-full"><div class="w-full h-64 bg-slate-50 dark:bg-slate-900 border-2 border-dashed border-slate-300 dark:border-slate-600 rounded-2xl flex items-center justify-center overflow-hidden relative"><img v-if="detail?.image" :src="detail.image" class="w-full h-full object-cover" /><svg v-else class="w-16 h-16 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg></div></div>

        <!-- 2. 信息区：超大加粗标题 + 时间 -->
        <section class="mt-10 pb-2">
          <h1 class="text-3xl font-black leading-tight tracking-tight text-slate-900 dark:text-slate-100 sm:text-4xl">
            {{ detail?.title }}
          </h1>

          <div class="mt-6 flex flex-wrap items-center gap-x-10 gap-y-3 text-base">
            <p class="flex items-center gap-2">
              <span class="font-medium text-slate-400">报名时间</span>
              <span class="font-bold text-slate-800 dark:text-slate-100">{{ detail?.signUpTime }}</span>
            </p>
            <p class="flex items-center gap-2">
              <span class="font-medium text-slate-400">比赛时间</span>
              <span class="font-bold text-slate-800 dark:text-slate-100">{{ detail?.contestTime }}</span>
            </p>
            <a
              v-if="detail.official_url"
              :href="detail.official_url"
              target="_blank"
              rel="noopener noreferrer"
              class="ml-auto inline-flex items-center gap-1 rounded-full bg-blue-50 px-3.5 py-1.5 text-xs font-bold
                     text-blue-700 ring-1 ring-blue-200 transition-colors
                     hover:bg-blue-100 hover:text-blue-800 hover:ring-blue-300
                     dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30
                     dark:hover:bg-blue-500/25 dark:hover:text-blue-200"
            >
              查看官网
              <span aria-hidden="true">↗</span>
            </a>
          </div>
        </section>

        <!-- 3. 详情区：分割线 + 四个基础区块 + 三个卡片区块 -->
        <section class="mt-10 border-t border-slate-200 dark:border-slate-700 pt-8">
          <h2 class="text-xl font-bold text-slate-900 dark:text-slate-100">竞赛详情：</h2>

          <div class="mt-6 space-y-6">
            <div
              v-for="sec in sections"
              :key="sec.index"
              class="rounded-2xl bg-white dark:bg-slate-800 p-6 ring-1 ring-slate-200/70 dark:ring-slate-700 shadow-sm"
            >
              <h3 class="text-lg font-bold text-slate-900 dark:text-slate-100">
                {{ sec.index }}、{{ sec.title }}
              </h3>
              <p class="mt-3 text-sm leading-7 text-slate-600 dark:text-slate-300">
                {{ sec.content }}
              </p>
            </div>
          </div>

          <!-- 3.1 卡片区块：参赛形式 / 竞赛内容 / 作品提交要求（占位文案） -->
          <div class="mt-6 space-y-5">
            <article
              v-for="card in featureCards"
              :key="card.key"
              class="rounded-2xl border p-5 shadow-sm sm:p-6"
              :class="cardTones[card.tone]"
            >
              <div class="flex flex-col gap-4 sm:flex-row sm:gap-8">
                <!-- 左：蓝色高亮小标题 -->
                <div class="shrink-0 sm:w-28 lg:w-32">
                  <h3
                    class="flex items-center gap-2 text-sm font-bold tracking-wide
                           text-blue-600 dark:text-blue-400"
                  >
                    <span class="h-4 w-[3px] rounded-full bg-blue-500" aria-hidden="true"></span>
                    {{ card.title }}
                  </h3>
                </div>

                <!-- 右：带序号的详细说明 -->
                <ol class="min-w-0 flex-1 space-y-3">
                  <li v-for="(item, i) in card.items" :key="i" class="flex gap-3">
                    <span
                      class="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md
                             bg-blue-600/10 text-[11px] font-bold text-blue-600
                             dark:bg-blue-400/15 dark:text-blue-300"
                    >
                      {{ i + 1 }}
                    </span>
                    <p class="text-sm leading-6 text-slate-600 dark:text-slate-300">
                      {{ item }}
                    </p>
                  </li>
                </ol>
              </div>
            </article>
          </div>
        </section>

        <!-- 4. 参赛全流程攻略 -->
        <div v-if="detail.full_process_guide" class="mt-8 bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700 rounded-2xl p-6"><h3 class="text-xl font-bold text-slate-800 dark:text-slate-100 mb-4 flex items-center gap-2">🗺️ 参赛全流程攻略</h3><div class="prose prose-slate dark:prose-invert max-w-none text-slate-600 dark:text-slate-300" v-html="renderedGuide"></div></div><div v-else class="mt-8 text-center py-6 bg-slate-50 dark:bg-slate-800/30 rounded-2xl border border-dashed border-slate-300 dark:border-slate-700"><p class="text-slate-500 dark:text-slate-400">该赛事攻略正在收集中，敬请期待...</p></div>
      </template>
    </main>
  </div>
</template>

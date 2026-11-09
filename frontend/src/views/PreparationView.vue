<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import CompetitionCard from '@/components/CompetitionCard.vue'
import Header from '@/components/Header.vue'
import { getMyPreparations } from '@/services/api'

const router = useRouter()

// 备赛列表：来自后端 GET /api/preparations（需登录），字段经 mapToPrepCard 映射为卡片 props
const list = ref([])
const total = ref(0)
const loading = ref(true)
const errorMsg = ref('')

// 时间展示统一走 utils/timeFormat（日期轨优先，缺失降级月份轨）
import { formatSignUp, formatContest } from '../utils/timeFormat'

// 封面图：后端 API 的 image（由 DB 的 image_url 映射而来）
// 只接受 http(s) 绝对地址，避免相对路径 / 协议相对地址 / 空串打坏卡片占位图
function resolveImage(item) {
  const raw = item.image ?? item.cover ?? item.image_url ?? ''
  const url = typeof raw === 'string' ? raw.trim() : ''
  return /^https?:\/\//i.test(url) ? url : ''
}

const LEVEL_FALLBACK = {
  national: '国家级',
  provincial: '省级',
  municipal: '市级',
  regional: '区域级',
  international: '国际级'
}

// 备赛条目 -> 卡片 props；competition_id 即真实竞赛 ID，保证详情/工作台跳转有效
function mapToPrepCard(item) {
  if (!item || item.competition_id === null || item.competition_id === undefined) return null
  return {
    id: item.competition_id,
    title: item.name || '未命名竞赛',
    level: item.level_label || LEVEL_FALLBACK[item.level] || item.level || '',
    category: item.category || '',
    image: resolveImage(item),
    shortName: item.short_name || '',
    signUpTime: formatSignUp(item),
    contestTime: formatContest(item),
    // 日期轨原始值：只喂给倒计时徽章
    regStartDate: item.reg_start_date || '',
    regEndDate: item.reg_end_date || '',
    contestStartDate: item.contest_start_date || ''
  }
}

// 卡片移出备赛后：本地剔除该项并同步总数，避免整页重拉导致闪烁
function onPreparationChange({ id, prepared }) {
  if (prepared) return
  list.value = list.value.filter((item) => item.id !== id)
  total.value = Math.max(0, total.value - 1)
}

async function loadPreparations() {
  loading.value = true
  errorMsg.value = ''
  try {
    const { items, total: count } = await getMyPreparations()
    list.value = items.map(mapToPrepCard).filter(Boolean)
    total.value = count
  } catch (err) {
    errorMsg.value = err?.message || '加载备赛列表失败'
    list.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

onMounted(loadPreparations)

function goCompetitions() {
  router.push({ name: 'competitions' })
}

function goWorkspace(id) {
  router.push({ name: 'preparation-workspace', params: { id } })
}
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
    <Header />

    <main class="mx-auto max-w-5xl px-4 pb-24 pt-24 sm:px-6 sm:pt-28">
      <header class="mb-10">
        <h1 class="text-2xl font-bold text-slate-900 dark:text-slate-100 sm:text-3xl">我的备赛</h1>
        <p class="mt-2 text-sm text-slate-500 dark:text-slate-400">
          <template v-if="total > 0">
            共 {{ total }} 项竞赛，选择一项进入备赛工作台开始准备
          </template>
          <template v-else>选择一项竞赛，进入备赛工作台开始准备</template>
        </p>
      </header>

      <!-- 加载态 -->
      <div v-if="loading" class="flex items-center justify-center py-24 text-slate-500 dark:text-slate-400">
        <svg class="mr-3 h-5 w-5 animate-spin" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" class="opacity-20" />
          <path d="M22 12a10 10 0 0 1-10 10" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
        </svg>
        <span class="text-sm font-medium">正在加载备赛列表…</span>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="errorMsg"
        class="rounded-2xl border border-red-100 dark:border-red-500/30 bg-red-50 dark:bg-red-500/15 px-6 py-10 text-center"
      >
        <p class="text-sm font-semibold text-red-600 dark:text-red-400">{{ errorMsg }}</p>
        <button
          type="button"
          class="mt-4 rounded-full bg-red-600 px-5 py-2 text-sm font-bold text-white hover:bg-red-700 transition-colors"
          @click="loadPreparations"
        >
          重新加载
        </button>
      </div>

      <!-- 空态：未加入任何备赛 -->
      <div
        v-else-if="list.length === 0"
        class="rounded-2xl border border-dashed border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-6 py-20 text-center"
      >
        <div
          class="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-slate-100 dark:bg-slate-700/60"
        >
          <svg class="h-7 w-7 text-slate-500 dark:text-slate-400" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 6v12m6-6H6" />
          </svg>
        </div>
        <p class="font-bold text-slate-700 dark:text-slate-200">还没有加入任何备赛</p>
        <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">去竞赛列表挑一项感兴趣的，点击「加入备赛」即可开始</p>
        <button
          type="button"
          class="mt-6 rounded-full bg-blue-600 px-6 py-2 text-sm font-bold text-white
                 shadow-sm shadow-blue-600/25 transition-colors hover:bg-blue-700 dark:hover:bg-blue-600"
          @click="goCompetitions"
        >
          去浏览竞赛
        </button>
      </div>

      <!-- 列表 -->
      <div v-else class="grid grid-cols-1 gap-6">
        <div v-for="item in list" :key="item.id">
          <!-- 复用通用卡片：整卡/详情 -> 竞赛详情，备赛 -> 本页 -->
          <CompetitionCard
            v-bind="item"
            show-remove-btn
            prepared
            @preparation-change="onPreparationChange"
          />

          <!-- 独立入口：进入该竞赛的备赛工作台 -->
          <div class="mt-3 flex justify-end">
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-full bg-slate-900 px-5 py-2
                     text-sm font-bold text-white shadow-sm transition-colors hover:bg-slate-800"
              @click="goWorkspace(item.id)"
            >
              进入备赛工作台
              <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M13.5 4.5L21 12l-7.5 7.5M21 12H3" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

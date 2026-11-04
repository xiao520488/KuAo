<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import CompetitionCard from '@/components/CompetitionCard.vue'
import Header from '@/components/Header.vue'
import { getCompetitions } from '@/services/api'
import { mapToCard } from '@/services/competitionMapper'

const router = useRouter()

// 备赛列表：来自后端 /api/competitions，字段经 mapToCard 映射为卡片 props
const list = ref([])
const loading = ref(true)
const errorMsg = ref('')

async function loadCompetitions() {
  loading.value = true
  errorMsg.value = ''
  try {
    const data = await getCompetitions()
    // mapToCard 对缺失真实 id 的条目返回 null，这里直接过滤掉，避免出现假 id 的卡片
    list.value = (Array.isArray(data) ? data : []).map(mapToCard).filter(Boolean)
  } catch (err) {
    errorMsg.value = err?.message || '加载竞赛数据失败'
    list.value = []
  } finally {
    loading.value = false
  }
}

onMounted(loadCompetitions)

function goWorkspace(id) {
  router.push({ name: 'preparation-workspace', params: { id } })
}
</script>

<template>
  <div class="min-h-screen bg-slate-50">
    <Header />

    <main class="mx-auto max-w-5xl px-6 py-12">
      <header class="mb-10">
        <h1 class="text-3xl font-bold text-slate-900">我的备赛</h1>
        <p class="mt-2 text-sm text-slate-500">选择一项竞赛，进入备赛工作台开始准备</p>
      </header>

      <!-- 加载态 -->
      <div v-if="loading" class="flex items-center justify-center py-24 text-slate-400">
        <svg class="mr-3 h-5 w-5 animate-spin" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" class="opacity-20" />
          <path d="M22 12a10 10 0 0 1-10 10" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
        </svg>
        <span class="text-sm font-medium">正在加载竞赛列表…</span>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="errorMsg"
        class="rounded-2xl border border-red-100 bg-red-50 px-6 py-10 text-center"
      >
        <p class="text-sm font-semibold text-red-600">{{ errorMsg }}</p>
        <button
          type="button"
          class="mt-4 rounded-full bg-red-600 px-5 py-2 text-sm font-bold text-white hover:bg-red-700 transition-colors"
          @click="loadCompetitions"
        >
          重新加载
        </button>
      </div>

      <!-- 空态 -->
      <div
        v-else-if="list.length === 0"
        class="rounded-2xl border border-slate-200 bg-white px-6 py-16 text-center"
      >
        <p class="text-sm text-slate-400">暂无竞赛数据</p>
      </div>

      <!-- 列表 -->
      <div v-else class="flex flex-col gap-6">
        <div v-for="item in list" :key="item.id">
          <!-- 复用通用卡片：整卡/详情 -> 竞赛详情，备赛 -> 本页 -->
          <CompetitionCard v-bind="item" hide-prep-btn />

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

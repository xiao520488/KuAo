import fs from 'fs';
const p = 'src/views/HomeView.vue';
let s = fs.readFileSync(p, 'utf8');
s = s.replace(/\r\n/g, '\n');

/* ---------- 1) import：去掉 onMounted，保留 watch ---------- */
s = s.replace(
  `import { ref, onMounted, watch } from 'vue'`,
  `import { ref, watch, onBeforeUnmount } from 'vue'`
);
console.log('import:', s.includes('onBeforeUnmount') ? 'OK' : 'MISS');

/* ---------- 2) 重写 runSearch：requestId 竞态隔离 + finally 无条件复位 ---------- */
const oldRunSearch = `async function runSearch() {
  const q = keyword.value.trim()
  // 空输入：不请求，直接回到默认列表
  if (!q) {
    resetToDefault()
    return
  }
  if (searching.value) return   // 防抖：请求中忽略重复触发

  searching.value = true
  searchError.value = ''
  try {
    const res = await searchCompetitions(q)
    competitions.value = (res.items || []).map(mapToCard)
    searched.value = true
    // 检索后滚动到列表区，让用户立刻看到结果
    scrollToList()
  } catch (err) {
    searchError.value = err?.message || 'AI 检索失败，请稍后重试'
    competitions.value = []
    searched.value = true
  } finally {
    searching.value = false
  }
}`;

const newRunSearch = `/**
 * 请求序号：用于竞态隔离
 * 每次发起检索自增，只有"最后一次"请求的回调才允许改写 UI 状态，
 * 避免快速切换页面/重复触发时旧请求覆盖新结果。
 */
let requestId = 0

// 组件卸载标记：卸载后不再写响应式状态，防止幽灵更新
let unmounted = false

onBeforeUnmount(() => {
  unmounted = true
  requestId++          // 作废所有在途请求
  searching.value = false
})

/**
 * 执行 AI 检索
 * 注意：本函数不再做 searching 死锁拦截，重复调用由 watch 单一入口保证，
 *      竞态由 requestId 兜底，finally 无条件复位 searching。
 * @param {string} [rawQuery] 传入检索词；不传则取输入框当前值
 */
async function runSearch(rawQuery) {
  const q = String(rawQuery ?? keyword.value ?? '').trim()

  // 空输入：不请求，直接回到默认列表
  if (!q) {
    resetToDefault()
    return
  }

  const currentId = ++requestId
  searching.value = true
  searchError.value = ''

  try {
    const res = await searchCompetitions(q)

    // 已卸载或已被更新的请求取代 → 放弃本次结果
    if (unmounted || currentId !== requestId) return

    competitions.value = (res.items || []).map(mapToCard)
    searched.value = true
    // 检索后滚动到列表区，让用户立刻看到结果
    scrollToList()
  } catch (err) {
    if (unmounted || currentId !== requestId) return
    searchError.value = err?.message || 'AI 检索失败，请稍后重试'
    competitions.value = []
    searched.value = true
  } finally {
    // 无论成功 / 失败 / 被取代，只有"最后一次"请求负责收尾，
    // 保证 searching 不会因早退分支而残留为 true
    if (currentId === requestId) {
      searching.value = false
    }
  }
}`;

if (!s.includes(oldRunSearch)) console.log('MISS: runSearch 锚点');
else { s = s.replace(oldRunSearch, newRunSearch); console.log('OK: runSearch 已重写'); }

/* ---------- 3) 删除 onMounted + applyQuerySearch，改用单一 immediate watch ---------- */
const startMark = `onMounted(() => {`;
const endMark = `)
</script>`;
const si = s.indexOf(startMark);
const ei = s.indexOf(endMark, si);

if (si === -1 || ei === -1) {
  console.log('MISS: 生命周期块锚点');
} else {
  const newWatch = `/**
 * 唯一的数据入口：监听地址栏 ?q=
 * - immediate: true  → 组件挂载时立即执行一次，天然取代 onMounted 的判断逻辑
 * - 单一入口        → 避免"onMounted + watch"双触发导致的并发冲突
 *
 * 语义：
 *   q 有值  → 回填输入框并检索
 *   q 为空  → 加载默认热门列表（仅首次挂载时需要，之后由 resetToDefault 负责）
 */
watch(
  () => route.query.q,
  (q, oldQ) => {
    if (unmounted) return

    const val = typeof q === 'string' ? q.trim() : ''

    if (val) {
      keyword.value = val
      runSearch(val)
      return
    }

    // 无 q：首次挂载直接加载热门列表
    if (oldQ === undefined) {
      loadCompetitions()
      return
    }

    // 原本有 q、现在被清掉（如点 Logo 返回首页）→ 恢复正常列表
    resetToDefault()
  },
  { immediate: true }
)
</script>`;
  s = s.slice(0, si) + newWatch + s.slice(ei + `)
</script>`.length);
  console.log('OK: 已替换为 immediate watch');
}

fs.writeFileSync(p, s, 'utf8');
console.log('CRLF after:', (s.match(/\r\n/g) || []).length);

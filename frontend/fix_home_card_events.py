# -*- coding: utf-8 -*-
"""Fix HomeView: pass :id + prepared state + preparation-change to CompetitionCard."""
import io

PATH = 'src/views/HomeView.vue'
text = io.open(PATH, encoding='utf-8').read()

# --- 1) imports: add authState + checkPrepared ---
OLD_IMP = "import { getCompetitions, searchCompetitions } from '@/services/api'"
NEW_IMP = ("import { getCompetitions, searchCompetitions, checkPrepared } from '@/services/api'\n"
           "import { authState } from '@/services/auth'")
assert text.count(OLD_IMP) == 1, 'import anchor not unique'
text = text.replace(OLD_IMP, NEW_IMP, 1)

# --- 2) add preparedMap + loadPreparedStates + onPreparationChange before loadCompetitions ---
OLD_FN = "async function loadCompetitions() {"
NEW_FN = """// preparedMap: competition id -> whether current user already joined preparation
const preparedMap = ref({})

// Batch-query preparation state for the given list (not logged in -> all false)
async function loadPreparedStates(list) {
  if (!authState.user || !Array.isArray(list) || list.length === 0) {
    preparedMap.value = {}
    return
  }
  try {
    const results = await Promise.all(
      list.map(async (item) => {
        const id = item.id
        if (id === null || id === undefined) return [null, false]
        const ok = await checkPrepared(id)
        return [String(id), ok]
      })
    )
    const next = {}
    for (const [key, ok] of results) {
      if (key !== null) next[key] = ok
    }
    preparedMap.value = next
  } catch (err) {
    console.error('[HomeView] load prepared states failed:', err)
    preparedMap.value = {}
  }
}

// Card emits preparation-change -> keep local state in sync (no refetch)
function onPreparationChange({ id, prepared }) {
  if (id === null || id === undefined) return
  preparedMap.value = { ...preparedMap.value, [String(id)]: prepared }
}

async function loadCompetitions() {"""
assert text.count(OLD_FN) == 1, 'loadCompetitions anchor not unique'
text = text.replace(OLD_FN, NEW_FN, 1)

# --- 3) call loadPreparedStates after list assignment ---
OLD_ASG = """    competitions.value = list.slice(0, 8).map(mapToCard)
    bumpListAnimation()"""
NEW_ASG = """    competitions.value = list.slice(0, 8).map(mapToCard)
    bumpListAnimation()
    loadPreparedStates(competitions.value)"""
assert text.count(OLD_ASG) == 1, 'assignment anchor not unique'
text = text.replace(OLD_ASG, NEW_ASG, 1)

# --- 4) template: pass id / prepared / @preparation-change ---
OLD_TAG = """          :sign-up-time="item.signUpTime"
          :contest-time="item.contestTime"
        />"""
NEW_TAG = """          :sign-up-time="item.signUpTime"
          :contest-time="item.contestTime"
          :prepared="preparedMap[String(item.id)] === true"
          @preparation-change="onPreparationChange"
        />"""
assert text.count(OLD_TAG) == 1, 'template anchor not unique'
text = text.replace(OLD_TAG, NEW_TAG, 1)

# --- 5) add :id right after :key ---
OLD_KEY = """          :key="listAnimKey + '-' + item.id"
          class="card-in\""""
NEW_KEY = """          :key="listAnimKey + '-' + item.id"
          :id="item.id"
          class="card-in\""""
assert text.count(OLD_KEY) == 1, 'key anchor not unique'
text = text.replace(OLD_KEY, NEW_KEY, 1)

io.open(PATH, 'w', encoding='utf-8', newline='').write(text)
print('[ok] HomeView patched: id + prepared + preparation-change + loadPreparedStates')

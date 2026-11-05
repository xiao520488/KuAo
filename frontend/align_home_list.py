# -*- coding: utf-8 -*-
"""HomeView.vue: use space-y-6 like CompetitionsView so card width/spacing match exactly"""
import io

PATH = 'src/views/HomeView.vue'
text = io.open(PATH, encoding='utf-8').read()

OLD_SKELETON = '<div v-if="loading && !searching" class="grid grid-cols-1 gap-6">'
NEW_SKELETON = '<div v-if="loading && !searching" class="space-y-6">'

OLD_LIST = '<div v-else :key="listAnimKey" class="grid grid-cols-1 gap-6">'
NEW_LIST = '<div v-else :key="listAnimKey" class="space-y-6">'

assert text.count(OLD_SKELETON) == 1, 'skeleton anchor not unique'
assert text.count(OLD_LIST) == 1, 'list anchor not unique'

text = text.replace(OLD_SKELETON, NEW_SKELETON, 1)
text = text.replace(OLD_LIST, NEW_LIST, 1)

assert 'grid-cols-1' not in text, 'grid-cols-1 still present'

io.open(PATH, 'w', encoding='utf-8', newline='').write(text)
print('[ok] HomeView list -> space-y-6 (mirrors CompetitionsView)')

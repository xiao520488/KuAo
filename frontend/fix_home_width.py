# -*- coding: utf-8 -*-
"""HomeView.vue: drop max-w-4xl on list container so it fills the mb-6xl section,
matching CompetitionsView.vue card width exactly"""
import io

PATH = 'src/views/HomeView.vue'
text = io.open(PATH, encoding='utf-8').read()

OLD_SKELETON = '<div v-if="loading && !searching" class="mx-auto grid max-w-4xl grid-cols-1 gap-6">'
NEW_SKELETON = '<div v-if="loading && !searching" class="grid grid-cols-1 gap-6">'

OLD_LIST = '<div v-else :key="listAnimKey" class="mx-auto grid max-w-4xl grid-cols-1 gap-6">'
NEW_LIST = '<div v-else :key="listAnimKey" class="grid grid-cols-1 gap-6">'

assert text.count(OLD_SKELETON) == 1, 'skeleton anchor not unique'
assert text.count(OLD_LIST) == 1, 'list anchor not unique'

text = text.replace(OLD_SKELETON, NEW_SKELETON, 1)
text = text.replace(OLD_LIST, NEW_LIST, 1)

assert 'max-w-4xl grid grid-cols-1' not in text, 'list container still width-limited'

io.open(PATH, 'w', encoding='utf-8', newline='').write(text)
print('[ok] list container now fills max-w-6xl section (same as CompetitionsView)')

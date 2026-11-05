# -*- coding: utf-8 -*-
"""HomeView.vue: hot-competition list back to single wide column (PC included)"""
import io

PATH = 'src/views/HomeView.vue'
text = io.open(PATH, encoding='utf-8').read()

OLD_SKELETON = '<div v-if="loading && !searching" class="grid grid-cols-1 gap-6 md:grid-cols-2">'
NEW_SKELETON = '<div v-if="loading && !searching" class="mx-auto grid max-w-4xl grid-cols-1 gap-6">'

OLD_LIST = '<div v-else :key="listAnimKey" class="grid grid-cols-1 gap-6 md:grid-cols-2">'
NEW_LIST = '<div v-else :key="listAnimKey" class="mx-auto grid max-w-4xl grid-cols-1 gap-6">'

assert text.count(OLD_SKELETON) == 1, 'skeleton anchor not unique'
assert text.count(OLD_LIST) == 1, 'list anchor not unique'
assert text.count('md:grid-cols-2') == 2, 'unexpected md:grid-cols-2 count'

text = text.replace(OLD_SKELETON, NEW_SKELETON, 1)
text = text.replace(OLD_LIST, NEW_LIST, 1)

assert 'md:grid-cols-2' not in text, 'md:grid-cols-2 still present'

io.open(PATH, 'w', encoding='utf-8', newline='').write(text)
print('[ok] HomeView grid -> single column max-w-4xl')

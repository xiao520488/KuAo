# -*- coding: utf-8 -*-
import io
PATH = 'src/views/ProfileView.vue'
t = io.open(PATH, encoding='utf-8').read()

old = """function goPreparation() {
  router.push({ name: 'preparation' })
}

function goCompetitions() {
  router.push({ name: 'competitions' })
}

"""
assert t.count(old) == 1, t.count(old)
t = t.replace(old, '')
io.open(PATH, 'w', encoding='utf-8', newline='\n').write(t)

t = io.open(PATH, encoding='utf-8').read()
print('goPreparation:', t.count('goPreparation'), 'goCompetitions:', t.count('goCompetitions'))
print('router used:', t.count('router.'))
# div 配平（排除自闭合）
import re
open_div = len(re.findall(r'<div(?![^>]*/>)', t))
close_div = t.count('</div>')
print('div open/close:', open_div, close_div)

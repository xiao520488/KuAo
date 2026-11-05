# -*- coding: utf-8 -*-
import io, sys
sys.stdout.reconfigure(encoding='utf-8')
lines = io.open('src/components/Header.vue', encoding='utf-8').read().splitlines()
for i in range(264, 296):
    print(i+1, lines[i])
print('====== 250-266')
for i in range(249, 266):
    print(i+1, lines[i])

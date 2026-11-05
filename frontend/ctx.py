# -*- coding: utf-8 -*-
"""打印指定 token 所在行的上下文，便于人工判断"""
import io, re, sys
sys.stdout.reconfigure(encoding='utf-8')

targets = sys.argv[1:]
files = ['src/components/CompetitionCard.vue', 'src/components/AuthModal.vue',
         'src/views/HomeView.vue', 'src/views/CompetitionsView.vue',
         'src/views/CompetitionDetailView.vue', 'src/views/PreparationWorkspaceView.vue']

for P in files:
    for i, line in enumerate(io.open(P, encoding='utf-8').read().split('\n'), 1):
        for t in targets:
            if t in line:
                print('%-38s %4d | %s' % (P.split('/')[-1], i, line.strip()[:190]))
                break

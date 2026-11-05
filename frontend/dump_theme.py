# -*- coding: utf-8 -*-
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
for p in ['src/composables/useTheme.js', 'index.html']:
    print('=' * 20, p, '=' * 20)
    print(open(p, encoding='utf-8').read())

# -*- coding: utf-8 -*-
"""删除 patch_header.py 误插入的多余 </div>（第 392 行）"""
import io

PATH = 'src/components/Header.vue'
lines = io.open(PATH, encoding='utf-8').read().split('\n')

# 校验：391 行为 </button>，392 行为 </div>，393 行为空，394 行为 drawer mask 注释
assert lines[390].strip() == '</button>', lines[390]
assert lines[391].strip() == '</div>', lines[391]
assert lines[393].strip() == '<!-- mobile drawer mask -->', lines[393]

del lines[391]  # 删除多余的 </div>
io.open(PATH, 'w', encoding='utf-8', newline='').write('\n'.join(lines))
print('[ok] removed duplicate </div>')

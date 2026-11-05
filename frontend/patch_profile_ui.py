# -*- coding: utf-8 -*-
import io

PATH = 'src/views/ProfileView.vue'
t = io.open(PATH, encoding='utf-8').read()

# ---------- 1. 标题改为 "AI 今日剩余次数" ----------
old_title = '<p class="text-sm font-medium text-slate-500">AI \u5269\u4f59\u6b21\u6570</p>'
assert t.count(old_title) == 1, ('title', t.count(old_title))
new_title = '<p class="text-sm font-medium text-slate-500">AI \u4eca\u65e5\u5269\u4f59\u6b21\u6570</p>'
t = t.replace(old_title, new_title)

# ---------- 2. 进度条下方加灰色醒目提示 ----------
old_hint = '''          <p class="mt-1 text-xs text-slate-400">
            {{ quotaUnlimited ? '\u5f53\u524d\u8d26\u53f7\u4e0d\u9650\u6b21\u6570' : `\u5df2\u7528 ${quota.used} / ${quota.limit}` }}
          </p>'''
assert t.count(old_hint) == 1, ('hint', t.count(old_hint))
new_hint = '''          <p class="mt-1 text-xs text-slate-400">
            {{ quotaUnlimited ? '\u5f53\u524d\u8d26\u53f7\u4e0d\u9650\u6b21\u6570' : `\u5df2\u7528 ${quota.used} / ${quota.limit}` }}
          </p>
          <p class="mt-3 flex items-start gap-1.5 rounded-lg bg-slate-50 px-3 py-2 text-[11px] font-semibold leading-relaxed text-slate-500 ring-1 ring-inset ring-slate-200">
            <svg class="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-slate-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="9" />
              <path stroke-linecap="round" d="M12 8v.01M12 11v5" />
            </svg>
            <span>\u989d\u5ea6\u6bcf\u65e5\u5237\u65b0\uff0c\u6b21\u65e5 00:00 \u91cd\u7f6e</span>
          </p>'''
t = t.replace(old_hint, new_hint)

# ---------- 3. 删除整个“快捷操作”模块 ----------
start_anchor = '      <!-- \u5feb\u6377\u64cd\u4f5c -->'
end_anchor = '''        </div>
      </section>
    </main>'''
assert t.count(start_anchor) == 1, ('start', t.count(start_anchor))
assert t.count(end_anchor) == 1, ('end', t.count(end_anchor))
i = t.index(start_anchor)
j = t.index(end_anchor)
# 保留 end_anchor 的收尾（</main> 等），去掉其首行 </div>（那是快捷操作内层的闭合）
tail = '''    </main>'''
t = t[:i] + tail + t[j + len(end_anchor):]

# ---------- 4. 清理不再使用的 goPreparation / goCompetitions ----------
for fn in ('goPreparation', 'goCompetitions'):
    idx = t.find('const ' + fn)
    if idx != -1:
        end = t.find('\n}\n', idx)
        assert end != -1, fn
        t = t[:idx] + t[end + 3:]
        # 清掉可能残留的空行
        t = t.replace('\n\n\n\n', '\n\n')

io.open(PATH, 'w', encoding='utf-8', newline='\n').write(t)
print('patched OK')
print('goPreparation left:', t.count('goPreparation'), 'goCompetitions left:', t.count('goCompetitions'))
print('quanJie left:', t.count('\u5feb\u6377\u64cd\u4f5c'))

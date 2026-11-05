import io

p = 'src/components/Header.vue'
s = io.open(p, encoding='utf-8').read()

anchor = '''            <router-link
              to="/preparation"
              class="block w-full rounded-xl px-4 py-3 text-left text-base font-bold
                     text-slate-700 hover:bg-slate-100 transition-colors"
              @click="closeMobile"
            >'''
assert s.count(anchor) == 1, s.count(anchor)

mobile = '''            <router-link
              to="/profile"
              class="block w-full rounded-xl px-4 py-3 text-left text-base font-bold
                     text-slate-700 hover:bg-slate-100 transition-colors"
              @click="closeMobile"
            >
              \u4e2a\u4eba\u4e2d\u5fc3
            </router-link>
''' + anchor

s = s.replace(anchor, mobile, 1)
io.open(p, 'w', encoding='utf-8', newline='\n').write(s)

# 校验
s2 = io.open(p, encoding='utf-8').read()
print('profile links:', s2.count('to="/profile"'))
print('div balance:', s2.count('<div'), s2.count('</div>'))
assert s2.count('<div') == s2.count('</div>'), 'div 不配平'
print('ok')

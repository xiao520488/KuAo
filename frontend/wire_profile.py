import io

# ---------- 1) 路由 ----------
p = 'src/router/index.js'
s = io.open(p, encoding='utf-8').read()

anchor = """    {
      path: '/preparation',
      name: 'preparation',"""
assert s.count(anchor) == 1, s.count(anchor)

route_block = """    {
      path: '/profile',
      name: 'profile',
      meta: { requiresAuth: true },
      component: () => import('@/views/ProfileView.vue')
    },
"""
s = s.replace(anchor, route_block + anchor, 1)
io.open(p, 'w', encoding='utf-8', newline='\n').write(s)
print('router ok')


# ---------- 2) Header 桌面下拉：我的备赛 前插入个人中心 ----------
p = 'src/components/Header.vue'
s = io.open(p, encoding='utf-8').read()

anchor = '''              <div class="my-1 h-px bg-slate-100" />
              <router-link
                to="/preparation"'''
assert s.count(anchor) == 1, s.count(anchor)

desktop = '''              <div class="my-1 h-px bg-slate-100" />
              <router-link
                to="/profile"
                class="block rounded-lg px-3 py-2 text-sm font-medium text-slate-600
                       hover:bg-slate-50 hover:text-slate-900"
                @click="menuOpen = false"
              >
                PROFILE_LABEL
              </router-link>
              <router-link
                to="/preparation"'''
s = s.replace(anchor, desktop, 1)

# 中文标签单独用 unicode 转义写入，避免编辑器/终端 GBK 干扰
s = s.replace('PROFILE_LABEL', '\u4e2a\u4eba\u4e2d\u5fc3', 1)

io.open(p, 'w', encoding='utf-8', newline='\n').write(s)
print('header desktop ok')

# -*- coding: utf-8 -*-
"""按精确文本替换方式为 4 个文件添加移动端响应式类。
每处替换都校验「原串存在且唯一」，任何不符直接报错退出，避免误改。
"""
import io
import sys

REPLACEMENTS = {
    # 1) 竞赛列表页：筛选栏手机端纵向堆叠、下拉框撑满宽度
    'src/views/CompetitionsView.vue': [
        (
            'class="mb-6 flex flex-wrap items-center gap-3 rounded-2xl bg-white p-4\n'
            '               ring-1 ring-slate-200/70 shadow-sm"',
            'class="mb-6 flex flex-col items-stretch gap-3 rounded-2xl bg-white p-4\n'
            '               sm:flex-row sm:flex-wrap sm:items-center ring-1 ring-slate-200/70 shadow-sm"',
        ),
        (
            '<label class="flex items-center gap-2">\n'
            '          <span class="text-sm font-medium text-slate-400">科目</span>\n'
            '          <select\n'
            '            v-model="subject"',
            '<label class="flex flex-1 items-center gap-2 sm:flex-none">\n'
            '          <span class="shrink-0 text-sm font-medium text-slate-400">科目</span>\n'
            '          <select\n'
            '            v-model="subject"',
        ),
        (
            'class="rounded-xl border-0 bg-slate-100 px-4 py-2 text-sm font-semibold\n'
            '                   text-slate-700 outline-none ring-1 ring-transparent\n'
            '                   transition focus:bg-white focus:ring-blue-500"\n'
            '          >\n'
            '            <option v-for="s in subjects"',
            'class="w-full min-w-0 flex-1 rounded-xl border-0 bg-slate-100 px-4 py-2 text-sm font-semibold\n'
            '                   text-slate-700 outline-none ring-1 ring-transparent\n'
            '                   transition focus:bg-white focus:ring-blue-500 sm:w-auto sm:flex-none"\n'
            '          >\n'
            '            <option v-for="s in subjects"',
        ),
        (
            '<label class="flex items-center gap-2">\n'
            '          <span class="text-sm font-medium text-slate-400">筛选</span>\n'
            '          <select\n'
            '            v-model="level"',
            '<label class="flex flex-1 items-center gap-2 sm:flex-none">\n'
            '          <span class="shrink-0 text-sm font-medium text-slate-400">筛选</span>\n'
            '          <select\n'
            '            v-model="level"',
        ),
        (
            'class="rounded-xl border-0 bg-slate-100 px-4 py-2 text-sm font-semibold\n'
            '                   text-slate-700 outline-none ring-1 ring-transparent\n'
            '                   transition focus:bg-white focus:ring-blue-500"\n'
            '          >\n'
            '            <option v-for="l in levels"',
            'class="w-full min-w-0 flex-1 rounded-xl border-0 bg-slate-100 px-4 py-2 text-sm font-semibold\n'
            '                   text-slate-700 outline-none ring-1 ring-transparent\n'
            '                   transition focus:bg-white focus:ring-blue-500 sm:w-auto sm:flex-none"\n'
            '          >\n'
            '            <option v-for="l in levels"',
        ),
        (
            '<span class="ml-auto text-sm text-slate-400">\n'
            '          {{ filtered.length }} 个结果',
            '<span class="ml-auto hidden text-sm text-slate-400 sm:inline">\n'
            '          {{ filtered.length }} 个结果',
        ),
    ],

    # 2) 首页：Hero 标题字号、搜索框宽度、卡片列表显式单列
    'src/views/HomeView.vue': [
        (
            'class="hero-in text-4xl md:text-6xl font-extrabold tracking-tight text-slate-900"',
            'class="hero-in text-3xl sm:text-4xl md:text-5xl lg:text-6xl font-extrabold tracking-tight text-slate-900"',
        ),
        (
            'class="hero-in mt-6 max-w-none whitespace-nowrap text-xs sm:text-sm md:text-base leading-relaxed text-slate-500 font-medium"',
            'class="hero-in mt-6 max-w-none text-xs leading-relaxed text-slate-500 font-medium sm:whitespace-nowrap sm:text-sm md:text-base"',
        ),
        (
            'class="hero-in mx-auto mt-12 flex w-full max-w-4xl items-center gap-3',
            'class="hero-in mx-auto mt-12 flex w-[90%] max-w-4xl items-center gap-3 md:w-full',
        ),
        (
            '<div v-else :key="listAnimKey" class="space-y-6">',
            '<div v-else :key="listAnimKey" class="grid grid-cols-1 gap-6 md:grid-cols-2">',
        ),
        (
            '<div v-if="loading && !searching" class="space-y-6">',
            '<div v-if="loading && !searching" class="grid grid-cols-1 gap-6 md:grid-cols-2">',
        ),
    ],

    # 3) 我的备赛：卡片列表显式单列（原 flex 列，改为响应式 grid）
    'src/views/PreparationView.vue': [
        (
            '<div v-else class="flex flex-col gap-6">',
            '<div v-else class="grid grid-cols-1 gap-6">',
        ),
        (
            '<main class="mx-auto max-w-5xl px-6 pb-24 pt-28">',
            '<main class="mx-auto max-w-5xl px-4 pb-24 pt-24 sm:px-6 sm:pt-28">',
        ),
        (
            '<h1 class="text-3xl font-bold text-slate-900">',
            '<h1 class="text-2xl font-bold text-slate-900 sm:text-3xl">',
        ),
    ],

    # 4) 备赛工作台：Tab 横向可滚动、顶部信息区手机端不溢出
    'src/views/PreparationWorkspaceView.vue': [
        (
            '<main class="mx-auto w-full max-w-6xl px-6 pb-24 pt-28">',
            '<main class="mx-auto w-full max-w-6xl px-4 pb-24 pt-24 sm:px-6 sm:pt-28">',
        ),
        (
            'class="sticky top-16 z-20 -mx-6 mb-6 border-b border-slate-200 bg-white/90 px-6 py-4 backdrop-blur"',
            'class="sticky top-16 z-20 -mx-4 mb-6 border-b border-slate-200 bg-white/90 px-4 py-4 backdrop-blur sm:-mx-6 sm:px-6"',
        ),
        (
            '<nav class="mt-4 flex items-center gap-2">\n'
            '        <button\n'
            '          v-for="tab in tabs"\n'
            '          :key="tab.key"\n'
            '          type="button"\n'
            '          class="rounded-full px-5 py-2 text-sm font-bold transition-colors"',
            '<nav class="mt-4 flex flex-nowrap items-center gap-2 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0">\n'
            '        <button\n'
            '          v-for="tab in tabs"\n'
            '          :key="tab.key"\n'
            '          type="button"\n'
            '          class="shrink-0 whitespace-nowrap rounded-full px-4 py-2 text-sm font-bold transition-colors sm:px-5"',
        ),
    ],
}


def main():
    errors = []
    for path, pairs in REPLACEMENTS.items():
        with io.open(path, encoding='utf-8') as f:
            text = f.read()
        original = text
        for old, new in pairs:
            cnt = text.count(old)
            if cnt != 1:
                errors.append('%s: 目标串出现 %d 次 (应为1) -> %r' % (path, cnt, old[:60]))
                continue
            text = text.replace(old, new, 1)
        if text != original:
            with io.open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(text)
            print('[OK] %s  %d 处替换' % (path, len(pairs)))
    if errors:
        print('\n'.join(errors))
        sys.exit(1)
    print('ALL DONE')


main()

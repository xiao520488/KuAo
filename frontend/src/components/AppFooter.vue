<script setup>
/**
 * 全站页脚：ICP 备案号
 *
 * 备案号是境内网站的强制公示项，必须全站可见、可点击核验，
 * 所以挂在根组件 App.vue 上，而不是散落在各个页面里（新增页面会漏）。
 * 链接指向工信部备案查询官网，新标签页打开，避免把用户带离站点。
 */
const BEIAN_NO = '湘ICP备2026054979号'
const BEIAN_URL = 'http://beian.miit.gov.cn/'
</script>

<template>
  <footer class="mt-auto w-full py-6 text-center text-xs leading-relaxed">
    <a
      :href="BEIAN_URL"
      target="_blank"
      rel="noopener noreferrer"
      class="text-slate-500 transition-colors hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
    >{{ BEIAN_NO }}</a>
  </footer>
</template>
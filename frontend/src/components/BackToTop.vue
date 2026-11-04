<script setup>
import { onMounted, onUnmounted, ref } from 'vue'

/**
 * 全局「回到顶部」悬浮按钮
 * - 右侧固定定位的圆形按钮，向上箭头 + 柔和阴影
 * - 滚动超过 offset 像素时淡入，回到顶部附近时淡出
 * - 点击平滑滚动回顶部
 */

// 触发显示的滚动阈值（px）
const offset = 300

const visible = ref(false)

/**
 * scroll 监听回调
 * 用 ref 取当前值即可，无需节流：scrollTo 的平滑滚动是浏览器原生实现，
 * 这里只做一次布尔比较，开销极小。
 */
function onScroll() {
  const y = window.scrollY ?? window.pageYOffset ?? document.documentElement.scrollTop ?? 0
  visible.value = y > offset
}

/** 平滑回到顶部（老浏览器降级为瞬时跳转） */
function scrollToTop() {
  if (typeof window === 'undefined') return
  if (typeof window.scrollTo === 'function') {
    try {
      window.scrollTo({ top: 0, behavior: 'smooth' })
      return
    } catch (e) {
      // 不支持对象参数的老浏览器，降级为普通跳转
    }
    window.scrollTo(0, 0)
  }
}

onMounted(() => {
  // 首次挂载可能已处于滚动状态（如浏览器恢复了滚动位置），先同步一次
  onScroll()
  // passive: true 表示不会调用 preventDefault，浏览器可优化滚动性能
  window.addEventListener('scroll', onScroll, { passive: true })
})

onUnmounted(() => {
  // 必须移除，否则路由切换后组件已销毁，回调仍会执行 → 内存泄漏 + 报错
  window.removeEventListener('scroll', onScroll)
})
</script>

<template>
  <!--
    Transition：用 opacity + 位移做淡入淡出
    pointer-events-none：隐藏时不可点击，避免透明按钮挡住页面右下角内容
    aria-hidden / tabindex：隐藏时不进入无障碍树与 Tab 焦点序列
  -->
  <Transition
    enter-active-class="transition duration-300 ease-out"
    enter-from-class="opacity-0 translate-y-2"
    enter-to-class="opacity-100 translate-y-0"
    leave-active-class="transition duration-200 ease-in"
    leave-from-class="opacity-100 translate-y-0"
    leave-to-class="opacity-0 translate-y-2"
  >
    <button
      v-if="visible"
      type="button"
      class="fixed right-10 bottom-16 z-40 group
             inline-flex h-12 w-12 items-center justify-center
             rounded-full bg-white text-slate-600
             ring-1 ring-slate-200/80
             shadow-lg shadow-slate-900/10
             transition-colors duration-300
             hover:text-blue-600 hover:bg-blue-50 hover:ring-blue-200
             hover:shadow-xl hover:shadow-blue-600/15
             focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500
             cursor-pointer"
      aria-label="回到顶部"
      title="回到顶部"
      @click="scrollToTop"
    >
      <svg
        class="h-5 w-5 transition-transform duration-300 group-hover:-translate-y-0.5"
        fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"
        aria-hidden="true"
      >
        <path stroke-linecap="round" stroke-linejoin="round" d="M5 12l7-7 7 7M12 5v14" />
      </svg>
    </button>
  </Transition>
</template>

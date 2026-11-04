<script setup>
import { toastState } from '@/composables/useToast'

/**
 * 全局轻提示（Toast）宿主组件
 *
 * - 固定在屏幕中上方，不参与页面布局、不阻塞点击（pointer-events-none）
 * - 黑色半透明胶囊，白字，淡入 + 轻微上浮
 * - 显示时长由 showToast 的 duration 控制（默认 2000ms），组件本身不设计时器
 */
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 -translate-y-2"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-200 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 -translate-y-2"
    >
      <div
        v-if="toastState.visible"
        class="pointer-events-none fixed inset-x-0 top-20 z-[100] flex justify-center px-4"
        role="status"
        aria-live="polite"
      >
        <div
          class="max-w-md rounded-full bg-slate-900/85 px-5 py-2.5
                 text-center text-sm font-medium text-white
                 shadow-lg shadow-slate-900/25 backdrop-blur-sm"
        >
          {{ toastState.message }}
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// 统一封面：程序化生成，纯 SVG，无网络请求、无积分、不依赖爬取的图。
//
// 三种主题 = 同一套构图的三种「材质」，几何母题与前景结构完全共用，只换底色/线条/质感：
//   tech    科技蓝：深蓝渐变 + 光晕 + 粒子 + 发光线稿（海报感）
//   official 官方风：浅蓝绿纸面 + 证书波纹 + 双线内框 + 印章环 + 墨线线稿（公文/证书感）
//   minimal  极简风：纯白 + 4% 微网格 + 超细单线稿 + 一枚类别色小方块（图纸/瑞士海报感）
//
// 信息编码不变：类别只改「类别色 + 几何母题」，于是同一主题下远看是一系列，近看能区分。
// 若以后要给某些赛事换成真 AI 图，把 COVER_USE_REMOTE 打开即可回退到 image_url。
import { computed } from 'vue'
import { coverFallback } from '@/utils/coverFallback'
import { theme } from '@/composables/useTheme'

const props = defineProps({
  title: { type: String, default: '' },
  shortName: { type: String, default: '' },
  category: { type: String, default: '' },
  seed: { type: [String, Number], default: '' },
  variant: { type: String, default: 'card' } // card | hero | thumb
})

let uidSeq = 0

const fb = computed(() => coverFallback({
  title: props.title,
  shortName: props.shortName,
  category: props.category
}))

const uid = `ca${++uidSeq}`

/** 当前主题对应的封面材质 */
const mode = computed(() => {
  const t = theme.value
  return t === 'tech' ? 'tech' : t === 'minimal' ? 'minimal' : 'official'
})

// 同一赛事在列表、详情页拿到同一版式：用 seed（赛事 id）而不是标题做微扰
const drift = computed(() => {
  const s = String(props.seed || props.title || '')
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 997
  return {
    angle: -12 + (h % 9) * 3, // -12 ~ 12 度
    glowX: 70 + (h % 5) * 3, // 70 ~ 82 %
    glowY: 22 + (h % 4) * 4,
    rotate: (h % 7) * 4
  }
})

const hue = computed(() => fb.value.hue)
const accentSoft = computed(() => `hsl(${hue.value} 78% 46%)`)
const accentDeep = computed(() => `hsl(${hue.value} 72% 30%)`)

/** 类别色：深色主题用亮色发光，浅色主题用降饱和的墨色，避免刺眼 */
const accent = computed(() => {
  if (mode.value === 'tech') return `hsl(${hue.value} 85% 62%)`
  if (mode.value === 'minimal') return `hsl(${hue.value} 34% 40%)`
  return `hsl(${hue.value} 54% 32%)`
})

// 大块实心（柱体、圆盘）在浅色材质里改成墨线轮廓：纸上印一块色会糊成脏斑
const solidFill = computed(() => (mode.value === 'tech' ? accent.value : 'none'))
const solidStroke = computed(() => (mode.value === 'tech' ? 'none' : accent.value))
const solidStrokeOpacity = computed(() => (mode.value === 'tech' ? 0 : 0.55))

/** 浅色主题的线条/文字墨色 */
const ink = '#1f3a5f'
const inkSoft = 'rgb(31 58 95 / 0.32)'
const inkHair = 'rgb(31 58 95 / 0.16)'

// 线稿参数按材质切换：深色主题粗发光，浅色主题细墨线
const motifStroke = computed(() => (
  mode.value === 'tech' ? `url(#${uid}-stroke)` : mode.value === 'official' ? accent.value : accent.value
))
const motifWidth = computed(() => (mode.value === 'tech' ? 4 : mode.value === 'official' ? 3 : 1.8))
const motifFilter = computed(() => (mode.value === 'tech' ? 'drop-shadow(0 4px 22px rgb(120 170 255 / 0.45))' : 'none'))
const motifOpacity = computed(() => (mode.value === 'tech' ? 1 : 0.95))
// 官方风右侧要留印章位，线稿整体左移一点，避免压住印章文字
const motifShift = computed(() => (mode.value === 'tech' ? 0 : mode.value === 'official' ? -56 : -18))

// 粒子只用于科技蓝
const particles = computed(() => {
  const s = String(props.seed ?? props.title ?? 'kuao')
  let h = 2166136261
  for (let i = 0; i < s.length; i++) h = (h ^ s.charCodeAt(i)) * 16777619 >>> 0
  const next = () => {
    h = (h * 1664525 + 1013904223) >>> 0
    return h / 4294967296
  }
  return Array.from({ length: 18 }, (_, i) => ({
    x: 120 + next() * 1360,
    y: 80 + next() * 720,
    r: 1.6 + next() * 4.2,
    o: 0.12 + next() * 0.5,
    warm: i % 3 === 0
  }))
})

// 类别 → 几何母题
const motif = computed(() => {
  const t = `${fb.value.major || ''}${fb.value.minor || ''}`
  if (/人工智能|算法|大数据|数据|信息|计算|软件/.test(t)) return 'network'
  if (/电子|自动化|机械|机器人|工程机械|物联网|通信|芯片|集成电路/.test(t)) return 'circuit'
  if (/土木|建筑|交通|水利|测绘|环境|能源|材料/.test(t)) return 'truss'
  if (/商科|创业|经济|金融|管理|营销/.test(t)) return 'rise'
  if (/农业|食品|生物|医学|护理|药学|化学|健康/.test(t)) return 'arc'
  return 'orbit'
})

const sealY = computed(() => Math.round((drift.value.glowY / 100) * 900 + 150))

const styleVars = computed(() => ({ '--accent-h': hue.value }))

const sizeClass = computed(() => (
  props.variant === 'hero'
    ? 'cover-art--hero'
    : props.variant === 'thumb'
      ? 'cover-art--thumb'
      : props.variant === 'banner'
        ? 'cover-art--banner'
        : ''
))

// 详情页头图是超宽画幅（约 4.8:1），16:9 的上下边会被裁掉：
// 边框类装饰在头图里改成「页边线 + 左侧色条」，否则只剩两条断掉的竖线
const isHero = computed(() => props.variant === 'hero')

// 首页首屏横幅：只当材质底片用，不放徽记/标签，也不画任何边框
const isBanner = computed(() => props.variant === 'banner')
const showFrame = computed(() => !isHero.value && !isBanner.value)
</script>

<template>
  <div
    class="cover-art relative h-full w-full overflow-hidden"
    :class="[`cover-art--${mode}`, sizeClass]"
    :key="mode"
    :style="styleVars"
    aria-hidden="true"
  >
    <svg
      class="absolute inset-0 h-full w-full"
      viewBox="0 0 1600 900"
      preserveAspectRatio="xMidYMid slice"
      role="presentation"
    >
      <defs>
        <!-- 科技蓝 -->
        <linearGradient :id="`${uid}-stroke`" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#dbe8ff" stop-opacity="0.95" />
          <stop offset="55%" :stop-color="accent" stop-opacity="0.9" />
          <stop offset="100%" stop-color="#7ea6ff" stop-opacity="0.45" />
        </linearGradient>
        <linearGradient :id="`${uid}-base`" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#050b1c" />
          <stop offset="46%" stop-color="#0b1d43" />
          <stop offset="100%" stop-color="#10265c" />
        </linearGradient>
        <radialGradient :id="`${uid}-glow`">
          <stop offset="0%" :stop-color="accent" stop-opacity="0.55" />
          <stop offset="52%" :stop-color="accentSoft" stop-opacity="0.22" />
          <stop offset="100%" :stop-color="accentDeep" stop-opacity="0" />
        </radialGradient>
        <linearGradient :id="`${uid}-beam`" :x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stop-color="#ffffff" stop-opacity="0" />
          <stop offset="48%" stop-color="#ffffff" stop-opacity="0.12" />
          <stop offset="100%" stop-color="#ffffff" stop-opacity="0" />
        </linearGradient>
        <radialGradient :id="`${uid}-fade`">
          <stop offset="0%" stop-color="#ffffff" stop-opacity="0.85" />
          <stop offset="62%" stop-color="#ffffff" stop-opacity="0.18" />
          <stop offset="100%" stop-color="#ffffff" stop-opacity="0" />
        </radialGradient>
        <linearGradient :id="`${uid}-vignette`" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#02060f" stop-opacity="0.55" />
          <stop offset="42%" stop-color="#02060f" stop-opacity="0" />
          <stop offset="100%" stop-color="#02060f" stop-opacity="0.72" />
        </linearGradient>
        <pattern :id="`${uid}-grid`" width="64" height="64" patternUnits="userSpaceOnUse">
          <path d="M64 0H0v64" fill="none" stroke="#8fb4ff" stroke-opacity="0.22" stroke-width="1.4" />
        </pattern>
        <mask :id="`${uid}-gridmask`">
          <rect width="1600" height="900" :fill="`url(#${uid}-fade)`" />
        </mask>

        <!-- 官方风：纸面 + 点阵 -->
        <linearGradient :id="`${uid}-paper`" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#ffffff" />
          <stop offset="50%" stop-color="#f3f9fc" />
          <stop offset="100%" stop-color="#dcecf4" />
        </linearGradient>
        <radialGradient :id="`${uid}-paperwash`">
          <stop offset="0%" :stop-color="accent" stop-opacity="0.16" />
          <stop offset="66%" :stop-color="accent" stop-opacity="0.05" />
          <stop offset="100%" :stop-color="accent" stop-opacity="0" />
        </radialGradient>
        <pattern :id="`${uid}-dots`" width="26" height="26" patternUnits="userSpaceOnUse">
          <circle cx="1.6" cy="1.6" r="1.1" fill="#1f3a5f" fill-opacity="0.16" />
        </pattern>
        <!-- 首屏横幅专用：底部一层很淡的薄荷地平线，让外部的动态波浪有落点 -->
        <linearGradient :id="`${uid}-horizon`" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#dcf1ec" stop-opacity="0" />
          <stop offset="40%" stop-color="#d8f0ea" stop-opacity="0.5" />
          <stop offset="76%" stop-color="#cfeae4" stop-opacity="0.62" />
          <stop offset="100%" stop-color="#eef8f5" stop-opacity="0.06" />
        </linearGradient>

        <!-- 极简风：微网格 + 极浅纸面 -->
        <pattern :id="`${uid}-micro`" width="48" height="48" patternUnits="userSpaceOnUse">
          <path d="M48 0H0v48" fill="none" stroke="#0f172a" stroke-opacity="0.075" stroke-width="1" />
        </pattern>
        <linearGradient :id="`${uid}-plain`" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#fdfefe" />
          <stop offset="100%" stop-color="#f4f6f8" />
        </linearGradient>
        <radialGradient :id="`${uid}-soft`">
          <stop offset="0%" :stop-color="accent" stop-opacity="0.12" />
          <stop offset="60%" :stop-color="accent" stop-opacity="0.04" />
          <stop offset="100%" :stop-color="accent" stop-opacity="0" />
        </radialGradient>
      </defs>

      <!-- ================= 底色层 ================= -->
      <template v-if="mode === 'tech'">
        <rect width="1600" height="900" :fill="`url(#${uid}-base)`" />
        <ellipse
          :cx="(drift.glowX / 100) * 1600"
          :cy="(drift.glowY / 100) * 900"
          rx="700"
          ry="470"
          :fill="`url(#${uid}-glow)`"
        />
        <rect width="1600" height="900" :fill="`url(#${uid}-grid)`" :mask="`url(#${uid}-gridmask)`" />

        <!-- 悬浮粒子 -->
        <g>
          <circle
            v-for="(p, i) in particles"
            :key="`p${i}`"
            :cx="p.x"
            :cy="p.y"
            :r="p.r"
            :fill="p.warm ? accent : '#dbe8ff'"
            :fill-opacity="p.o"
          />
        </g>

        <!-- 底部光边 -->
        <rect x="0" y="884" width="1600" height="2.5" :fill="accent" opacity="0.45" />
        <rect x="0" y="858" width="1600" height="26" :fill="accent" opacity="0.08" />

        <!-- 斜向光带 -->
        <g :transform="`rotate(${drift.angle} 1180 430)`">
          <rect x="820" y="-120" width="150" height="1200" :fill="`url(#${uid}-beam)`" />
          <rect x="1060" y="-120" width="62" height="1200" :fill="`url(#${uid}-beam)`" opacity="0.7" />
        </g>
      </template>

      <template v-else-if="mode === 'official'">
        <rect width="1600" height="900" :fill="`url(#${uid}-paper)`" />
        <ellipse
          :cx="(drift.glowX / 100) * 1600"
          :cy="(drift.glowY / 100) * 900"
          rx="760"
          ry="520"
          :fill="`url(#${uid}-paperwash)`"
        />
        <rect width="1600" height="900" :fill="`url(#${uid}-dots)`" :opacity="isBanner ? 0.85 : 0.55" />
        <rect v-if="isBanner" x="0" y="640" width="1600" height="260" :fill="`url(#${uid}-horizon)`" />

        <!-- 证书波纹：两条相位错开的正弦，极低透明度 -->
        <g fill="none" :stroke="ink" stroke-opacity="0.1" stroke-width="1.6">
          <path d="M-40 214 C 230 160, 470 268, 760 214 S 1290 160, 1660 214" />
          <path d="M-40 246 C 230 192, 470 300, 760 246 S 1290 192, 1660 246" />
          <path d="M-40 690 C 230 636, 470 744, 760 690 S 1290 636, 1660 690" />
          <path d="M-40 722 C 230 668, 470 776, 760 722 S 1290 668, 1660 722" />
        </g>

        <!-- 双线内框：卡片用完整边框，头图只留页边线，首屏横幅不画框 -->
        <g v-if="showFrame">
          <rect x="28" y="28" width="1544" height="844" fill="none" :stroke="ink" stroke-opacity="0.36" stroke-width="2.5" />
          <rect x="46" y="46" width="1508" height="808" fill="none" :stroke="ink" stroke-opacity="0.2" stroke-width="1.2" />
        </g>
        <template v-else-if="isHero">
          <rect x="0" y="452" width="1600" height="2" :fill="ink" opacity="0.14" />
          <rect x="0" y="0" width="10" height="900" :fill="accent" opacity="0.55" />
        </template>

        <!-- 印章环：双环 + 齿边 + 竖排大类名，整体微倾、环有断口，像真盖上去的 -->
        <g v-if="!isBanner" :transform="`translate(1352 ${sealY}) rotate(${drift.rotate - 6.5})`" fill="none" :stroke="accent" stroke-opacity="0.42">
          <circle r="116" :fill="accent" fill-opacity="0.05" stroke="none" />
          <circle r="140" stroke-width="3.6" stroke-dasharray="742 34" stroke-dashoffset="120" />
          <circle r="116" stroke-width="1.6" stroke-dasharray="4 10" />
          <circle r="128" stroke-width="0.8" stroke-opacity="0.6" />
          <g stroke-width="1.8" stroke-opacity="0.5">
            <path d="M-150 -46h-11" /><path d="M-150 46h-11" />
            <path d="M150 -46h11" /><path d="M150 46h11" />
          </g>
          <text
            y="18"
            text-anchor="middle"
            :fill="accent"
            :fill-opacity="0.58"
            stroke="none"
            font-size="52"
            font-weight="700"
            letter-spacing="4"
          >{{ fb.major || fb.catLabel }}</text>
        </g>

        <!-- 首屏横幅：官方风不再加任何装饰，靠纸纹 + 地平线 + 外部动态波浪收尾 -->
        <!-- 顶部类别色细条 -->
        <rect x="0" y="0" width="1600" height="7" :fill="accent" opacity="0.9" />
      </template>

      <template v-else>
        <rect width="1600" height="900" :fill="`url(#${uid}-plain)`" />
        <ellipse cx="1120" cy="420" rx="620" ry="430" :fill="`url(#${uid}-soft)`" />
        <rect width="1600" height="900" :fill="`url(#${uid}-micro)`" />
        <!-- 索引条：类别色只占左上三分之一，右侧留灰线接住 -->
        <rect x="0" y="0" width="520" height="4" :fill="accent" opacity="0.8" />
        <rect x="520" y="0" width="1080" height="4" fill="#0f172a" fill-opacity="0.08" />
        <rect v-if="isHero" x="0" y="0" width="10" height="900" :fill="accent" opacity="0.5" />
        <rect x="0" y="897" width="1600" height="3" fill="#0f172a" fill-opacity="0.07" />
        <!-- 首屏横幅专用：底部一条刻度尺代替环形母题（极简风不用圆） -->
        <g v-if="isBanner" :stroke="ink" stroke-opacity="0.2" stroke-width="1.2">
          <path d="M96 806H1504" />
          <path d="M184 806v40" /><path d="M272 806v22" /><path d="M360 806v40" />
          <path d="M448 806v22" /><path d="M536 806v40" /><path d="M624 806v22" />
          <path d="M712 806v40" /><path d="M800 806v22" /><path d="M888 806v40" />
          <path d="M976 806v22" /><path d="M1064 806v40" /><path d="M1152 806v22" />
          <path d="M1240 806v40" /><path d="M1328 806v22" /><path d="M1416 806v40" />
        </g>
        <rect v-if="isBanner" x="96" y="802" width="88" height="4" :fill="accent" opacity="0.75" />

        <!-- 对角对齐角标：极简海报的定位记号 -->
        <g v-if="showFrame" fill="none" :stroke="accent" stroke-opacity="0.5" stroke-width="2">
          <path d="M56 96V56h40" />
          <path d="M1544 804v40h-40" />
        </g>
      </template>

      <!-- ================= 几何母题（三套材质共用同一套形状） ================= -->
      <!-- 首屏横幅：浅色材质不用环形母题（在大画幅上会变成星空感），只有科技蓝保留 -->
      <g
        v-if="!isBanner || mode === 'tech'"
        fill="none"
        :stroke="motifStroke"
        :stroke-width="motifWidth"
        :opacity="motifOpacity"
        stroke-linecap="round"
        stroke-linejoin="round"
        :transform="`translate(${motifShift} 0) rotate(${drift.rotate} 1120 430)`"
        :style="{ filter: motifFilter }"
      >
        <template v-if="motif === 'orbit'">
          <circle cx="1120" cy="430" r="250" stroke-opacity="0.55" />
          <circle cx="1120" cy="430" r="178" stroke-opacity="0.32" stroke-dasharray="5 16" />
          <circle cx="1120" cy="430" r="326" :stroke="accent" stroke-opacity="0.55" />
          <ellipse cx="1120" cy="430" rx="326" ry="118" :stroke="accent" stroke-opacity="0.4" />
          <circle cx="1120" cy="104" r="9" :fill="accent" stroke="none" />
          <circle cx="1412" cy="506" r="7" :fill="accent" stroke="none" opacity="0.85" />
          <circle cx="836" cy="300" r="5" :fill="accent" stroke="none" opacity="0.6" />
        </template>

        <template v-else-if="motif === 'network'">
          <path d="M980 250 L1180 300 L1290 470 L1180 640 L980 600 L900 430 Z" stroke-opacity="0.55" />
          <path d="M1180 300 L1180 640 M980 250 L1180 470 L900 430 M1180 470 L1290 470" stroke-opacity="0.3" />
          <circle cx="980" cy="250" r="11" :fill="accent" stroke="none" />
          <circle cx="1180" cy="300" r="8" :fill="accent" stroke="none" opacity="0.9" />
          <circle cx="1290" cy="470" r="13" :fill="accent" stroke="none" opacity="0.75" />
          <circle cx="1180" cy="640" r="9" :fill="accent" stroke="none" opacity="0.7" />
          <circle cx="980" cy="600" r="7" :fill="accent" stroke="none" opacity="0.6" />
          <circle cx="900" cy="430" r="6" :fill="accent" stroke="none" opacity="0.55" />
          <circle cx="1180" cy="470" r="16" :stroke="accent" stroke-opacity="0.8" />
        </template>

        <template v-else-if="motif === 'circuit'">
          <path d="M900 620 H1040 V470 H1180 V320 H1330" stroke-opacity="0.6" />
          <path d="M900 380 H1010 V250 H1200 V400 H1300" stroke-opacity="0.35" />
          <path d="M960 700 H1120 V560" stroke-opacity="0.25" />
          <rect x="1040" y="470" width="18" height="18" :fill="accent" stroke="none" opacity="0.9" />
          <rect x="1180" y="320" width="18" height="18" :fill="accent" stroke="none" opacity="0.8" />
          <rect x="1010" y="250" width="14" height="14" :fill="accent" stroke="none" opacity="0.7" />
          <circle cx="1330" cy="320" r="10" :fill="accent" stroke="none" />
          <circle cx="900" cy="380" r="8" :fill="accent" stroke="none" opacity="0.7" />
          <path d="M1120 470 v60 h200" stroke-opacity="0.25" stroke-dasharray="6 12" />
        </template>

        <template v-else-if="motif === 'truss'">
          <path d="M880 660 L1080 540 L1300 660" stroke-opacity="0.6" />
          <path d="M880 660 L1080 780 L1300 660" stroke-opacity="0.3" />
          <path d="M1080 540 v120 M960 600 v120 M1200 600 v120" stroke-opacity="0.35" />
          <path d="M960 600 L1200 600 M920 660 L1080 660 L1240 660" stroke-opacity="0.25" />
          <path d="M1080 540 L1080 300 L1240 220" stroke-opacity="0.4" />
          <circle cx="1080" cy="300" r="10" :fill="accent" stroke="none" />
          <circle cx="1240" cy="220" r="7" :fill="accent" stroke="none" opacity="0.8" />
          <rect x="1040" y="620" width="80" height="12" :fill="accent" stroke="none" opacity="0.5" />
        </template>

        <template v-else-if="motif === 'rise'">
          <rect x="900" y="560" width="56" height="120" :fill="solidFill" :stroke="solidStroke" :stroke-opacity="solidStrokeOpacity" :opacity="mode === 'tech' ? 0.3 : 1" />
          <rect x="986" y="470" width="56" height="210" :fill="solidFill" :stroke="solidStroke" :stroke-opacity="solidStrokeOpacity" :opacity="mode === 'tech' ? 0.42 : 1" />
          <rect x="1072" y="370" width="56" height="310" :fill="solidFill" :stroke="solidStroke" :stroke-opacity="solidStrokeOpacity" :opacity="mode === 'tech' ? 0.56 : 1" />
          <rect x="1158" y="270" width="56" height="410" :fill="solidFill" :stroke="solidStroke" :stroke-opacity="solidStrokeOpacity" :opacity="mode === 'tech' ? 0.7 : 1" />
          <path d="M880 640 L1300 200" stroke-opacity="0.5" />
          <path d="M1214 200 L1300 200 L1300 286" stroke-opacity="0.7" :stroke="accent" />
          <circle cx="1158" cy="270" r="9" :fill="accent" stroke="none" />
        </template>

        <template v-else>
          <path d="M880 620 Q1080 380 1300 620" stroke-opacity="0.55" />
          <path d="M900 680 Q1080 470 1280 680" stroke-opacity="0.35" />
          <path d="M940 740 Q1080 570 1240 740" stroke-opacity="0.22" />
          <circle cx="1090" cy="500" r="86" :stroke="accent" stroke-opacity="0.55" />
          <circle cx="1090" cy="500" r="34" :fill="solidFill" :stroke="solidStroke" :stroke-opacity="solidStrokeOpacity" :opacity="mode === 'tech' ? 0.7 : 1" />
          <circle cx="1250" cy="420" r="12" :fill="accent" stroke="none" opacity="0.55" />
        </template>
      </g>

      <rect v-if="mode === 'tech'" width="1600" height="900" :fill="`url(#${uid}-vignette)`" />
    </svg>

    <!-- 前景：类别徽记 + 分类标签（文字用 HTML 渲染，缩放更清晰） -->
    <div class="cover-art__pad absolute inset-0 flex flex-col justify-end">
      <div class="cover-art__row flex items-center">
        <span class="cover-mark" :class="{ 'cover-mark--wide': fb.isShortName }">{{ fb.glyph }}</span>
        <span class="cover-tag">{{ fb.catLabel }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cover-art {
  container-type: inline-size;
  /* 换主题时整块重绘，淡入 240ms 避免材质「硬切」 */
  animation: cover-art-in 240ms ease-out both;
}

@keyframes cover-art-in {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .cover-art {
    animation: none;
  }
}

/* ================= 科技蓝 ================= */
.cover-art--tech {
  background: #071026;
}

.cover-art--tech .cover-mark {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: clamp(42px, 15cqw, 68px);
  height: clamp(42px, 15cqw, 68px);
  border-radius: clamp(11px, 4cqw, 20px);
  border: 1px solid rgb(255 255 255 / 0.3);
  background: linear-gradient(145deg, hsl(var(--accent-h) 88% 61%), hsl(var(--accent-h) 82% 36%));
  box-shadow:
    inset 0 1px 0 rgb(255 255 255 / 0.42),
    0 12px 26px -12px hsl(var(--accent-h) 85% 25% / 0.9);
  color: #fff;
  font-size: clamp(15px, 5.4cqw, 26px);
  font-weight: 800;
  line-height: 1;
  letter-spacing: -0.02em;
  text-shadow: 0 1px 2px rgb(0 0 0 / 0.25);
}

.cover-art--tech .cover-tag {
  color: rgb(226 236 255 / 0.72);
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

/* ================= 官方风：印章式方块 + 宋体小字 ================= */
.cover-art--official {
  background: #f3f9fc;
  box-shadow: inset 0 0 0 1px rgb(31 58 95 / 0.12);
}

.cover-art--official .cover-mark {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: clamp(40px, 13cqw, 62px);
  height: clamp(40px, 13cqw, 62px);
  border-radius: clamp(6px, 1.6cqw, 10px);
  border: 1px solid hsl(var(--accent-h) 52% 30% / 0.35);
  background: hsl(var(--accent-h) 52% 38%);
  box-shadow: inset 0 0 0 2px rgb(255 255 255 / 0.22);
  color: #fff;
  font-size: clamp(15px, 5cqw, 25px);
  font-weight: 700;
  line-height: 1;
  letter-spacing: 0.02em;
}

.cover-art--official .cover-tag {
  color: #38536f;
  font-family: ui-serif, Georgia, 'Songti SC', 'SimSun', serif;
  font-weight: 600;
  letter-spacing: 0.2em;
  text-transform: uppercase;
}

/* ================= 极简风：空心细框 + 灰字 ================= */
.cover-art--minimal {
  background: #fbfcfd;
  box-shadow: inset 0 0 0 1px rgb(15 23 42 / 0.08);
}

.cover-art--minimal .cover-mark {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: clamp(34px, 10cqw, 46px);
  height: clamp(34px, 10cqw, 46px);
  border-radius: 2px;
  border: 1.5px solid hsl(var(--accent-h) 32% 52% / 0.55);
  background: transparent;
  color: hsl(var(--accent-h) 38% 40%);
  font-size: clamp(13px, 4cqw, 20px);
  font-weight: 600;
  line-height: 1;
  letter-spacing: 0;
}

.cover-art--minimal .cover-tag {
  color: #6b7480;
  font-weight: 500;
  letter-spacing: 0.22em;
  text-transform: uppercase;
}

/* ================= 共用排版 ================= */
.cover-art__pad {
  padding: 3.2cqw;
}

.cover-art__row {
  gap: 2.4cqw;
}

/* 首屏横幅只当材质底片用：徽记和标签交给页面自己排版 */
.cover-art--banner .cover-art__row {
  display: none;
}

.cover-mark--wide {
  letter-spacing: -0.04em;
}

.cover-tag {
  font-size: clamp(10px, 2.9cqw, 13px);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cover-art--hero .cover-mark {
  width: clamp(58px, 8.6cqw, 92px);
  height: clamp(58px, 8.6cqw, 92px);
  font-size: clamp(20px, 3.2cqw, 32px);
}

.cover-art--hero .cover-tag {
  font-size: clamp(12px, 1.5cqw, 15px);
}

/* 工作台顶部小图（约 96×64）：只留徽记，字与标签按比例缩，避免溢出 */
.cover-art--thumb .cover-art__pad {
  padding: 8cqw;
}

.cover-art--thumb .cover-mark {
  width: clamp(18px, 26cqw, 30px);
  height: clamp(18px, 26cqw, 30px);
  border-radius: 4px;
  font-size: clamp(10px, 13cqw, 14px);
}

.cover-art--thumb .cover-tag {
  display: none;
}
</style>
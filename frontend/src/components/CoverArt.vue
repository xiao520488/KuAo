<script setup>
// 统一封面：程序化生成的「深蓝科技几何」封面（纯 SVG，无网络请求、无积分、不依赖爬取的图）。
// 全站所有赛事图片窗口（列表卡片 / 详情页头图）都走这一套，保证风格一致。
//
// 信息编码：底色永远是同一套深蓝；类别只改「光晕 + 徽记」的色相与几何母题，
// 于是远看是同一系列，近看能区分工科/理科/商科/文科。
// 若以后要给某些赛事换成真 AI 图，把 COVER_USE_REMOTE 打开即可回退到 image_url。
import { computed } from 'vue'
import { coverFallback } from '@/utils/coverFallback'

const props = defineProps({
  title: { type: String, default: '' },
  shortName: { type: String, default: '' },
  category: { type: String, default: '' },
  seed: { type: [String, Number], default: '' },
  variant: { type: String, default: 'card' } // card | hero
})

let uidSeq = 0

const fb = computed(() => coverFallback({
  title: props.title,
  shortName: props.shortName,
  category: props.category
}))

const uid = `ca${++uidSeq}`

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
const accent = computed(() => `hsl(${hue.value} 85% 62%)`)
const accentSoft = computed(() => `hsl(${hue.value} 78% 46%)`)
const accentDeep = computed(() => `hsl(${hue.value} 72% 30%)`)
const lineColor = '#9dc0ff'

// 粒子：同一赛事每次渲染位置一致（用 seed 做伪随机），保证列表与详情页对得上
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

const styleVars = computed(() => ({ '--accent-h': hue.value }))
</script>

<template>
  <div
    class="cover-art relative h-full w-full overflow-hidden"
    :class="variant === 'hero' ? 'cover-art--hero' : variant === 'thumb' ? 'cover-art--thumb' : ''"
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
      </defs>

      <rect width="1600" height="900" :fill="`url(#${uid}-base)`" />
      <ellipse
        :cx="(drift.glowX / 100) * 1600"
        :cy="(drift.glowY / 100) * 900"
        rx="700"
        ry="470"
        :fill="`url(#${uid}-glow)`"
      />
      <rect width="1600" height="900" :fill="`url(#${uid}-grid)`" :mask="`url(#${uid}-gridmask)`" />

      <!-- 悬浮粒子：给纯渐变加一点空气感，位置由 seed 固定 -->
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

      <!-- 底部光边：统一的一道 accent 亮线，强化海报感 -->
      <rect x="0" y="884" width="1600" height="2.5" :fill="accent" opacity="0.45" />
      <rect x="0" y="858" width="1600" height="26" :fill="accent" opacity="0.08" />

      <!-- 斜向光带：两束，极低不透明度，负责把画面拉出纵深 -->
      <g :transform="`rotate(${drift.angle} 1180 430)`">
        <rect x="820" y="-120" width="150" height="1200" :fill="`url(#${uid}-beam)`" />
        <rect x="1060" y="-120" width="62" height="1200" :fill="`url(#${uid}-beam)`" opacity="0.7" />
      </g>

      <!-- 几何母题：同一套线框语言，按类别换形状 -->
      <g
        fill="none"
        :stroke="`url(#${uid}-stroke)`"
        stroke-width="4"
        stroke-linecap="round"
        stroke-linejoin="round"
        :transform="`rotate(${drift.rotate} 1120 430)`"
        style="filter: drop-shadow(0 4px 22px rgb(120 170 255 / 0.45))"
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
          <rect x="900" y="560" width="56" height="120" :fill="accent" stroke="none" opacity="0.3" />
          <rect x="986" y="470" width="56" height="210" :fill="accent" stroke="none" opacity="0.42" />
          <rect x="1072" y="370" width="56" height="310" :fill="accent" stroke="none" opacity="0.56" />
          <rect x="1158" y="270" width="56" height="410" :fill="accent" stroke="none" opacity="0.7" />
          <path d="M880 640 L1300 200" stroke-opacity="0.5" />
          <path d="M1214 200 L1300 200 L1300 286" stroke-opacity="0.7" :stroke="accent" />
          <circle cx="1158" cy="270" r="9" :fill="accent" stroke="none" />
        </template>

        <template v-else>
          <path d="M880 620 Q1080 380 1300 620" stroke-opacity="0.55" />
          <path d="M900 680 Q1080 470 1280 680" stroke-opacity="0.35" />
          <path d="M940 740 Q1080 570 1240 740" stroke-opacity="0.22" />
          <circle cx="1090" cy="500" r="86" :stroke="accent" stroke-opacity="0.55" />
          <circle cx="1090" cy="500" r="34" :fill="accent" stroke="none" opacity="0.7" />
          <circle cx="1250" cy="420" r="12" :fill="accent" stroke="none" opacity="0.55" />
        </template>
      </g>

      <rect width="1600" height="900" :fill="`url(#${uid}-vignette)`" />
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
  background: #071026;
}

.cover-art__pad {
  padding: 3.2cqw;
}

.cover-art__row {
  gap: 2.4cqw;
}

.cover-mark {
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

.cover-mark--wide {
  letter-spacing: -0.04em;
}

.cover-tag {
  color: rgb(226 236 255 / 0.72);
  font-size: clamp(10px, 2.9cqw, 13px);
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cover-art--hero .cover-mark {
  width: clamp(60px, 9cqw, 96px);
  height: clamp(60px, 9cqw, 96px);
  font-size: clamp(20px, 3.4cqw, 34px);
}

.cover-art--hero .cover-tag {
  font-size: clamp(12px, 1.5cqw, 15px);
}

/* 工作台顶部小图（约 96×64）：只留徽记，字与标签都按比例缩，避免溢出 */
.cover-art--thumb .cover-art__pad {
  padding: 8cqw;
}

.cover-art--thumb .cover-mark {
  width: clamp(18px, 26cqw, 30px);
  height: clamp(18px, 26cqw, 30px);
  border-radius: 7px;
  font-size: clamp(10px, 13cqw, 14px);
}

.cover-art--thumb .cover-tag {
  display: none;
}
</style>

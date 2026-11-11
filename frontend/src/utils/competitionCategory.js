/**
 * 竞赛分类字典（前端唯一数据源）
 * ---------------------------------------------------------
 * 第一级大类仅四个：工科 / 理科 / 商科 / 文科。
 * 第二级细分严格按产品给定的 33 个方向维护，筛选菜单与卡片标签共用。
 */

export const MAJOR_ORDER = ['工科', '理科', '商科', '文科']

export const CATEGORY_TREE = {
  工科: [
    '数学建模',
    '程序设计',
    '机器人',
    '工程机械',
    '土木建筑',
    '大数据',
    '交通车辆',
    '航空航天',
    '船舶海洋',
    '环境能源',
    '计算机&信息技术',
    '材料高分子',
    '电子&自动化',
    '人工智能',
    '职业技能'
  ],
  理科: ['数学', '物理', '化学化工', '健康生命&医学', '力学', '农林食品'],
  商科: ['创业', '商业', '创青春'],
  文科: [
    '工业&创意设计',
    '外语',
    '演讲主持&辩论',
    '模特',
    '歌舞书画&摄影',
    '体育',
    '科技文化艺术节',
    'UI设计',
    '服装设计',
    '电子竞技',
    '艺术素养'
  ]
}

// 细分方向 -> 大类（由字典自动派生，避免两处维护不一致）
export const MINOR_TO_MAJOR = Object.fromEntries(
  MAJOR_ORDER.flatMap((major) => CATEGORY_TREE[major].map((minor) => [minor, major]))
)

/**
 * 数据库/后端已经落库的原始 category -> [大类, 细分]
 * 覆盖当前库中的全部取值，并预留常见别名。
 */
const RAW_CATEGORY_ALIASES = {
  创新创业: ['商科', '创业'],
  '创新创业/软件服务': ['商科', '创业'],
  程序设计: ['工科', '程序设计'],
  计算机综合与软件: ['工科', '计算机&信息技术'],
  安全与人工智能: ['工科', '人工智能'],
  '物联网/嵌入式/ICT': ['工科', '计算机&信息技术'],
  '互联网+': ['商科', '创业'],
  创青春: ['商科', '创青春']
}

// 关键词兜底规则：顺序即优先级，越具体的规则越靠前
const KEYWORD_RULES = [
  { pattern: /科技文化艺术节|文化艺术节/i, major: '文科', minor: '科技文化艺术节' },
  { pattern: /数学建模/i, major: '工科', minor: '数学建模' },
  { pattern: /服装(设计)?/i, major: '文科', minor: '服装设计' },
  { pattern: /\bUI\b|ui设计|界面设计/i, major: '文科', minor: 'UI设计' },
  { pattern: /工业设计|创意设计|产品设计|视觉传达|平面设计|设计/i, major: '文科', minor: '工业&创意设计' },
  { pattern: /机器人/i, major: '工科', minor: '机器人' },
  { pattern: /人工智能|\bAI\b|机器学习|深度学习|智能科学/i, major: '工科', minor: '人工智能' },
  { pattern: /大数据|数据科学|数据分析/i, major: '工科', minor: '大数据' },
  { pattern: /土木|建筑|道桥|桥梁/i, major: '工科', minor: '土木建筑' },
  { pattern: /交通|车辆|汽车|轨道/i, major: '工科', minor: '交通车辆' },
  { pattern: /航空|航天|飞行器/i, major: '工科', minor: '航空航天' },
  { pattern: /船舶|海洋|海事/i, major: '工科', minor: '船舶海洋' },
  { pattern: /环境|能源|双碳|低碳/i, major: '工科', minor: '环境能源' },
  { pattern: /材料|高分子/i, major: '工科', minor: '材料高分子' },
  { pattern: /计算机|软件|信息技术|物联网|ICT|网络安全|信息安全|通信|互联网/i, major: '工科', minor: '计算机&信息技术' },
  { pattern: /电子|电气|自动化|嵌入式|控制|仪器/i, major: '工科', minor: '电子&自动化' },
  { pattern: /程序设计|编程|算法/i, major: '工科', minor: '程序设计' },
  { pattern: /工程机械|机械|制造/i, major: '工科', minor: '工程机械' },
  { pattern: /数学/i, major: '理科', minor: '数学' },
  { pattern: /物理/i, major: '理科', minor: '物理' },
  { pattern: /化学|化工/i, major: '理科', minor: '化学化工' },
  { pattern: /生物|生命|医学|医药|健康|护理/i, major: '理科', minor: '健康生命&医学' },
  { pattern: /力学/i, major: '理科', minor: '力学' },
  { pattern: /创青春/i, major: '商科', minor: '创青春' },
  { pattern: /创业|创新|双创|挑战杯/i, major: '商科', minor: '创业' },
  { pattern: /商业|经济|金融|管理|营销|会计|市场|商务|贸易/i, major: '商科', minor: '商业' },
  { pattern: /外语|英语|日语|翻译/i, major: '文科', minor: '外语' },
  { pattern: /演讲|主持|辩论|朗诵/i, major: '文科', minor: '演讲主持&辩论' },
  { pattern: /模特/i, major: '文科', minor: '模特' },
  { pattern: /歌舞|书画|摄影|音乐|舞蹈|美术/i, major: '文科', minor: '歌舞书画&摄影' },
  { pattern: /体育|运动/i, major: '文科', minor: '体育' },
  { pattern: /电子竞技|电竞/i, major: '文科', minor: '电子竞技' },
  { pattern: /艺术素养|艺术/i, major: '文科', minor: '艺术素养' }
]

/**
 * 解析原始分类。
 * @param {string} raw 数据库里具体的类别，如“计算机综合与软件”
 * @returns {{ major: string, minor: string, label: string }} 映射结果；无法识别时 label 保留原文
 */
export function resolveCategory(raw) {
  const text = typeof raw === 'string' ? raw.trim() : ''
  if (!text) return { major: '', minor: '', label: '' }

  // 允许“A/B”“A、B”这类复合分类逐段匹配，但优先整体匹配已有别名
  const segments = [text, ...text.split(/[/、,，|·]+/).map((s) => s.trim()).filter(Boolean)]

  for (const segment of segments) {
    const hit = RAW_CATEGORY_ALIASES[segment]
    if (hit) return { major: hit[0], minor: hit[1], label: `${hit[0]} · ${hit[1]}` }
  }

  for (const segment of segments) {
    const major = MINOR_TO_MAJOR[segment]
    if (major) return { major, minor: segment, label: `${major} · ${segment}` }
  }

  for (const segment of segments) {
    if (MAJOR_ORDER.includes(segment)) return { major: segment, minor: '', label: segment }
  }

  for (const rule of KEYWORD_RULES) {
    if (rule.pattern.test(text)) {
      return { major: rule.major, minor: rule.minor, label: `${rule.major} · ${rule.minor}` }
    }
  }

  return { major: '', minor: '', label: text }
}

/** 卡片标签：大类 + 细分；识别不了时回退原始分类，保证不出现空标签 */
export function categoryTag(raw) {
  return resolveCategory(raw).label
}

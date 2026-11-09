import { formatSignUp, formatContest, parseDate, countdownState, parseDateObject } from '../frontend/src/utils/timeFormat.js'

const cases = [
  ['双日期', formatContest({ contest_start_date: '2026-11-27', contest_end_date: '2026-11-29' }), '2026-11-27 ~ 2026-11-29'],
  ['同一日期', formatContest({ contest_start_date: '2026-11-27', contest_end_date: '2026-11-27' }), '2026-11-27'],
  ['只有开始日', formatContest({ contest_start_date: '2026-11-27', contest_start_month: 11, contest_end_month: 11 }), '2026-11-27'],
  ['无日期降级月份', formatContest({ contest_start_month: 11, contest_end_month: 11 }), '11 月'],
  ['无日期跨月', formatContest({ contest_start_month: 10, contest_end_month: 11 }), '10 月 - 11 月'],
  ['无日期跨年', formatContest({ contest_start_month: 12, contest_end_month: 1 }), '12 月 - 次年 1 月'],
  ['无年月日', formatContest({ contest_start_date: '04-15', contest_end_date: '04-20' }), '04-15 ~ 04-20'],
  ['月份残缺不猜日期', formatContest({ contest_start_date: '2026-04', contest_start_month: 4 }), '4 月'],
  ['全空', formatContest({}), '待定'],
  ['日报双日期', formatSignUp({ reg_start_date: '2026-03-24', reg_end_date: '2026-06-30' }), '2026-03-24 ~ 2026-06-30'],
  ['报名只有截止日', formatSignUp({ reg_end_date: '2026-11-10', reg_end_month: 11 }), '截止 2026-11-10'],
  ['报名降级月份', formatSignUp({ reg_start_month: 3, reg_end_month: 6 }), '3 月 - 6 月'],
  ['报名跨年', formatSignUp({ reg_start_month: 11, reg_end_month: 3, reg_cross_year: 1 }), '11 月 - 次年 3 月'],
  ['报名全空', formatSignUp({}), '待公布'],
  ['非法日期', String(parseDate('2026-13-40')), 'null'],
  ['非日期字符串', String(parseDate('待公布')), 'null'],
]

// ---------------- 倒计时徽章 ----------------
const TODAY = new Date(2026, 10, 9) // 2026-11-09
const cd = (item) => {
  const s = countdownState(item, TODAY)
  return s ? `${s.tone}|${s.text}` : 'null'
}

cases.push(
  // 报名中（11-09 在 08-24 ~ 11-20 之间）→ 11 天
  ['报名中', cd({ reg_start_date: '2026-08-24', reg_end_date: '2026-11-20' }), 'open|报名倒计时 11 天'],
  // 7 天内 → 高危
  ['一周内截止', cd({ reg_start_date: '2026-11-01', reg_end_date: '2026-11-12' }), 'urgent|报名倒计时 3 天'],
  // 就是今天截止
  ['今天截止', cd({ reg_start_date: '2026-11-01', reg_end_date: '2026-11-09' }), 'urgent|今天截止'],
  // 已截止
  ['已截止', cd({ reg_start_date: '2026-03-01', reg_end_date: '2026-03-31' }), 'over|已截止'],
  // 还没开报
  ['未开报', cd({ reg_start_date: '2026-12-01', reg_end_date: '2027-01-10' }), 'soon|距开报 22 天'],
  // 只有报名截止、且已过 → 已截止
  ['只有截止日且已过', cd({ reg_end_date: '2026-06-30' }), 'over|已截止'],
  // 没有报名日，用比赛日兜底
  ['等开赛', cd({ contest_start_date: '2026-12-19' }), 'contest|距开赛 40 天'],
  // 比赛日今天
  ['今天开赛', cd({ contest_start_date: '2026-11-09' }), 'contest|今天开赛'],
  // 比赛日也过去了 → 不显示
  ['全都过去', cd({ contest_start_date: '2026-05-01' }), 'null'],
  // 只有月份 → 不猜日子，不显示
  ['只有月份', cd({ reg_start_month: 11, reg_end_month: 12 }), 'null'],
  // 无年份 MM-DD：已过半年 → 视为明年
  ['无年份跨年', cd({ reg_end_date: '01-15' }), 'open|报名倒计时 67 天'],
  // 非法日期（2 月 30 日）→ 徽章不出现，也不崩
  ['非法日期2月30', cd({ reg_end_date: '2026-02-30' }), 'null'],
  // 时区/时刻无关：同一天 23:59 仍算 0 天
  ['当天深夜', (() => {
    const s = countdownState({ reg_start_date: '2026-11-01', reg_end_date: '2026-11-09' },
      new Date(2026, 10, 9, 23, 59, 59))
    return s ? `${s.tone}|${s.text}` : 'null'
  })(), 'urgent|今天截止']
)

// parseDateObject 细节
cases.push(
  ['parseDateObject 无年份取今年', String(parseDateObject('08-01', TODAY).getFullYear()), '2026'],
  ['parseDateObject 无年份已过半年取明年', String(parseDateObject('01-15', TODAY).getFullYear()), '2027'],
  ['parseDateObject 非法', String(parseDateObject('2026-02-30', TODAY)), 'null'],
  ['parseDateObject 空', String(parseDateObject('', TODAY)), 'null']
)

let bad = 0
for (const [name, got, want] of cases) {
  const ok = got === want
  if (!ok) bad++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}: ${JSON.stringify(got)}${ok ? '' : ' 期望 ' + JSON.stringify(want)}`)
}
console.log(bad ? `\n${bad} 个失败` : '\n全部通过')
process.exit(bad ? 1 : 0)

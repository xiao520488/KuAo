const ROSTER_HOSTS = ['sdaeu.edu.cn']
const NOTICE_PATH_RE = /\/info\/\d{1,6}\/\d{1,6}\.(?:html?|shtml?)$/i
function safeExternalUrl(raw){ if(typeof raw!=='string')return ''; const v=raw.trim(); if(!/^https?:\/\//i.test(v))return ''; try{const u=new URL(v); return (u.protocol==='http:'||u.protocol==='https:')?u.href:''}catch(e){return ''} }
function isRealOfficialSite(raw){ const href=safeExternalUrl(raw); if(!href)return false; try{ const u=new URL(href); if(ROSTER_HOSTS.some(h=>u.hostname===h||u.hostname.endsWith('.'+h)))return false; return !NOTICE_PATH_RE.test(u.pathname) }catch(e){return false} }
const cases = [
  ['https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm', false, '名录目录页'],
  ['http://www.mcm.edu.cn/', true, '真实官网'],
  ['http://tjjmds.ai-learning.net/', true, '统计建模官网'],
  ['https://www.sdaeu.edu.cn/', false, '名录站首页'],
  ['javascript:alert(1)', false, '非法协议'],
  ['', false, '空值'],
  ['http://mit.caai.cn/', true, '真实官网2'],
]
let bad = 0
for (const [url, want, label] of cases) {
  const got = isRealOfficialSite(url)
  const ok = got === want
  if (!ok) bad++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label.padEnd(10)} want=${want} got=${got}  ${url}`)
}
console.log(bad === 0 ? 'ALL PASS' : `${bad} FAILED`)
process.exit(bad === 0 ? 0 : 1)

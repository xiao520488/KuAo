# -*- coding: utf-8 -*-
"""competition_whitelist.py —— 「教育部 84 项核心国赛」白名单（唯一事实源）

来源：全国普通高校大学生竞赛分析报告 · 84 项竞赛目录
      https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm（2026-11-11 抓取，序号 1–84）

用途：
  * 主库清理（cleanup_non_core_races.py）：不在名单里的野生赛事直接删；
  * 候选入库（discover_* / review_candidates / admin 审核）：不是 84 项一律丢弃；
  * 前端展示：既然全是国家级核心赛，`level`（国家级/省级）失去意义，白名单即「核心」标记。

设计：
  * WHITELIST 是只读元组，**改名单只能改这个文件**，不要散落在各处写死；
  * 匹配走「归一化 + 别名 + 最长命中」，容忍：书名号/引号差异、"全国大学生"省略、
    "·"分隔、简称（如「蓝桥杯」「三创赛」「互联网+」）、名称里带年份/届次/赛道后缀。
  * 匹配是**保守**的：宁可漏判（人工审核补），不可误判把水赛放进核心库。
"""
from __future__ import annotations

import re

ROSTER_SOURCE = "https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm"

# (序号, 官方名称, 官网)
WHITELIST: tuple[tuple[int, str, str], ...] = (
    (1, "中国国际大学生创新大赛", "https://cy.ncss.cn/"),
    (2, "“挑战杯”全国大学生课外学术科技作品竞赛", "http://www.tiaozhanbei.net/"),
    (3, "“挑战杯”中国大学生创业计划大赛", "http://www.chuangqingchun.net/"),
    (4, "ACM-ICPC国际大学生程序设计竞赛", "https://acm.cumt.edu.cn/"),
    (5, "全国大学生数学建模竞赛", "http://www.mcm.edu.cn/"),
    (6, "全国大学生电子设计竞赛", "http://www.nuedcchina.com/"),
    (7, "中国大学生医学技术技能大赛", ""),
    (8, "全国大学生机械创新设计大赛", "http://umic.ckcest.cn/"),
    (9, "全国大学生结构设计竞赛", "http://www.structurecontest.com/"),
    (10, "全国大学生广告艺术大赛", "http://www.sun-ada.net/"),
    (11, "全国大学生智能汽车竞赛", "http://www.eepw.com.cn/event/action/freescale_car2012/"),
    (12, "全国大学生电子商务“创新、创意及创业”挑战赛", "http://www.3chuang.net/"),
    (13, "中国大学生工程实践与创新能力大赛", "http://www.gcxl.edu.cn/new/index.html"),
    (14, "全国大学生物流设计大赛", "http://www.clpp.org.cn/"),
    (15, "“外研社·国才杯”“理解当代中国”全国大学生外语能力大赛", "http://uchallenge.unipus.cn/"),
    (16, "两岸新锐设计竞赛·华灿奖", "http://www.huacanjiang.com/home"),
    (17, "全国大学生创新创业训练计划年会展示", "http://gjcxcy.bjtu.edu.cn/Index.aspx"),
    (18, "全国大学生化工设计竞赛", "http://iche.zju.edu.cn/"),
    (19, "全国大学生机器人大赛(CURC)", "http://www.cnrobocon.net/"),
    (20, "全国大学生市场调查与分析大赛", "http://www.china-cssc.org/list-56-1.html"),
    (21, "全国大学生先进成图技术与产品信息建模创新大赛", "http://www.chengtudasai.com/"),
    (22, "全国三维数字化创新设计大赛", "https://3dds.3ddl.net/"),
    (23, "“西门子杯”中国智能制造挑战赛", "http://www.siemenscup-cimc.org.cn/"),
    (24, "中国大学生服务外包创新创业大赛", "http://www.fwwb.org.cn/"),
    (25, "中国大学生计算机设计大赛", "http://jsjds.blcu.edu.cn/"),
    (26, "中国高校计算机大赛", "http://www.c4best.cn/"),
    (27, "蓝桥杯全国软件和信息技术专业人才大赛", "http://dasai.lanqiao.cn/"),
    (28, "米兰设计周--中国高校设计学科师生优秀作品展", "http://www.dandad.cn/"),
    (29, "全国大学生地质技能竞赛", "https://yuanxi.cugb.edu.cn/competition/"),
    (30, "全国大学生光电设计竞赛", "http://gd.p.moocollege.com/"),
    (31, "全国大学生集成电路创新创业大赛", "http://univ.ciciec.com/"),
    (32, "全国大学生金相技能大赛", "http://www.cnzjjx.cn/"),
    (33, "全国大学生信息安全竞赛", "http://www.ciscn.cn/"),
    (34, "未来设计师·全国高校数字艺术设计大赛", "https://www.ncda.org.cn/"),
    (35, "全国周培源大学生力学竞赛", "http://zpy.cstam.org.cn/"),
    (36, "中国大学生机械工程创新创意大赛", "http://www.gczbds.org"),
    (37, "中国机器人大赛暨RoboCup机器人世界杯中国赛", "http://crc.drct-caa.org.cn/"),
    (38, "“中国软件杯”大学生软件设计大赛", "http://www.cnsoftbei.com/"),
    (39, "中美青年创客大赛", "https://www.eol.cn/html/lx/maker/"),
    (40, "睿抗机器人开发者大赛(RAICOM)", "https://www.robocom.com.cn/"),
    (41, "“大唐杯”全国大学生新一代信息通信技术大赛", "https://dtcup.dtxiaotangren.com"),
    (42, "华为ICT大赛", "https://e.huawei.com/cn/talent/ict-academy/"),
    (43, "全国大学生嵌入式芯片与系统设计竞赛", "http://www.socchina.net/"),
    (44, "全国大学生生命科学竞赛(CULSC)", "https://www.culsc.cn/"),
    (45, "全国大学生物理实验竞赛", "http://wlsycx.moocollege.com/"),
    (46, "全国高校BIM毕业设计创新大赛", "http://gxbsxs.glodonedu.com/index"),
    (47, "全国高校商业精英挑战赛", "http://cubec.org.cn/"),
    (48, "“学创杯”全国大学生创业综合模拟大赛", "http://www.bster.cn/cyds/index"),
    (49, "中国高校智能机器人创意大赛", "http://www.robotcontest.cn/"),
    (50, "中国好创意暨全国数字艺术设计大赛", "https://www.cdec.org.cn/"),
    (51, "中国机器人及人工智能大赛", "https://developer.apollo.auto/devcenter/gameOperations_cn.html"),
    (52, "全国大学生节能减排社会实践与科技竞赛", "http://www.jienengjianpai.org/"),
    (53, "“21世纪杯”全国英语演讲比赛", "https://contest.i21st.cn/"),
    (54, "iCAN大学生创新创业大赛", "http://www.g-ican.com"),
    (55, "“工行杯”全国大学生金融科技创新大赛", "https://www.gonghangbei.com/"),
    (56, "中华经典诵写讲大赛", "https://www.jingdiansxj.cn/home"),
    (57, "“外教社杯”全国高校学生跨文化能力大赛", ""),
    (58, "百度之星程序设计大赛", "https://star.baidu.com/"),
    (59, "全国大学生工业设计大赛", "https://www.cuidc.net/"),
    (60, "全国大学生水利创新设计大赛", ""),
    (61, "全国大学生化工实验大赛", "http://www.cteic.com/higherEducation-199.html"),
    (62, "全国大学生化学实验创新设计大赛", "https://cid.nju.edu.cn/"),
    (63, "全国大学生计算机系统能力大赛", "https://compiler.educg.net/"),
    (64, "全国大学生花园设计建造竞赛", ""),
    (65, "全国大学生物联网设计竞赛", "https://developer.huaweicloud.com/college/wulianwang.html"),
    (66, "全国大学生信息安全与对抗技术竞赛", "http://www.isclab.org.cn"),
    (67, "全国大学生测绘学科创新创业智能大赛", "http://smt.whu.edu.cn/index.htm"),
    (68, "全国大学生统计建模大赛", "http://tjjmds.ai-learning.net/"),
    (69, "全国大学生能源经济学术创意大赛", "http://energy.ckcest.cn/eneco/contribution/index.html"),
    (70, "全国大学生基础医学创新研究暨实验设计论坛(大赛)", "http://www.jcyxds.com/"),
    (71, "全国大学生数字媒体科技作品及创意竞赛", "http://mit.caai.cn/"),
    (72, "全国本科院校税收风险管控赛例大赛", ""),
    (73, "全国企业模拟竞赛大赛", "http://www.ibizsim.cn/"),
    (74, "全国高等院校数智化企业经营沙盘大赛", "http://spbk.seentao.com"),
    (75, "全国数字建筑创新应用大赛", "http://bisai.ccen.com.cn"),
    (76, "全球校园人工智能算法精英大赛", "https://developer.huawei.com/consumer/cn/"),
    (77, "国际大学生智能农业装备创新大赛", "http://uiaec.ujs.edu.cn"),
    (78, "“科云杯”全国大学生财会职业能力大赛", "http://match.xmkeyun.com.cn/"),
    (79, "全国职业院校技能大赛", "https://chinaskills.icve.com.cn"),
    (80, "全国大学生机器人大赛-RoboTac", "http://www.robotac.cn"),
    (81, "世界技能大赛", "http://worldskillschina.mohrss.gov.cn/"),
    (82, "世界技能大赛中国选拔赛", "http://worldskillschina.mohrss.gov.cn/"),
    (83, "一带一路暨金砖国家技能发展与技术创新大赛", "http://www.brskills.com"),
    (84, "码蹄杯全国职业院校程序设计大赛", "https://matiji.net/matibei"),
)

# 序号 -> 常见简称/别称。只放「专指性强」的，避免把泛称（全国大学生大赛）当别名。
ALIASES: dict[int, tuple[str, ...]] = {
    1: ("互联网+", "互联网+大赛", "互联网加", "国创赛", "中国国际互联网+"),
    2: ("挑战杯", "大挑", "课外学术科技作品竞赛"),
    3: ("创青春", "小挑", "挑战杯创业计划竞赛", "挑战杯创业计划大赛",
        "挑战杯中国大学生创业计划竞赛", "创业计划竞赛"),
    4: ("ACM-ICPC", "ICPC", "ACM程序设计竞赛"),
    5: ("数学建模", "国赛数模", "CUMCM"),
    6: ("电子设计竞赛", "电赛", "NUEDC"),
    8: ("机械创新设计大赛", "机创赛"),
    10: ("大广赛", "广告艺术大赛"),
    11: ("智能汽车竞赛", "恩智浦杯"),
    12: ("三创赛", "电子商务三创赛", "创新创意及创业挑战赛"),
    13: ("工程训练综合能力竞赛", "工训赛", "工程实践与创新能力大赛"),
    15: ("外研社国才杯", "国才杯", "理解当代中国"),
    16: ("华灿奖",),
    17: ("大创年会", "创新创业年会", "国创年会"),
    19: ("CURC", "全国大学生机器人大赛"),
    20: ("市场调查与分析大赛", "市调赛"),
    22: ("3D大赛", "三维数字化创新设计大赛"),
    23: ("西门子杯", "智能制造挑战赛"),
    24: ("服务外包创新创业大赛", "服创赛"),
    25: ("计算机设计大赛", "计设赛", "4C大赛"),
    26: ("C4", "高校计算机大赛"),
    27: ("蓝桥杯", "蓝桥"),
    30: ("光电设计竞赛",),
    31: ("集成电路创新创业大赛", "集创赛"),
    32: ("金相技能大赛", "金相大赛"),
    33: ("信息安全竞赛", "信安赛"),
    34: ("未来设计师", "NCDA"),
    36: ("机械工程创新创意大赛",),
    37: ("RoboCup", "中国机器人大赛"),
    38: ("软件杯", "中国软件杯"),
    40: ("睿抗", "RAICOM", "RoboCom"),
    41: ("大唐杯",),
    42: ("ICT大赛", "华为ICT"),
    43: ("嵌入式芯片与系统设计竞赛", "嵌入式竞赛", "SOC竞赛"),
    44: ("生命科学竞赛", "CULSC"),
    45: ("物理实验竞赛",),
    46: ("BIM毕业设计创新大赛", "BIM大赛"),
    47: ("商业精英挑战赛",),
    48: ("学创杯", "创业综合模拟大赛"),
    49: ("智能机器人创意大赛",),
    50: ("中国好创意", "数字艺术设计大赛"),
    51: ("机器人及人工智能大赛", "中国机器人及人工智能大赛"),
    52: ("节能减排", "节能减排大赛"),
    53: ("21世纪杯", "21世纪英语演讲"),
    54: ("iCAN",),
    55: ("工行杯",),
    56: ("中华经典诵写讲", "诵写讲大赛"),
    57: ("外教社杯", "跨文化能力大赛"),
    58: ("百度之星",),
    59: ("工业设计大赛",),
    60: ("水利创新设计大赛",),
    61: ("化工实验大赛",),
    62: ("化学实验创新设计大赛",),
    63: ("计算机系统能力大赛", "系统能力大赛", "编译系统设计赛"),
    64: ("花园设计建造竞赛",),
    65: ("物联网设计竞赛", "华为物联网"),
    66: ("信息安全与对抗", "ISCC"),
    67: ("测绘学科创新创业智能大赛", "测绘大赛"),
    68: ("统计建模大赛",),
    69: ("能源经济学术创意大赛", "能源经济大赛"),
    70: ("基础医学创新研究", "基础医学大赛"),
    71: ("数字媒体科技作品", "数媒竞赛"),
    72: ("税收风险管控",),
    73: ("企业模拟竞赛",),
    74: ("数智化企业经营沙盘", "沙盘大赛"),
    75: ("数字建筑创新应用大赛",),
    76: ("全球校园AI算法", "算法精英大赛"),
    77: ("智能农业装备",),
    78: ("科云杯",),
    79: ("职业院校技能大赛",),
    80: ("RoboTac",),
    81: ("世界技能大赛",),
    83: ("金砖大赛", "一带一路暨金砖"),
    84: ("码蹄杯", "Matibei"),
}

# 省级 / 区域级赛事字眼（命中即「非核心」，除非有真实获奖作品）
PROVINCIAL_RE = re.compile(
    r"省赛|省级|省内|省域|区域赛|区域级|赛区|分赛区|片区赛|市赛|市级|校赛|"
    r"选拔赛|初赛|分站赛|赛区选拔",
)

_PUNCT_RE = re.compile(r"[\s\u3000·・．.、，,;；:：!！?？\"'“”‘’《》〈〉<>()（）\[\]【】—\-_/\\|]+")
_TAIL_RE = re.compile(r"[①-⑳]|[-—]?[①②③④⑤].*$")
_YEAR_RE = re.compile(r"(19|20)\d{2}\s*年?|第[一二三四五六七八九十百零\d]+届|20\d{2}")


def normalize(text: str) -> str:
    """归一化：去标点/空白、去年份与届次、去序号尾巴、小写。"""
    if not text:
        return ""
    s = str(text).strip()
    s = _TAIL_RE.sub("", s)          # 砍掉「-①英语演讲，②…」这类并列尾巴
    s = _YEAR_RE.sub("", s)          # 砍掉年份/届次
    s = _PUNCT_RE.sub("", s)
    return s.lower()


def _base_name(name: str) -> str:
    """官方名去掉并列尾巴，用于匹配。"""
    return _TAIL_RE.sub("", name or "")


def _build_index():
    """预编译：[(序号, 归一化名, 归一化别名列表)]，按名称长度降序，保证最长命中优先。"""
    rows = []
    for idx, name, url in WHITELIST:
        keys = [normalize(_base_name(name))]
        keys += [normalize(a) for a in ALIASES.get(idx, ())]
        keys = [k for k in keys if len(k) >= 3]
        rows.append((idx, name, url, keys))
    rows.sort(key=lambda r: -max((len(k) for k in r[3]), default=0))
    return tuple(rows)


_INDEX = _build_index()
_BY_INDEX = {idx: (idx, name, url) for idx, name, url in WHITELIST}


def match(name: str):
    """返回命中的白名单条目 dict（index/name/url/matched），未命中返回 None。

    三趟匹配，优先级从高到低：
      ① 完全相等 —— 「世界技能大赛」必须命中 #81，而不是被 #82（中国选拔赛）截走；
      ② 键被名称包含 —— 「2026年高教社杯全国大学生数学建模竞赛」命中 #5；
      ③ 名称被键包含 —— 名称偏短时兜底（如只写了「大唐杯」）。
    每趟内部取**最长键**，避免「挑战杯」这种短别名抢在「挑战杯中国大学生创业计划
    竞赛」前面（#2 vs #3 必须分清）。
    """
    norm = normalize(name)
    if not norm:
        return None

    def _pick(predicate):
        best = None
        for idx, _wl_name, _url, keys in _INDEX:
            for key in keys:
                if len(key) < 3 or not predicate(norm, key):
                    continue
                if best is None or len(key) > best[0]:
                    best = (len(key), idx, key)
        return best

    best = (_pick(lambda n, k: n == k)
            or _pick(lambda n, k: k in n)
            or _pick(lambda n, k: len(n) >= 4 and n in k))
    if best is None:
        return None
    idx = best[1]
    _, wl_name, url = _BY_INDEX[idx]
    return {"index": idx, "name": wl_name, "url": url, "matched": best[2]}


def is_whitelisted(name: str) -> bool:
    return match(name) is not None


def is_provincial(name: str) -> bool:
    """名称里明确带省级/区域级字眼。"""
    return bool(PROVINCIAL_RE.search(name or ""))


def gate(name: str) -> tuple[bool, str]:
    """入库闸门（候选/主表统一口径）。

    返回 (allowed, reason)：
      * 省级/区域赛 -> False（"省级/区域赛事"）
      * 不在 84 项   -> False（"不在教育部 84 项白名单"）
      * 命中          -> True（"#序号 官方名"）
    """
    hit = match(name)
    if is_provincial(name):
        return False, "省级/区域赛事（非 84 项国家级核心赛）"
    if hit is None:
        return False, "不在教育部 84 项白名单"
    return True, "#%d %s" % (hit["index"], hit["name"])


def summary() -> str:
    return "白名单 %d 项（来源：%s）" % (len(WHITELIST), ROSTER_SOURCE)


# ---------------------------------------------------------------- 学科分类
# 分类取值**直接用前端 33 个细分方向的原名**（frontend/src/utils/competitionCategory.js），
# 这样 resolveCategory() 能一次命中 MINOR_TO_MAJOR，不用再维护一层「原始类目 -> 大类」的
# 别名表。84 项里有两种前端暂时没有的方向（职业技能、农林食品），已同步补进前端字典。
# 规则顺序敏感：越具体的放前面，通用词放后面。
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("数学建模", ("数学建模", "统计建模")),
    ("健康生命&医学", ("医学", "护理", "药学", "生命科学", "生物医学", "食品")),
    ("农林食品", ("农业", "林业", "兽医", "水产", "园艺", "农林")),
    ("外语", ("外语", "英语", "翻译", "跨文化")),
    ("演讲主持&辩论", ("演讲", "主持", "辩论")),
    ("艺术素养", ("诵写讲", "书写", "经典诵")),
    ("工业&创意设计", ("广告艺术", "艺术设计", "视觉传达", "数字艺术", "华灿奖", "好创意",
                   "米兰设计周", "工业设计", "数字媒体", "美术", "动画", "创意设计")),
    ("土木建筑", ("结构设计", "建筑", "bim", "土木", "桥梁", "水利", "花园", "城乡规划",
              "测绘", "地质", "遥感", "地理信息")),
    ("化学化工", ("化工", "化学", "金相", "冶金")),
    ("材料高分子", ("材料", "高分子")),
    ("物理", ("物理", "光电", "光学")),
    ("力学", ("力学",)),
    ("环境能源", ("节能减排", "能源", "环境", "电力", "电气", "双碳")),
    ("工程机械", ("机械", "汽车", "车辆", "成图", "三维数字化", "智能制造", "工程训练",
              "工程实践")),
    ("电子&自动化", ("电子设计", "集成电路", "芯片", "通信", "半导体", "嵌入式", "物联网",
                 "ict", "仪器", "自动化")),
    ("商业", ("市场调查", "商业", "财会", "会计", "金融", "税收", "经济", "企业模拟",
            "沙盘", "物流", "管理", "营销", "审计", "贸易")),
    ("机器人", ("机器人", "robocup", "robomaster", "robotac", "无人机", "智能装备")),
    ("程序设计", ("程序设计", "编程", "算法", "acm", "icpc", "蓝桥", "码蹄", "软件测试")),
    ("人工智能", ("人工智能", "信息安全", "网络安全", "大数据", "智能", "数据科学")),
    ("计算机&信息技术", ("计算机", "信息技术", "数字化", "互联网", "软件", "云计算")),
    ("创业", ("创新创业", "创业", "创客", "电子商务", "三创", "挑战杯", "创新大赛")),
    ("创青春", ("创青春",)),
    ("职业技能", ("职业技能", "技能大赛", "世界技能", "金砖", "一带一路", "职业院校")),
)

DEFAULT_CATEGORY = "计算机&信息技术"


def guess_category(name: str) -> str:
    """按名称给 84 项目录里的赛事归一个前端细分方向（只用于核心赛，别复用到野生赛事）。"""
    text = (name or "").lower()
    for category, keywords in CATEGORY_RULES:
        if any(k.lower() in text for k in keywords):
            return category
    return DEFAULT_CATEGORY


if __name__ == "__main__":
    import sys

    print(summary())
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            hit = match(arg)
            tag = "省级/区域" if is_provincial(arg) else ""
            if hit:
                print("  ✓ %-40s -> #%d %s %s" % (arg, hit["index"], hit["name"], tag))
            else:
                print("  ✗ %-40s -> 不在白名单 %s" % (arg, tag))
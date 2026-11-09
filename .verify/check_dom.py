# -*- coding: utf-8 -*-
"""用 DOM 断言首页/列表页渲染结果：卡片数量、顺序、月份兜底卡是否无倒计时徽章。"""
import re
import subprocess
import sys

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def dom(url: str) -> str:
    out = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--dump-dom",
         "--virtual-time-budget=20000", url],
        capture_output=True,
    )
    return out.stdout.decode("utf-8", "replace")


def cards(html: str) -> list:
    """按出现顺序提取卡片标题。"""
    return re.findall(r'class="[^"]*comp-title[^"]*"[^>]*>([^<]+)<', html) or \
        re.findall(r'<h3[^>]*>([^<]{4,60})</h3>', html)


home = dom("http://localhost:5173/")
badges = len(re.findall(r'class="[^"]*countdown-badge', home))
titles = cards(home)
print("首页卡片标题：", [t.strip()[:24] for t in titles])
print("首页倒计时徽章数：", badges)

# 首页热门区：找 4 个热门卡所在片段，检查只有带日期的那个有徽章
print("首页出现 '已截止' 徽章文案：", home.count(">已截止<"))
print("首页出现 '报名倒计时'：", home.count("报名倒计时"))

lst = dom("http://localhost:5173/competitions")
lt = cards(lst)
print("\n列表页卡片数：", len(lt))
print("列表页前 8 张：", [t.strip()[:22] for t in lt[:8]])
print("列表页徽章数：", len(re.findall(r'class="[^"]*countdown-badge', lst)))
print("列表页含 '共 33 项'：", "共 33 项" in lst)

if not titles:
    sys.exit(1)
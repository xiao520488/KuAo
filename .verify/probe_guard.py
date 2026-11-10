import os, sys
BASE = r"D:\agent-lab\KuAo-Platform"
sys.path.insert(0, os.path.join(BASE, "backend"))
os.chdir(os.path.join(BASE, "backend"))
import image_guard
urls = [
 "https://publicqn.saikr.com/saikr/contest/1776911593416/1776911593416dl",
 "https://publicqn.saikr.com/saikr/contest/1776939985792/1776939985792za",
 "https://publicqn.saikr.com/saikr/contest/1776929254750/1776929254750iq",
]
for u in urls:
    ok, reason, info = image_guard.check_image(u)
    print(ok, reason, {k: info.get(k) for k in ("width","height","mode")} if isinstance(info, dict) else info)

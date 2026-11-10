import re, sys
html = open(sys.argv[1], encoding="utf-8", errors="replace").read()
text = re.sub(r"<script[\s\S]*?</script>", " ", html)
text = re.sub(r"<[^>]+>", "\n", text)
lines = [l.strip() for l in text.splitlines() if l.strip()]
for i, l in enumerate(lines):
    if l in ("报名时间", "比赛时间", "报名阶段", "比赛阶段"):
        nxt = lines[i+1] if i+1 < len(lines) else ""
        print(f"{l}: {nxt}")
print("has_zanwei:", text.count("暂未收录"))

import io

parts = []
for i in (0, 110, 220):
    p = 'tmpview/Comp_%03d.txt' % i
    with io.open(p, encoding='utf-8') as f:
        parts.append(f.read())

text = '\n'.join(parts)
with io.open('src/views/CompetitionsView.vue', 'w', encoding='utf-8') as f:
    f.write(text)

print('restored bytes:', len(text.encode('utf-8')))

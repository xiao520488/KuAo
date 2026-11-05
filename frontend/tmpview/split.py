import sys, os
src = sys.argv[1]
name = sys.argv[2]
step = int(sys.argv[3]) if len(sys.argv) > 3 else 110
L = open(src, encoding='utf-8').read().split('\n')
print(name, len(L))
for i in range(0, len(L), step):
    open('tmpview/%s_%03d.txt' % (name, i), 'w', encoding='utf-8').write('\n'.join(L[i:i+step]))

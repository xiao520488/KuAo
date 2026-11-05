import io, os

p1 = io.open('tmpview/profile_part1.txt', encoding='utf-8').read()
p2 = io.open('tmpview/profile_part2.txt', encoding='utf-8').read()

out = p1 + p2
assert out.count('<script setup>') == 1
assert out.count('</script>') == 1
assert out.count('\n<template>') == 1, out.count('\n<template>')

# div 配平（含 <div v-if / <div class 等所有写法）
opens = out.count('<div')
closes = out.count('</div>')
print('div open/close:', opens, closes)
assert opens == closes, (opens, closes)

# 内联 <template v-if> 配平
to = out.count('<template v-')
tc = out.count('</template>') - 1  # 减去根模板闭合
print('inline template:', to, tc)
assert to == tc, (to, tc)

io.open('src/views/ProfileView.vue', 'w', encoding='utf-8', newline='\n').write(out)
print('written bytes:', os.path.getsize('src/views/ProfileView.vue'))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性数据订正：把链接指向了别的小区的条目标记为 excluded（不再上图）。

背景：`b2` 我们记为「深水埗 海柏汇 $15,000」，但它的 28Hse 链接
（property-4018339）实际打开是「维港湾 $35,000」——历史轮次的楼宇与链接错配，
与之前订正过的 a1/a7/a18/b3 属同一类问题。
"""
import json
import os

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'spots.json')

FIX = {
    'b2': '链接指向「维港湾」（$35,000），与本条记录的「海柏汇」不符，属历史数据错配',
}

spots = json.load(open(STATE, encoding='utf-8'))
n = 0
for s in spots:
    if s['id'] in FIX:
        s['excluded'] = True
        s['excludeWhy'] = FIX[s['id']]
        n += 1
json.dump(spots, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('已标记剔除 %d 条：%s' % (n, ', '.join(FIX)))

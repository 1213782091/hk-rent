#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data/spots.json 注入 template/index.html，生成可发布的 index.html。

只渲染「在架」房源；已下架的保留在状态文件里，但不再上图。
"""
import json
import os
import re
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, 'data', 'spots.json')
TPL = os.path.join(ROOT, 'template', 'index.html')
OUT = os.path.join(ROOT, 'index.html')

# 页面实际用到的字段，其余（delisted / lastSeen / priceHistory）不写进 HTML
KEEP = ('id', 'name', 'addr', 'price', 'area', 'room', 'floor', 'year', 'ynote',
        'note', 'url', 'lat', 'lng', 'ride', 'feed', 'feedMin', 'ok', 'walk', 'est')


def main():
    with open(STATE, encoding='utf-8') as f:
        spots = json.load(f)

    live = [s for s in spots if not s.get('delisted') and not s.get('excluded')]
    gone = [s for s in spots if s.get('delisted')]
    dropped = [s for s in spots if s.get('excluded') and not s.get('delisted')]

    asof = max((s.get('lastSeen') or '') for s in spots) or datetime.date.today().isoformat()
    meta = {
        'asof': asof,
        'total': len(live),
        'delisted': len(gone),
        'excluded': len(dropped),
        'generated': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    }

    slim = [{k: s[k] for k in KEEP if k in s} for s in live]

    tpl = open(TPL, encoding='utf-8').read()
    if '/*__SPOTS__*/' not in tpl or '/*__META__*/' not in tpl:
        raise SystemExit('模板缺少 __SPOTS__ / __META__ 占位符')

    html = re.sub(r'/\*__SPOTS__\*/\[.*?\]', lambda m: '/*__SPOTS__*/' + json.dumps(slim, ensure_ascii=False), tpl, count=1, flags=re.S)
    html = re.sub(r'/\*__META__\*/\{.*?\}', lambda m: '/*__META__*/' + json.dumps(meta, ensure_ascii=False), html, count=1, flags=re.S)

    if '__SPOTS__' in html.replace('/*__SPOTS__*/', '') or html.count('/*__META__*/') != 1:
        raise SystemExit('占位符替换异常')

    with open(OUT, 'w', encoding='utf-8', newline='') as f:
        f.write(html)

    print('已生成 index.html  %d 字节' % len(html.encode('utf-8')))
    print('在架 %d 套 / 已下架 %d 套 / 已剔除 %d 套 / 数据截至 %s' % (len(live), len(gone), len(dropped), asof))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量巡检：逐个核对已入图房源的在架状态与最新租金。

用法：
    python scrape.py              # 全量巡检（默认）
    python scrape.py --limit 5    # 只查前 5 条，用于本地试跑
    python scrape.py --id f1      # 只查指定 id
    python scrape.py --dry        # 只打印结果，不写回 spots.json

判定逻辑（三条独立信号，保守处理）：
    1. HTTP 404 / 410                      → 已下架
    2. 页面出现「已下架 / 已租出 / 已成交」等字样   → 已下架
    3. <title> 里找不到 #房源编号             → 已下架（多半被重定向到列表页）
    抓不到租金但上述信号都没触发            → 保留在架，只在日志里标注「解析失败」
"""
import argparse
import json
import os
import re
import sys
import time
import datetime
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, 'data', 'spots.json')
REPORT = os.path.join(ROOT, 'data', 'changes.md')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
GONE_WORDS = ['已下架', '已租出', '已成交', '楼盘不存在', '找不到该', '已停售', '已售出', '已租']

RE_PRICE = re.compile(r'每月租金[\s\S]{0,160}?租\s*([\d,]+)\s*元')
RE_PRICE2 = re.compile(r'\b租\s*([\d,]{3,})\s*元')
# 「实用面积」和数字之间隔着 </td>、<div class="pairValue"> 等标签，必须允许跨标签
RE_AREA = re.compile(r'实用面积[\s\S]{0,240}?([\d,]+)\s*平方尺')
RE_FT = re.compile(r'@\s*([\d.]+)\s*元')
RE_AGE = re.compile(r'屋苑楼龄[:：]\s*(\d+)\s*年')
RE_UPD = re.compile(r'更新[:：]\s*(\d{4}-\d{2}-\d{2})')
RE_TITLE = re.compile(r'<title>([^<]*)</title>')


def fetch(url, timeout=25, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': UA,
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'zh-HK,zh;q=0.9,en;q=0.8',
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return e.code, ''
            last = e
        except Exception as e:                      # noqa: BLE001
            last = e
        time.sleep(1.5 * (i + 1))
    return None, str(last)


def parse(html, sid):
    """返回 dict：price / area / ft / age / updated / live"""
    out = {'price': None, 'area': None, 'ft': None, 'age': None, 'updated': None}

    m = RE_TITLE.search(html)
    title = m.group(1) if m else ''
    out['title'] = title
    out['live'] = ('#' + sid) in title
    # 标题格式固定为「<楼盘名> #<编号> 租盘楼盘详细资料 | 28Hse 香港屋网」
    out['site'] = title.split('#')[0].strip() if '#' in title else ''

    m = RE_PRICE.search(html) or RE_PRICE2.search(html)
    if m:
        out['price'] = int(m.group(1).replace(',', ''))

    m = RE_AREA.search(html)
    if m:
        out['area'] = int(m.group(1).replace(',', ''))

    m = RE_FT.search(html)
    if m:
        out['ft'] = float(m.group(1))

    m = RE_AGE.search(html)
    if m:
        out['age'] = int(m.group(1))

    m = RE_UPD.search(html)
    if m:
        out['updated'] = m.group(1)

    return out


def check(spot):
    # 房源编号取自 url 末尾（如 .../property-4000397 → 4000397），
    # 不能用内部 id（f1 / e12 之类），那个不会出现在页面标题里
    sid = spot['url'].rstrip('/').rsplit('-', 1)[-1]
    status, html = fetch(spot['url'])

    if status in (404, 410):
        return spot, {'live': False, 'why': 'HTTP %s' % status, 'price': None}

    if not html:
        return spot, {'live': None, 'why': '网络失败（保留原状）', 'price': None}

    if any(w in html for w in GONE_WORDS):
        return spot, {'live': False, 'why': '页面出现下架字样', 'price': None}

    info = parse(html, sid)

    if not info['live']:
        return spot, {'live': False, 'why': '标题中无 #%s（疑似重定向）' % sid, 'price': None}

    return spot, {
        'live': True,
        'why': '',
        'price': info['price'],
        'area': info['area'],
        'ft': info['ft'],
        'age': info['age'],
        'updated': info['updated'],
        'site': info.get('site', ''),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--id', default='')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--workers', type=int, default=4)
    args = ap.parse_args()

    spots = json.load(open(STATE, encoding='utf-8'))
    live_all = [s for s in spots if not s.get('delisted') and not s.get('excluded')]
    todo = live_all
    if args.id:
        todo = [s for s in todo if s['id'] == args.id]
    if args.limit:
        todo = todo[:args.limit]

    today = datetime.date.today().isoformat()
    print('待巡检 %d 套（总 %d 套，其中已下架 %d 套跳过）' % (len(todo), len(spots), len(spots) - len(live_all)))
    print('数据日期 %s' % today)

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (spot, res) in enumerate(ex.map(check, todo), 1):
            results.append((spot, res))
            if i % 20 == 0 or i == len(todo):
                print('  %d/%d  已用 %.0fs' % (i, len(todo), time.time() - t0))

    gone, changed, kept, unknown = [], [], 0, 0
    for spot, res in results:
        if res['live'] is None:
            unknown += 1
            continue
        if not res['live']:
            spot['delisted'] = True
            spot['goneWhy'] = res['why']
            spot['lastSeen'] = spot.get('lastSeen') or today
            gone.append((spot, res))
            continue
        kept += 1
        spot['lastSeen'] = today
        spot.pop('goneWhy', None)
        # 记下页面标题里的楼盘名。注意：我们多数条目记的是「地址」（如「湾仔 轩尼诗道231号」），
        # 而 28Hse 标题给的是「楼名」（如「怡明阁」），两者不同不代表错配，所以只记录不报警。
        site = (res.get('site') or '').strip()
        if site:
            spot['siteName'] = site
        p = res.get('price')
        if p and p != spot.get('price'):
            old = spot['price']
            spot.setdefault('priceHistory', []).append({'date': today, 'price': p})
            changed.append((spot, old, p))
            spot['price'] = p

    # 价格变动超过 30% 的，多半是链接指向了另一套房，而不是真的调价
    big = [c for c in changed if abs(c[2] - c[1]) / c[1] > 0.30]

    print('')
    print('在架 %d ｜ 新增下架 %d ｜ 改价 %d（其中大幅 %d）｜ 网络失败 %d'
          % (kept, len(gone), len(changed), len(big), unknown))

    lines = ['# 数据变化报告', '', '巡检日期：%s' % today, '',
             '在架 **%d** 套 ｜ 本次新发现下架 **%d** 套 ｜ 改价 **%d** 套 ｜ 网络失败 **%d** 套'
             % (kept, len(gone), len(changed), unknown), '']
    if gone:
        lines += ['## 已下架', '']
        for s, r in gone:
            lines.append('- `%s` %s（%s）— %s' % (s['id'], s['name'], s['addr'], r['why']))
        lines.append('')
    if changed:
        lines += ['## 租金变化', '']
        for s, old, new in sorted(changed, key=lambda c: -abs(c[2] - c[1]) / c[1]):
            arrow = '↓' if new < old else '↑'
            pct = (new - old) / old * 100
            flag = ''
            if abs(pct) > 30:
                flag = '　⚠️ **幅度异常，建议核对链接是否指向另一套房**（页面楼名：%s）' % s.get('siteName', '?')
            lines.append('- `%s` %s：HK$%s → HK$%s　%s%.1f%%%s'
                         % (s['id'], s['name'], format(old, ','), format(new, ','), arrow, abs(pct), flag))
        lines.append('')
    if not gone and not changed:
        lines.append('本次巡检没有发现变化。')
        lines.append('')

    if args.dry:
        print('--dry：不写回文件')
    else:
        json.dump(spots, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        open(REPORT, 'w', encoding='utf-8', newline='').write('\n'.join(lines))
        print('已写回 data/spots.json 与 data/changes.md')


if __name__ == '__main__':
    main()

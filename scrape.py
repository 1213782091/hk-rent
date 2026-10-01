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

写入策略：
    只有出现**实质变化**（房源下架 / 租金变动）才写回 spots.json 并向 changes.md 追加一条记录。
    没有实质变化时**一个文件都不碰** —— 这样仓库里每一次自动提交都对应一次真实变化。
    无论有无变化，本次运行的摘要都会写到环境变量 RUN_SUMMARY 指定的文件（供 Actions 展示）。
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

REPORT_HEAD = """# 数据变化记录

本文件**只记录发生实质变化**的巡检结果（房源下架 / 租金变动），按时间倒序，最新在最上面。

没有变化的巡检不写入 —— 每次运行的完整结果见仓库 **Actions** 页的运行摘要。

<!-- NEW -->
"""

MAX_ENTRIES = 200


def prepend_report(entry):
    """把本次条目插到 <!-- NEW --> 标记下方，超出上限则截断最旧的。"""
    try:
        old = open(REPORT, encoding='utf-8').read()
    except FileNotFoundError:
        old = ''
    if '<!-- NEW -->' not in old:
        old = REPORT_HEAD + '\n' + old
    head, _, tail = old.partition('<!-- NEW -->')
    body = '\n'.join(entry).rstrip() + '\n\n'
    merged = head + '<!-- NEW -->\n\n' + body + tail.lstrip('\n')

    parts = merged.split('\n## ')
    if len(parts) > MAX_ENTRIES + 1:
        merged = parts[0] + '\n## ' + '\n## '.join(parts[1:MAX_ENTRIES + 1]).rstrip() + '\n'
    open(REPORT, 'w', encoding='utf-8', newline='').write(merged)


def write_summary(lines):
    """把本次运行摘要写到 RUN_SUMMARY 指定的文件（供 Actions 展示），不写进仓库。"""
    p = os.environ.get('RUN_SUMMARY')
    if not p:
        return
    try:
        open(p, 'w', encoding='utf-8', newline='').write('\n'.join(lines) + '\n')
    except OSError:
        pass

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

    # 「实质变化」= 有房源下架，或有租金变动。
    # 只有实质变化才落盘 —— 这样 git 历史里每一次提交都对应一次真实变化，
    # 不会被 lastSeen 这类「心跳」字段搅成每天一堆无意义提交。
    meaningful = bool(gone or changed)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')

    summary = ['### 本次巡检结果', '',
               '巡检时间：%s UTC　｜　数据日期：%s' % (stamp, today), '',
               '| 指标 | 数值 |', '|---|---|',
               '| 在架 | **%d** 套 |' % kept,
               '| 新发现下架 | **%d** 套 |' % len(gone),
               '| 租金变化 | **%d** 套 |' % len(changed),
               '| 网络失败 | **%d** 套 |' % unknown, '']

    if not meaningful:
        summary.append('本次巡检**没有实质变化**，未写入任何文件，也未产生提交。')
        write_summary(summary)
        print('本次巡检没有实质变化，不写入文件。')
        if args.dry:
            print('--dry：不写入文件')
        return

    entry = ['## %s UTC　在架 %d ｜ 下架 %d ｜ 改价 %d ｜ 网络失败 %d'
             % (stamp, kept, len(gone), len(changed), unknown), '']
    if gone:
        entry += ['**新发现下架**', '']
        for s, r in gone:
            entry.append('- `%s` %s（%s）— %s' % (s['id'], s['name'], s['addr'], r['why']))
        entry.append('')
    if changed:
        entry += ['**租金变化**', '']
        for s, old, new in sorted(changed, key=lambda c: -abs(c[2] - c[1]) / c[1]):
            arrow = '↓' if new < old else '↑'
            pct = (new - old) / old * 100
            flag = ''
            if abs(pct) > 30:
                flag = '　⚠️ **幅度异常，建议核对链接是否指向另一套房**（页面楼名：%s）' % s.get('siteName', '?')
            entry.append('- `%s` %s：HK$%s → HK$%s　%s%.1f%%%s'
                         % (s['id'], s['name'], format(old, ','), format(new, ','), arrow, abs(pct), flag))
        entry.append('')

    summary += ['**本次变化**', ''] + [l for l in entry[2:] if l]
    write_summary(summary)

    if args.dry:
        print('--dry：不写回文件（本次有实质变化：下架 %d、改价 %d）' % (len(gone), len(changed)))
        return

    json.dump(spots, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    prepend_report(entry)
    print('已写回 data/spots.json，并向 data/changes.md 追加一条记录')


if __name__ == '__main__':
    main()

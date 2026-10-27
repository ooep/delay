#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_delay_assets.py — 组装 jr-delay 纯静态 Worker 的 assets 目录。

从 data/ 把主站消费的 JSON 复制到 static-worker/assets/（内容直接放 assets 根，
域名 jr-delay.suki.ing → assets 根，线上 URL 即 https://jr-delay.suki.ing/<file>）并补 CORS 头：
  data/delays.json            → assets/delays.json        → https://jr-delay.suki.ing/delays.json
  data/sukijr.json            → assets/sukijr.json
  data/lines.json             → assets/lines.json
  data/keikyu-positions.json  → assets/keikyu-positions.json
  data/suki/                  → assets/suki/（每周快照，烘焙输入缓存）

部署由 bake-delays.yml 的 wrangler deploy 完成（仅资产模式，零计费）。
线上 URL: https://jr-delay.suki.ing/delays.json
"""
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, 'data')
OUT = os.path.join(REPO, 'static-worker', 'assets')

HEADERS = """/*
  Access-Control-Allow-Origin: *
  Access-Control-Allow-Methods: GET, OPTIONS
  Access-Control-Allow-Headers: *
  Access-Control-Max-Age: 86400
  Access-Control-Expose-Headers: ETag, Last-Modified, Content-Length, Content-Type

/*.json
  Cache-Control: public, max-age=300
"""
ROBOTS = "User-agent: *\nDisallow: /\n"


def copy_tree(src, dst):
    n = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = dst if rel == '.' else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root, f), os.path.join(target, f))
            n += 1
    return n


def main():
    if not os.path.isdir(SRC):
        print('[assets] data/ 不存在:', SRC, file=sys.stderr)
        return 1
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    # data/ 内容直接放 assets 根（路由 /data/delay/* → assets 根）
    # 只复制线上前端真正消费的文件；data/suki/（40MB 每周快照缓存）
    # 和 data/delay-history/ 是烘焙输入，不进 Worker assets，省 ~10s
    # 打包+上传。
    PUBLISH = [
        'delays.json', 'sukijr.json', 'lines.json',
        'keikyu-positions.json', 'nishitetsu-positions.json',
    ]
    os.makedirs(OUT, exist_ok=True)
    n = 0
    for f in PUBLISH:
        src_f = os.path.join(SRC, f)
        if os.path.isfile(src_f):
            shutil.copy2(src_f, os.path.join(OUT, f))
            n += 1
    with open(os.path.join(OUT, '_headers'), 'w', encoding='utf-8') as f:
        f.write(HEADERS)
    with open(os.path.join(OUT, 'robots.txt'), 'w', encoding='utf-8') as f:
        f.write(ROBOTS)

    total = 0
    for root, _, files in os.walk(OUT):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    print(f'[assets] 组装完成: {n} 个数据文件 → {OUT}（{total / 1048576:.1f} MB）')
    return 0


if __name__ == '__main__':
    sys.exit(main())

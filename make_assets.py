#!/usr/bin/env python3
"""生成占位 PNG 资源。想换图片，直接替换 assets/ 下的同名文件。"""

import math
import random
from pathlib import Path
from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent / "assets"
ASSETS.mkdir(exist_ok=True)


def make_grass(path, w=800, h=600):
    img = Image.new("RGB", (w, h), (60, 140, 60))
    draw = ImageDraw.Draw(img)
    random.seed(42)
    for _ in range(4000):
        x, y = random.randint(0, w-1), random.randint(0, h-1)
        r = random.randint(1, 4)
        g = random.randint(100, 180)
        color = (random.randint(20, 70), g, random.randint(20, 70))
        draw.ellipse([x-r, y-r, x+r, y+r], fill=color)
    # 一些浅色草丛
    for _ in range(200):
        x, y = random.randint(0, w-1), random.randint(0, h-1)
        draw.line([x, y, x+2, y-8], fill=(120, 200, 100), width=1)
    img.save(path)
    print(f"  {path.name}  ({w}x{h})")


def make_fly(path, size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = size//2, size//2
    # 翅膀
    d.ellipse([cx-24, cy-8, cx-2, cy+14], fill=(220, 220, 240, 150))
    d.ellipse([cx+2,  cy-8, cx+24, cy+14], fill=(220, 220, 240, 150))
    # 身体（顶视图，头朝上）
    d.ellipse([cx-8, cy-14, cx+8, cy+18], fill=(50, 35, 25))
    # 胸
    d.ellipse([cx-7, cy-8, cx+7, cy+6], fill=(80, 55, 40))
    # 头
    d.ellipse([cx-6, cy-22, cx+6, cy-12], fill=(35, 25, 20))
    # 眼睛
    d.ellipse([cx-5, cy-20, cx-1, cy-16], fill=(200, 30, 30))
    d.ellipse([cx+1, cy-20, cx+5, cy-16], fill=(200, 30, 30))
    img.save(path)
    print(f"  {path.name}  ({size}x{size})")


def make_light(path, size=96):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = size//2, size//2
    # 光晕（从外到内）
    for r in range(44, 0, -2):
        alpha = int(180 * (1 - r/44))
        d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(255, 250, 150, alpha))
    # 射线
    for ang in range(0, 360, 30):
        rad = math.radians(ang)
        x1 = cx + 16 * math.cos(rad)
        y1 = cy + 16 * math.sin(rad)
        x2 = cx + 40 * math.cos(rad)
        y2 = cy + 40 * math.sin(rad)
        d.line([x1, y1, x2, y2], fill=(255, 240, 100, 220), width=3)
    # 核心
    d.ellipse([cx-12, cy-12, cx+12, cy+12], fill=(255, 245, 120))
    d.ellipse([cx-6, cy-6, cx+6, cy+6], fill=(255, 255, 220))
    img.save(path)
    print(f"  {path.name}  ({size}x{size})")


if __name__ == "__main__":
    print(f"生成资源到 {ASSETS}/")
    make_grass(ASSETS / "grass.png")
    make_fly(ASSETS / "fly.png")
    make_light(ASSETS / "light.png")
    print("完成。想换图片直接替换同名 PNG 即可。")
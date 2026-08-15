"""生成双端应用图标：与登录页 logo 同一个标记（圆角方形 + 樱粉->紫 135° 渐变 + 白色实心菱形）。

产物（本脚本是双端图标的唯一来源，改设计只动这里再重跑）：
- desktop/app.ico            Windows exe / 窗口图标（多尺寸 ico）
- frontend/assets/app-icon.png  网页 favicon（前端 index.html 引用）
- android mipmap-*/ic_launcher.png + ic_launcher_round.png            各密度传统图标
- android mipmap-*/ic_launcher_{foreground,background}.png            自适应图标（v26+）
  （mipmap-anydpi-v26/*.xml 已存在并正确引用两层，本脚本不改）

用法：python tools/gen_icons.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

# 与 frontend/style.css 的 --accent / --accent-2 默认值保持一致
ACCENT = (255, 127, 172)   # #ff7fac 樱粉
ACCENT2 = (160, 107, 255)  # #a06bff 紫

# 圆角比例取自 CSS 登录页 .logo：58px 方框 / border-radius 16px
RADIUS_RATIO = 16 / 58
# 菱形对角线占方片边长 1/3（用户定的观感最佳值），故半对角比例 = 1/6。
# ⚠️必须与 style.css 的 `.logo::before / .about-logo::before` 宽度 33.333% 保持一致，
# 那是同一个标记的 CSS 画法（界面里要跟随主题色，所以没直接贴这张 PNG）。
# ⚠️菱形是**实心**的：早先版本在中心又叠了一个渐变色小菱形当"高光"，缩到启动器
# 尺寸后中间那块渐变把菱形掏空成了空心——不要再加回来。
DIAMOND_RATIO = 1 / 6
# 自适应图标：108dp 画布只有中间 72dp 可见（安全区 66dp）。前景菱形按可见区
# 换算，使实际观感与传统图标/桌面图标一致。
ADAPTIVE_VISIBLE = 72 / 108

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(size, c1, c2):
    """135° 线性渐变（左上->右下）。线性渐变放大无损，故小图算完再拉伸。"""
    n = min(size, 128)
    img = Image.new("RGB", (n, n))
    px = img.load()
    for y in range(n):
        for x in range(n):
            px[x, y] = lerp(c1, c2, (x + y) / (2 * (n - 1)))
    return img if n == size else img.resize((size, size), Image.BICUBIC)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def diamond(draw, cx, cy, r, fill):
    draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=fill)


def make_logo(size, diamond_ratio=DIAMOND_RATIO, bg=True):
    """返回 RGBA 图标。bg=True 圆角渐变底；False 透明底（adaptive 前景用）。"""
    ss = size * 4  # 超采样抗锯齿
    if bg:
        base = gradient(ss, ACCENT, ACCENT2).convert("RGBA")
        base.putalpha(rounded_mask(ss, int(ss * RADIUS_RATIO)))
    else:
        base = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    diamond(d, ss / 2, ss / 2, ss * diamond_ratio, (255, 255, 255, 255))
    return base.resize((size, size), Image.LANCZOS)


def save(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    print("wrote", os.path.relpath(path, ROOT))


def main():
    # --- Windows .ico（多尺寸）---
    ico_path = os.path.join(ROOT, "desktop", "app.ico")
    os.makedirs(os.path.dirname(ico_path), exist_ok=True)
    make_logo(256).save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                         (64, 64), (128, 128), (256, 256)])
    print("wrote", os.path.relpath(ico_path, ROOT))

    master = make_logo(512)
    # --- 网页 favicon（登录页/WebView 标签用同一个标记）---
    save(master, os.path.join(ROOT, "frontend", "assets", "app-icon.png"))

    # --- Android 传统 mipmap（各密度，API<26）---
    res = os.path.join(ROOT, "android", "app", "src", "main", "res")
    for d, px in {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}.items():
        logo = make_logo(px)
        save(logo, os.path.join(res, f"mipmap-{d}", "ic_launcher.png"))
        save(logo, os.path.join(res, f"mipmap-{d}", "ic_launcher_round.png"))

    # --- Android 自适应图标（v26+）：透明底前景菱形 + 满铺渐变背景 ---
    # 背景必须满铺整张 108dp（启动器按机型裁成圆/圆角方/水滴），故不加圆角。
    fg_ratio = DIAMOND_RATIO * ADAPTIVE_VISIBLE
    for d, px in {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}.items():
        save(make_logo(px, diamond_ratio=fg_ratio, bg=False),
             os.path.join(res, f"mipmap-{d}", "ic_launcher_foreground.png"))
        save(gradient(px, ACCENT, ACCENT2).convert("RGBA"),
             os.path.join(res, f"mipmap-{d}", "ic_launcher_background.png"))


if __name__ == "__main__":
    main()

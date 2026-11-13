#!/usr/bin/env python3
"""Draw the site icon and write every size the page links to (web/icons/).

A checkered road running to the horizon under a starfield, with a ship on
it. Writes icon.svg (rounded and zoomed in on the ship, for browser tabs) and
art.svg (the full scene, full bleed),
then, with rsvg-convert and ImageMagick (brew install librsvg imagemagick):
apple-touch-icon.png (180), icon-192.png, icon-512.png, icon-maskable-512.png
(art shrunk into the maskable safe zone) and favicon.ico (16, 32, 48).
The outputs are committed, so building the game does not need these tools.

usage: python3 gen_icons.py      (from anywhere)
"""
import os
import random
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
S = 512
HORIZON = 196                  # y of the vanishing point
CX = S / 2
MAGENTA, BLUE = "#a32590", "#3f86d6"


def road():
    """Perspective checkerboard: 4 columns, rows shrinking toward the horizon."""
    parts = []
    half_near, y_near = 300.0, S + 40.0          # road half-width at y_near
    cols = 4
    # depth z: y = HORIZON + K / z ; rows at evenly spaced depths
    K = (y_near - HORIZON) * 1.0
    zs = [1.0 + i * 0.55 for i in range(26)]
    for r in range(len(zs) - 1):
        z0, z1 = zs[r], zs[r + 1]
        y0, y1 = HORIZON + K / z0, HORIZON + K / z1
        w0, w1 = half_near / z0, half_near / z1
        for c in range(cols):
            a, b = -1 + 2 * c / cols, -1 + 2 * (c + 1) / cols
            pts = [(CX + a * w0, y0), (CX + b * w0, y0), (CX + b * w1, y1), (CX + a * w1, y1)]
            fill = MAGENTA if (r + c) % 2 else BLUE
            parts.append('<polygon fill="%s" points="%s"/>' % (
                fill, " ".join("%.1f,%.1f" % p for p in pts)))
    return "\n    ".join(parts)


def stars():
    rnd = random.Random(1993)
    out = []
    for _ in range(38):
        x, y = rnd.uniform(14, S - 14), rnd.uniform(14, HORIZON - 18)
        rad = rnd.choice([1.6, 2.2, 2.2, 3.0, 3.8])
        out.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="#fff" opacity="%.2f"/>' % (
            x, y, rad, rnd.uniform(.45, .95)))
    return "\n    ".join(out)


def art(rounded, view=(0, 0, S)):
    """view = (x, y, size): the square of the scene to show (tab icons zoom in)."""
    vx, vy, vs = view
    clip = '<clipPath id="c"><rect x="%d" y="%d" width="%d" height="%d" rx="%d"/></clipPath>' % (
        vx, vy, vs, vs, vs * 3 // 16 if rounded else 0)
    ship = ('<g transform="translate(256 392)">'
            '<path d="M0 -58 L74 30 L40 44 L0 30 L-40 44 L-74 30 Z" fill="#d9e2ee" stroke="#0b1424" stroke-width="7" stroke-linejoin="round"/>'
            '<path d="M0 -30 L20 14 L0 8 L-20 14 Z" fill="#2a4a7a"/>'
            '<path d="M-30 46 L-14 40 L-14 62 Z M30 46 L14 40 L14 62 Z" fill="#ffb347"/>'
            '</g>')
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx} {vy} {vs} {vs}">
  <defs>
    {clip}
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#05081a"/>
      <stop offset=".38" stop-color="#1b1350"/>
      <stop offset=".39" stop-color="#05081a"/>
    </linearGradient>
    <radialGradient id="glow" cx=".5" cy=".5" r=".5">
      <stop offset="0" stop-color="#7d5cff" stop-opacity=".55"/>
      <stop offset="1" stop-color="#7d5cff" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <g clip-path="url(#c)">
    <rect width="{S}" height="{S}" fill="url(#sky)"/>
    <ellipse cx="{CX}" cy="{H}" rx="330" ry="110" fill="url(#glow)"/>
    {stars}
    {road}
    {ship}
  </g>
</svg>
""".format(S=S, vx=vx, vy=vy, vs=vs, CX=CX, H=HORIZON, hy=HORIZON / S, clip=clip, stars=stars(), road=road(), ship=ship)


def write(name, text):
    with open(os.path.join(HERE, name), "w") as f:
        f.write(text)


def png(svg, out, size):
    subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "-o", os.path.join(HERE, out),
                    os.path.join(HERE, svg)], check=True)


if __name__ == "__main__":
    # tab icons (16-48 px): zoomed in on the road and ship so they read when tiny
    write("icon.svg", art(True, view=(72, 150, 368)))
    write("art.svg", art(False))
    png("art.svg", "apple-touch-icon.png", 180)
    png("art.svg", "icon-192.png", 192)
    png("art.svg", "icon-512.png", 512)
    # maskable: the art at 80% on its own background colour, so any mask shape keeps the ship
    png("art.svg", "_inner.png", 410)
    subprocess.run(["magick", "-size", "512x512", "xc:#05081a", os.path.join(HERE, "_inner.png"),
                    "-gravity", "center", "-composite", os.path.join(HERE, "icon-maskable-512.png")], check=True)
    os.remove(os.path.join(HERE, "_inner.png"))
    for n in (16, 32, 48):
        png("icon.svg", "_%d.png" % n, n)
    subprocess.run(["magick"] + [os.path.join(HERE, "_%d.png" % n) for n in (16, 32, 48)]
                   + [os.path.join(HERE, "favicon.ico")], check=True)
    for n in (16, 32, 48):
        os.remove(os.path.join(HERE, "_%d.png" % n))
    os.remove(os.path.join(HERE, "art.svg"))

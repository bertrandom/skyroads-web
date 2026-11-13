#!/usr/bin/env python3
"""Make the site icons (web/icons/) from a frame of the game's own intro.

intro-frame.png is the 320x200 screen 3.2 s into the intro, where the ship
flies in low over the checkered road, saved losslessly by the native build:

    SKYROADS_DATA=resources/Skyroads SKY_SHOT=/tmp/shots SKY_SHOT_AT=3200 port/skyroads
    magick /tmp/shots/shot000.bmp -strip PNG8:web/icons/intro-frame.png

Writes, with ImageMagick (brew install imagemagick):
  favicon.ico            16, 32, 48 (tight on the ship, so it reads in a tab)
  apple-touch-icon.png   180 (ship, road and sky)
  icon-192.png, icon-512.png
  icon-maskable-512.png  wider, so a circular mask keeps the wings
The pixels are enlarged 4x with nearest-neighbour first and then scaled
smoothly to size, so they stay sharp. The outputs are committed, so building
the game does not need ImageMagick.

usage: python3 gen_icons.py      (from anywhere)
"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "intro-frame.png")

# square crops of the 320x200 frame: (x, y, size); the ship is centred near (161, 110)
SHIP = (97, 46, 128)        # the ship nearly fills it
SCENE = (86, 33, 150)       # ship, road and stars
WIDE = (61, 0, 200)         # whole height, ship inside the maskable safe zone


def icon(crop, size, out):
    x, y, s = crop
    subprocess.run(["magick", SRC, "-crop", "%dx%d+%d+%d" % (s, s, x, y), "+repage",
                    "-filter", "point", "-resize", "400%",
                    "-filter", "lanczos", "-resize", "%dx%d" % (size, size),
                    "-strip", os.path.join(HERE, out)], check=True)


if __name__ == "__main__":
    icon(SCENE, 180, "apple-touch-icon.png")
    icon(SCENE, 192, "icon-192.png")
    icon(SCENE, 512, "icon-512.png")
    icon(WIDE, 512, "icon-maskable-512.png")
    sizes = (16, 32, 48)
    for n in sizes:
        icon(SHIP, n, "_%d.png" % n)
    subprocess.run(["magick"] + [os.path.join(HERE, "_%d.png" % n) for n in sizes]
                   + [os.path.join(HERE, "favicon.ico")], check=True)
    for n in sizes:
        os.remove(os.path.join(HERE, "_%d.png" % n))

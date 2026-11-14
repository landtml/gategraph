"""GateGraph artwork: logo, social preview and flowchart, rendered flat in Blender.

Usage: blender -b --factory-startup -P docs/assets/render.py -- docs/assets

One theme for everything: GitHub's dark canvas and text colours, green for a pass, red for a
refusal, amber for a stale gate. Workbench engine, flat light, Standard view transform, so the
hex values below are the colours in the PNG. One Blender unit is one output pixel.
Fonts: Inter (text) and the Octicons in JetBrainsMono Nerd Font (icons).
"""
import math
import sys
from pathlib import Path

import bmesh
import bpy

OUT = Path(sys.argv[sys.argv.index("--") + 1])
INTER = "/usr/share/fonts/opentype/inter/Inter-{}.otf"
ICONS = str(Path.home() / ".local/share/fonts/JetBrainsMonoNerd/JetBrainsMonoNerdFont-Regular.ttf")

INK = "#0d1117"       # canvas
CARD = "#161b22"      # raised surface
RIM = "#30363d"       # borders
TRACK = "#21262d"     # empty bar track, icon disc
TEXT = "#e6edf3"
MUTED = "#8b949e"
FAINT = "#6e7681"
PASS = "#3fb950"
FAIL = "#f85149"
STALE = "#d29922"

ICON = {  # Octicons code points in the Nerd Font
    "agent": 0xF4B8, "guard": 0xF510, "files": 0xF40D, "checks": 0xF45E, "seal": 0xF456,
    "done": 0xF4A1, "repair": 0xF425, "approval": 0xF415, "deny": 0xF479, "stale": 0xF464,
    "next": 0xF46A, "pass": 0xF42E, "fail": 0xF52F,
}

_fonts = {}


def font(path):
    if path not in _fonts:
        _fonts[path] = bpy.data.fonts.load(path)
    return _fonts[path]


def lin(hexstr):
    h = hexstr.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return (*out, 1.0)


# ---------------------------------------------------------------- primitives

def poly(pts, color, z=0.0):
    me = bpy.data.meshes.new("p")
    bm = bmesh.new()
    bm.faces.new([bm.verts.new((x, y, z)) for x, y in pts])
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("p", me)
    ob.color = lin(color)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def rrect(cx, cy, w, h, r, color, z=0.0, seg=16):
    r = max(0.01, min(r, w / 2, h / 2))
    pts = []
    for ox, oy, a0 in ((cx + w / 2 - r, cy + h / 2 - r, 0), (cx - w / 2 + r, cy + h / 2 - r, 90),
                       (cx - w / 2 + r, cy - h / 2 + r, 180), (cx + w / 2 - r, cy - h / 2 + r, 270)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90 * i / seg)
            pts.append((ox + r * math.cos(a), oy + r * math.sin(a)))
    return poly(pts, color, z)


def frame(cx, cy, w, h, r, fill, rim, z=0.0, t=2.0):
    """A filled rounded rectangle with a t-wide border."""
    rrect(cx, cy, w, h, r, rim, z)
    rrect(cx, cy, w - 2 * t, h - 2 * t, r - t, fill, z + 0.01)


def circle(cx, cy, r, color, z=0.0, seg=96):
    return poly([(cx + r * math.cos(2 * math.pi * i / seg), cy + r * math.sin(2 * math.pi * i / seg))
                 for i in range(seg)], color, z)


def stroke(p0, p1, t, color, z=0.0, seg=12):
    """A line of width t with round caps."""
    (x0, y0), (x1, y1) = p0, p1
    a = math.atan2(y1 - y0, x1 - x0)
    pts = []
    for (cx, cy), a0 in (((x1, y1), a - math.pi / 2), ((x0, y0), a + math.pi / 2)):
        for i in range(seg + 1):
            ang = a0 + math.pi * i / seg
            pts.append((cx + t / 2 * math.cos(ang), cy + t / 2 * math.sin(ang)))
    return poly(pts, color, z)


def dashed(p0, p1, t, color, z=0.0, dash=12, gap=10):
    (x0, y0), (x1, y1) = p0, p1
    length = math.hypot(x1 - x0, y1 - y0)
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    s = 0.0
    while s < length:
        e = min(s + dash, length)
        stroke((x0 + ux * s, y0 + uy * s), (x0 + ux * e, y0 + uy * e), t, color, z)
        s = e + gap


def path(points, t, color, z=0.0, dashed_line=False, head=True):
    """An orthogonal connector through points, with an arrowhead at the last point."""
    pts = list(points)
    if head:  # stop the line short so the head's tip lands on the target
        (xa, ya), (xb, yb) = pts[-2], pts[-1]
        d = math.hypot(xb - xa, yb - ya)
        pts[-1] = (xb - (xb - xa) / d * 10, yb - (yb - ya) / d * 10)
    for a, b in zip(pts, pts[1:]):
        (dashed if dashed_line else stroke)(a, b, t, color, z)
    if head:
        (xa, ya), (xb, yb) = points[-2], points[-1]
        ang = math.atan2(yb - ya, xb - xa)
        L, W = 18, 11
        bx, by = xb - L * math.cos(ang), yb - L * math.sin(ang)
        poly([(xb, yb), (bx + W * math.sin(ang), by - W * math.cos(ang)),
              (bx - W * math.sin(ang), by + W * math.cos(ang))], color, z + 0.02)


def text(body, x, y, size, color, weight="Medium", z=1.0, align="CENTER", path_=None):
    cu = bpy.data.curves.new("t", type="FONT")
    cu.body = body
    cu.font = font(path_ or INTER.format(weight))
    cu.size = size
    cu.align_x = align
    cu.align_y = "CENTER"
    cu.space_line = 1.15
    ob = bpy.data.objects.new("t", cu)
    ob.location = (x, y, z)
    ob.color = lin(color)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def width_of(ob):
    bpy.context.view_layer.update()
    xs = [v[0] for v in ob.bound_box]
    return (max(xs) - min(xs)) * ob.scale.x


def icon(name, x, y, size, color, z=1.2):
    """An Octicon centred on (x, y) by its own bounding box, not the font's metrics."""
    ob = text(chr(ICON[name]), 0, 0, size, color, z=z, path_=ICONS)
    bpy.context.view_layer.update()
    xs = [v[0] for v in ob.bound_box]
    ys = [v[1] for v in ob.bound_box]
    ob.location = (x - (min(xs) + max(xs)) / 2, y - (min(ys) + max(ys)) / 2, z)
    return ob


def pill(name, label, x, y, color, z=2.0):
    """A small label on a connector: icon + word, on the canvas colour, outlined in the line colour."""
    t = text(label, 0, y, 19, color, weight="SemiBold", z=z + 0.1, align="LEFT")
    tw = width_of(t)
    w = tw + 22 + 8 + 28
    frame(x, y, w, 36, 18, INK, color, z)
    left = x - w / 2 + 14
    icon(name, left + 11, y, 19, color, z=z + 0.1)
    t.location.x = left + 22 + 8


# ---------------------------------------------------------------- scene

def scene(w, h):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    _fonts.clear()
    sc = bpy.context.scene
    cam = bpy.data.cameras.new("cam")
    cam.type = "ORTHO"
    cam.ortho_scale = max(w, h)
    cam.clip_start, cam.clip_end = 0.1, 100
    ob = bpy.data.objects.new("cam", cam)
    ob.location = (0, 0, 50)
    sc.collection.objects.link(ob)
    sc.camera = ob
    r = sc.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100
    r.engine = "BLENDER_WORKBENCH"
    r.film_transparent = True
    r.dither_intensity = 0.0
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    sc.display.shading.light = "FLAT"
    sc.display.shading.color_type = "OBJECT"
    sc.display.render_aa = "32"
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"


def render(name):
    bpy.context.scene.render.filepath = str(OUT / name)
    bpy.ops.render.render(write_still=True)


# ---------------------------------------------------------------- logo

def mark(cx, cy, s, z=1.0, bg=INK):
    """Two inputs fan into one node; its edge passes through a gate to the sealed (green) node."""
    def P(x, y):
        return cx + x * s, cy + y * s

    t = 44 * s
    A, B, C, D = (-380, 230), (-380, -230), (-190, 0), (330, 0)
    gx, half_w, top, bot = 55, 95, 175, -175
    stroke(P(*A), P(*C), t, TEXT, z)
    stroke(P(*B), P(*C), t, TEXT, z)
    stroke(P(*C), P(gx, 0), t, TEXT, z)
    stroke(P(gx, 0), P(*D), t, PASS, z - 0.05)
    for side in (-1, 1):
        stroke(P(gx + side * half_w, bot), P(gx + side * half_w, top), t, TEXT, z + 0.1)
    stroke(P(gx - half_w, top), P(gx + half_w, top), t, TEXT, z + 0.2)
    for x, y in (A, B):
        circle(*P(x, y), 52 * s, TEXT, z + 0.3)
    circle(*P(*C), 86 * s, TEXT, z + 0.3)
    circle(*P(*C), 42 * s, bg, z + 0.4)
    circle(*P(*D), 100 * s, PASS, z + 0.3)


def logo():
    scene(1024, 1024)
    rrect(0, 0, 1024, 1024, 225, INK, 0)
    mark(0, 0, 1.024)
    render("logo.png")

    scene(1024, 1024)
    mark(0, 0, 1.024)
    render("mark.png")

    scene(1280, 640)
    rrect(0, 0, 1280, 640, 0.01, INK, 0)
    mark(-230, 0, 0.42)
    text("GateGraph", -25, 38, 118, TEXT, weight="Bold", align="LEFT")
    text("Evidence gates for AI coding agents", -21, -62, 36, MUTED, align="LEFT")
    render("social-preview.png")


# ---------------------------------------------------------------- flowchart

def node(key, title, sub, x, y, w=210, h=176, accent=TEXT, rim=RIM):
    frame(x, y, w, h, 18, CARD, rim, 0.5, t=2)
    circle(x, y + 40, 31, TRACK, 0.6)
    icon(key, x, y + 40, 30, accent, z=0.7)
    text(title, x, y - 12, 24, TEXT, weight="SemiBold", z=0.7)
    text(sub, x, y - 52, 17, MUTED, z=0.7)


def flowchart():
    W, H = 1700, 700
    scene(W, H)
    rrect(0, 0, W, H, 28, INK, 0)
    oy = 70                                    # main row height
    xs = [-675 + 270 * i for i in range(6)]
    nh, nw = 176, 210
    top, bot = oy + nh / 2, oy - nh / 2
    lane = top + 70                            # loops above the row
    low = -230                                 # helper row below

    steps = [
        ("agent", "Agent", "writes code"),
        ("guard", "Guard", "policy on every\ntool call"),
        ("files", "Unit files", "the work under\ncheck"),
        ("checks", "Checks", "exit 0 pass,\n1 fail, 2 warn"),
        ("seal", "Seal", "hash-linked\nevidence record"),
        ("done", "Done", "gate verify\nproves the chain"),
    ]
    for (key, title, sub), x in zip(steps, xs):
        hot = key == "done"
        node(key, title, sub, x, oy, accent=PASS if hot else TEXT, rim=PASS if hot else RIM)

    # main path
    for i, (a, b) in enumerate(zip(xs, xs[1:])):
        colour = PASS if i >= 3 else MUTED
        path([(a + nw / 2 + 6, oy), (b - nw / 2 - 4, oy)], 4, colour, 0.3)
    text("allow", (xs[1] + xs[2]) / 2, oy + 20, 16, MUTED, z=0.4)
    text("pass", (xs[3] + xs[4]) / 2, oy + 20, 16, PASS, weight="SemiBold", z=0.4)
    text("last gate", (xs[4] + xs[5]) / 2, oy + 20, 16, PASS, weight="SemiBold", z=0.4)

    # loops above the row: deny, stale, next gate
    def loop(xa, xb, colour, key, label, dashed_line=False):
        path([(xa, top + 4), (xa, lane), (xb, lane), (xb, top + 4)], 4, colour, 0.3,
             dashed_line=dashed_line)
        pill(key, label, (xa + xb) / 2, lane, colour)

    loop(xs[1] - 30, xs[0] + 30, FAIL, "deny", "deny")
    loop(xs[2] - 40, xs[3] - 30, STALE, "stale", "stale after edit", dashed_line=True)
    loop(xs[4] + 30, xs[3] + 30, MUTED, "next", "next gate")

    # below the row: fail -> repair -> agent, and the optional approval into the seal
    node("repair", "Repair", "findings go back\nto the agent", xs[3], low, accent=FAIL, rim=RIM)
    path([(xs[3], bot - 4), (xs[3], low + nh / 2 + 4)], 4, FAIL, 0.3)
    pill("fail", "fail", xs[3], (bot + low + nh / 2) / 2, FAIL)
    path([(xs[3] - nw / 2 - 4, low), (xs[0], low), (xs[0], bot - 4)], 4, FAIL, 0.3)

    node("approval", "Approval", "optional: terminal\nor one-time token", xs[4], low, accent=MUTED)
    path([(xs[4], low + nh / 2 + 4), (xs[4], bot - 4)], 4, MUTED, 0.3, dashed_line=True)

    render("flowchart.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    logo()
    flowchart()

#!/usr/bin/env python3
"""从一张拓扑图（试卷截图 / 自己画的图）里量出「文字 + 位置 + 图标位置」，
输出可以直接拿去生成 .topo 的坐标表。

它把两件容易反复试错的事一次做完：
  1) 文字：调 Windows 自带 OCR（scripts/ocr_win.ps1，零依赖零联网）拿 文字+像素框；
     视觉模型不可用时也能干活。
  2) 图标：给一个已知图标的 bbox，用模板匹配把同型号设备全部找出来（需要 numpy+Pillow）。

最后按「图标中心」输出坐标，并提示要加 --calibrate 才能直接填进 .topo 的 cx/cy
（eNSP 的 cx/cy 不是图标中心：实测 PC 偏 (+27,+42)，S3700 偏 (+3,+41)）。

用法：
    python figure_probe.py 图.png                        # 只出 OCR 文字表 + 密度图
    python figure_probe.py 图.png --ocr-scale 2          # 小字放大 2 倍再 OCR
    python figure_probe.py 图.png --template 496,576,568,628   # 额外模板匹配该图标
    python figure_probe.py 图.png --json                 # 机读输出
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
OCR_PS1 = os.path.join(HERE, "ocr_win.ps1")
LINE_RE = re.compile(r"^(\d+),(\d+)-(\d+),(\d+)\s+(\d+)x(\d+)\s+(.*)$")


def run_ocr(path, scale=1):
    ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", OCR_PS1,
          "-Path", os.path.abspath(path)]
    if scale > 1:
        ps += ["-Scale", str(scale)]
    r = subprocess.run(ps, capture_output=True, timeout=180,
                       encoding="utf-8", errors="replace")
    items = []
    for ln in (r.stdout or "").splitlines():
        m = LINE_RE.match(ln.strip())
        if m:
            items.append({
                "text": m.group(7).strip(),
                "x": int(m.group(1)), "y": int(m.group(2)),
                "w": int(m.group(5)), "h": int(m.group(6)),
            })
    if not items and r.returncode != 0:
        print("[warn] OCR 失败: %s" % (r.stderr or "")[:200], file=sys.stderr)
    return items


def ink_grid(path, cols=40):
    """粗粒度墨迹密度图：一眼看出设备/连线大概在哪，不需要视觉模型。"""
    try:
        from PIL import Image
    except ImportError:
        print("[skip] 密度图需要 Pillow（pip install pillow）", file=sys.stderr)
        return None
    im = Image.open(path).convert("L")
    W, H = im.size
    rows = max(1, int(cols * H / W / 2.1))
    px = im.load()
    cw, ch = W / cols, H / rows
    grid = []
    for r in range(rows):
        line = []
        for c in range(cols):
            n = tot = 0
            for y in range(int(r * ch), min(int((r + 1) * ch), H), 2):
                for x in range(int(c * cw), min(int((c + 1) * cw), W), 2):
                    tot += 1
                    if px[x, y] < 200:
                        n += 1
            line.append(" .:-=+*#@"[min(8, int(9 * n / max(tot, 1)))])
        grid.append("".join(line))
    return {"size": [W, H], "cols": cols, "rows": rows, "grid": grid}


def template_match(path, box, scale=2):
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        print("[skip] 模板匹配需要 numpy+Pillow", file=sys.stderr)
        return []
    x0, y0, x1, y1 = [int(v) for v in box.split(",")]
    im = Image.open(path).convert("RGB")
    small = im.resize((im.width // scale, im.height // scale), Image.LANCZOS)
    a = np.asarray(small, dtype=np.float32)
    H, W, _ = a.shape
    t = a[y0 // scale:y1 // scale, x0 // scale:x1 // scale]
    th, tw, _ = t.shape
    tv = t.reshape(-1); tv = tv - tv.mean()
    tden = np.sqrt((tv * tv).sum()) + 1e-9
    hits = []
    for y in range(0, H - th, 2):
        band = a[y:y + th]
        for x in range(0, W - tw, 2):
            w = band[:, x:x + tw].reshape(-1)
            wv = w - w.mean()
            s = float(wv @ tv) / ((np.sqrt((wv * wv).sum()) + 1e-9) * tden)
            if s > 0.62:
                hits.append((s, x * scale + tw * scale // 2, y * scale + th * scale // 2))
    hits.sort(reverse=True)
    kept = []
    for s, cx, cy in hits:
        if all(abs(cx - k[1]) > 40 or abs(cy - k[2]) > 40 for k in kept):
            kept.append((s, cx, cy))
    return [{"score": round(s, 3), "cx": int(cx), "cy": int(cy)} for s, cx, cy in kept[:12]]


def main(argv=None):
    ap = argparse.ArgumentParser(description="从拓扑图里量文字与图标位置")
    ap.add_argument("image")
    ap.add_argument("--ocr-scale", type=int, default=1)
    ap.add_argument("--template", default="", help="已知图标 bbox: x0,y0,x1,y1")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    out = {"image": os.path.abspath(args.image), "labels": [], "icons": [], "grid": None}
    if not args.no_ocr:
        out["labels"] = run_ocr(args.image, args.ocr_scale)
    if args.template:
        out["icons"] = template_match(args.image, args.template)
    out["grid"] = ink_grid(args.image)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print("=" * 70)
    print("文字（按 y 再按 x 排序；坐标是该行文字的包围盒）")
    print("=" * 70)
    for it in sorted(out["labels"], key=lambda d: (d["y"], d["x"])):
        print("  (%3d,%3d) %3dx%-2d  %s" % (it["x"], it["y"], it["w"], it["h"], it["text"]))
    if out["icons"]:
        print("\n模板匹配到的同型号图标（图标中心坐标）：")
        for it in out["icons"]:
            print("  score=%.3f  (%d, %d)" % (it["score"], it["cx"], it["cy"]))
        print("  → 这些是「图标中心」。生成 .topo 时加 --calibrate 让工具扣掉 eNSP 的锚点偏差")
    if out["grid"]:
        print("\n墨迹密度图（%dx%d 的格子，越靠右的符号越密，用来看设备/连线的大致位置）"
              % (out["grid"]["cols"], out["grid"]["rows"]))
        for i, row in enumerate(out["grid"]["grid"]):
            print("  %2d %s" % (i, row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
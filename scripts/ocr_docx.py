#!/usr/bin/env python3
"""OCR a report (.docx) to recover addressing from topology screenshots.

Optional helper: needs ``pip install rapidocr-onnxruntime`` (and Pillow, which
that package pulls in). Nothing else in this skill depends on it.

Two modes:

    python ocr_docx.py <report.docx> --list
        Print the document order of paragraphs and images. A question's
        topology picture normally follows its question text, so this is how
        you decide *which* image belongs to "第3题".

    python ocr_docx.py <report.docx> --image 3 [--save-dir out]
        OCR the 3rd image. Small figure text is enlarged 4x with a contrast
        boost and OCR'd at several scales; duplicate strings are merged and
        only tokens that look like addresses are reported.

The point is to recover the addresses drawn inside the topology picture when
the .topo does not carry them. Treat the result as a candidate list to confirm
with the user -- OCR misreads digits like 8/6 and 1/7.
"""

import argparse
import io
import os
import re
import sys
import zipfile

IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".emf", ".wmf")


def _rels(zf):
    """Map rId -> part name from word/_rels/document.xml.rels."""
    out = {}
    try:
        data = zf.read("word/_rels/document.xml.rels").decode("utf-8", "replace")
    except KeyError:
        return out
    for rid, target in re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', data):
        out[rid] = target
    return out


def list_structure(path):
    """Print paragraphs and images in document order."""
    try:
        import xml.etree.ElementTree as ET
        from xml.etree.ElementTree import XMLParser
    except ImportError:  # pragma: no cover
        pass

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}

    with zipfile.ZipFile(path) as zf:
        rels = _rels(zf)
        root = ET.fromstring(zf.read("word/document.xml"))

    image_no = 0
    for i, para in enumerate(root.iter("{%s}p" % ns["w"]), start=1):
        text = "".join(t.text or "" for t in para.iter("{%s}t" % ns["w"])).strip()
        changed = False
        for blip in para.iter("{%s}blip" % ns["a"]):
            rid = blip.get("{%s}embed" % ns["r"])
            target = rels.get(rid, "")
            image_no += 1
            changed = True
            if text and not changed:
                break
            print("[img %d] <- %s" % (image_no, target))
        if text:
            print("[p %d] %s" % (i, text[:160]))
    if image_no == 0:
        print("(no embedded images)")


def extract_images(path):
    """Return [(index, extension, bytes)] in document order."""
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    import xml.etree.ElementTree as ET

    out = []
    with zipfile.ZipFile(path) as zf:
        rels = _rels(zf)
        root = ET.fromstring(zf.read("word/document.xml"))
        seen = []
        for blip in root.iter("{%s}blip" % ns["a"]):
            rid = blip.get("{%s}embed" % ns["r"])
            target = rels.get(rid)
            if target:
                seen.append(target)
        # VML fallback images (older Word) use v:imagedata r:id.
        for img in root.iter(
                "{urn:schemas-microsoft-com:vml}imagedata"):
            rid = img.get("{%s}id" % ns["r"])
            target = rels.get(rid)
            if target and target not in seen:
                seen.append(target)
        for i, target in enumerate(seen, start=1):
            part = "word/" + target.lstrip("/")
            if not part.lower().endswith(IMAGE_EXTS):
                continue
            try:
                out.append((i, os.path.splitext(target)[1], zf.read(part)))
            except KeyError:
                continue
    return out


def ocr_image(data, ext, scale=4):
    """OCR one image and return candidate address strings."""
    try:
        from rapidocr_onnxruntime import RapidOCR
        from PIL import Image, ImageEnhance
    except ImportError:
        raise SystemExit(
            "ocr_docx.py needs: pip install rapidocr-onnxruntime\n"
            "(it pulls in Pillow; nothing else in this skill uses it)"
        )

    engine = RapidOCR()
    image = Image.open(io.BytesIO(data)).convert("RGB")
    texts = []
    for factor in (scale, scale * 2):
        big = image.resize(
            (image.width * factor, image.height * factor), Image.LANCZOS
        )
        big = ImageEnhance.Contrast(big).enhance(1.8)
        buf = io.BytesIO()
        big.save(buf, format="PNG")
        result, _ = engine(buf.getvalue())
        if result:
            texts.extend(part[1] for part in result)
    return texts


def main(argv=None):
    ap = argparse.ArgumentParser(description="OCR topology figures in a .docx report.")
    ap.add_argument("docx", help="path to the report")
    ap.add_argument("--list", action="store_true", help="list paragraphs and images in order")
    ap.add_argument("--image", type=int, help="1-based image index to OCR")
    ap.add_argument("--save-dir", help="write the extracted image here")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.docx):
        print("error: no such file: %s" % args.docx, file=sys.stderr)
        return 2

    if args.list or not args.image:
        list_structure(args.docx)
        if not args.image:
            print()
            print("next: python ocr_docx.py %s --image <n>" % args.docx)
        return 0

    images = extract_images(args.docx)
    if not (1 <= args.image <= len(images)):
        print("error: image index out of range (1..%d)" % len(images), file=sys.stderr)
        return 2
    index, ext, data = images[args.image - 1]

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)
        out = os.path.join(args.save_dir, "image-%d%s" % (index, ext))
        with open(out, "wb") as fh:
            fh.write(data)
        print("saved %s" % out)

    texts = ocr_image(data, ext)
    candidates = []
    for text in texts:
        candidates.extend(IP_RE.findall(text))
    unique = sorted(set(candidates))
    print("raw OCR lines: %d" % len(texts))
    print("address candidates:")
    for addr in unique:
        print("  %s" % addr)
    if not unique:
        print("  (none -- the figure may use host labels like PC1 rather than bare addresses)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

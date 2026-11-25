#!/usr/bin/env python3
"""lab.json：把「拓扑 + 配置 + 断言」写成一份数据，脚本不再被改来改去。

以前做一个新实验要改 make_topo.py 里的 DEVICES/LINKS/PC_SETTINGS 常量，还要再写一份
apply_plan 的 plan.json —— 同一张拓扑被描述两遍，很容易对不上。现在只写一份：

{
  "name": "vlan4",
  "base_port": 2000,
  "encoding": "gbk",
  "devices": [
    {"name": "LSW3", "model": "S3700", "x": 340, "y": 150},
    {"name": "PC1", "model": "PC", "x": 70, "y": 55,
     "ip": "192.168.10.10", "mask": "255.255.255.0", "gateway": "192.168.10.1"}
  ],
  "links": [
    {"a": "LSW3", "a_if": "Ethernet0/0/1", "b": "PC1", "b_if": "Ethernet0/0/1"}
  ],
  "labels": [{"text": "VLAN10", "x": 100, "y": 100}],
  "config": {
    "LSW3": {
      "lines": ["system-view", "sysname LSW3", "vlan batch 10"],
      "expect": [{"name": "VLAN10 存在", "cmd": "display vlan", "contains": "10"}]
    }
  },
  "save": true
}

分工：
  make_topo.py --spec lab.json   生成 .topo（设备/连线/PC 地址/标注/端口段）
  apply_plan.py --spec lab.json  下发 config 里的 lines，并按 expect 判定
  本文件                         validate / plan（把 spec 编译成 apply_plan 能吃的计划）

用法：
    python lab_spec.py validate lab.json
    python lab_spec.py plan lab.json -o plan.json
"""
import argparse
import io
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import parse_topo  # noqa: E402

SUPPORTED_MODELS = tuple(sorted(parse_topo.DEFAULT_SLOTS))
MEDIA = ("copper", "serial")


def load(path):
    """读 + 校验；返回 dict（校验失败抛 ValueError，信息里带字段路径）。"""
    with io.open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    validate(spec)
    return spec


def validate(spec):
    if not isinstance(spec, dict):
        raise ValueError("lab.json 顶层必须是对象")
    devices = spec.get("devices") or []
    if not devices:
        raise ValueError("devices 不能为空")

    names = []
    for i, dev in enumerate(devices):
        where = "devices[%d]" % i
        name = dev.get("name")
        if not name:
            raise ValueError("%s 缺少 name" % where)
        if name in names:
            raise ValueError("设备名重复: %s" % name)
        names.append(name)
        model = dev.get("model")
        if model not in SUPPORTED_MODELS:
            raise ValueError("%s (%s) 的 model=%r 不支持；可选: %s"
                             % (where, name, model, ", ".join(SUPPORTED_MODELS)))
        if model.upper() == "PC" and not dev.get("ip"):
            raise ValueError("%s (%s) 是 PC，必须给 ip" % (where, name))
        for key in ("x", "y"):
            if key not in dev:
                raise ValueError("%s (%s) 缺少 %s 坐标" % (where, name, key))

    for i, link in enumerate(spec.get("links") or []):
        where = "links[%d]" % i
        for side in ("a", "b"):
            if link.get(side) not in names:
                raise ValueError("%s 的 %s=%r 不在 devices 里" % (where, side, link.get(side)))
        for side in ("a_if", "b_if"):
            if not link.get(side):
                raise ValueError("%s 缺少 %s 接口名" % (where, side))
        media = str(link.get("media") or "Copper").lower()
        if media not in MEDIA:
            raise ValueError("%s 的 media 只能是 Copper/Serial" % where)

    for name, item in (spec.get("config") or {}).items():
        if name not in names:
            raise ValueError("config 里的 %r 不在 devices 里" % name)
        dev = next(d for d in devices if d["name"] == name)
        if dev["model"].upper() == "PC":
            raise ValueError("config.%s: PC 没有 CLI，不能下发配置" % name)
        if not isinstance(item.get("lines"), list):
            raise ValueError("config.%s.lines 必须是数组" % name)

    for i, label in enumerate(spec.get("labels") or []):
        if not label.get("text"):
            raise ValueError("labels[%d] 缺少 text" % i)


def topology(spec):
    """转成 make_topo.build_project() 要的四个表。"""
    devices = [(d["name"], d["model"], d.get("x", 0), d.get("y", 0))
               for d in spec["devices"]]
    links = [(l["a"], l["a_if"], l["b"], l["b_if"], l.get("media", "Copper"))
             for l in spec.get("links") or []]
    pc = {}
    for dev in spec["devices"]:
        if dev["model"].upper() == "PC":
            pc[dev["name"]] = {k: dev[k] for k in ("ip", "mask", "gateway") if k in dev}
    labels = [(lb["text"], lb.get("x", 0), lb.get("y", 0))
              for lb in spec.get("labels") or []]
    return devices, links, pc, labels


def plan(spec):
    """转成 apply_plan 的计划。console 端口与 make_topo 的分配规则一致：
    非 PC 设备按 devices 顺序从 base_port 起逐个 +1。"""
    base = int(spec.get("base_port", 2000))
    out = []
    port = base
    config = spec.get("config") or {}
    for dev in spec["devices"]:
        if dev["model"].upper() == "PC":
            continue
        item = {"name": dev["name"], "port": port,
                "lines": list((config.get(dev["name"]) or {}).get("lines") or [])}
        cfg = config.get(dev["name"]) or {}
        if cfg.get("verify"):
            item["verify"] = cfg["verify"]
        if cfg.get("expect"):
            item["expect"] = cfg["expect"]
        out.append(item)
        port += 1
    return {"devices": out, "save": bool(spec.get("save", True))}


def main(argv=None):
    ap = argparse.ArgumentParser(description="校验/编译 lab.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_val = sub.add_parser("validate", help="只校验")
    p_val.add_argument("spec")
    p_plan = sub.add_parser("plan", help="编译成 apply_plan 的计划 JSON")
    p_plan.add_argument("spec")
    p_plan.add_argument("-o", "--out", default="", help="输出路径（默认打印到 stdout）")
    args = ap.parse_args(argv)

    try:
        spec = load(args.spec)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.cmd == "validate":
        devices, links, pc, labels = topology(spec)
        print("ok: %d devices, %d links, %d PCs, %d labels, base_port=%s"
              % (len(devices), len(links), len(pc), len(labels),
                 spec.get("base_port", 2000)))
        return 0

    struct = plan(spec)
    text = json.dumps(struct, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        io.open(args.out, "w", encoding="utf-8").write(text)
        print("wrote %s (%d devices)" % (args.out, len(struct["devices"])))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
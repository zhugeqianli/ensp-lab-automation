#!/usr/bin/env python3
"""冒烟测试：每个脚本「能 import、能 --help」才算活着。

为什么单独有这一步：apply_plan.py 曾经因为少写 `import os`，在模块第 31 行就
NameError，整个工具是死的，而 test_skill.py 当时不 import 它，所以一直没发现。
这里用两个最便宜的动作把这类问题挡住：

  1. `python -c "import <模块>"` —— 抓模块级 SyntaxError / NameError / 坏 import
  2. `python <脚本> --help`      —— 抓 argparse 装配错误、import 期副作用崩溃

不依赖 eNSP、不联网、不写 __pycache__（PYTHONDONTWRITEBYTECODE=1）。

用法：
    python test_smoke.py          # 直接跑，退出码 0 = 全绿
"""

import glob
import os
import subprocess
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))

# 库模块没有 CLI；test_offline 没有 argparse（跑起来就是自测）；
# test_smoke 自己不测自己。
SKIP_CLI = {"vrp_console.py", "test_offline.py", "test_smoke.py"}


def scripts():
    return sorted(glob.glob(os.path.join(HERE, "*.py")))


def run_all(verbose=True):
    """返回失败项列表（空列表 = 全绿）。"""
    # 子进程统一走 UTF-8：Windows 控制台是 GBK，直接用 text=True 解码会在
    # 子进程输出中文时抛 UnicodeDecodeError，把「工具本身没问题」误判成失败。
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    failures = []
    for path in scripts():
        name = os.path.basename(path)
        if name == "test_smoke.py":
            continue
        module = name[:-3]

        r = subprocess.run([sys.executable, "-c", "import %s" % module],
                           cwd=HERE, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        ok_import = r.returncode == 0
        if not ok_import:
            failures.append(name)
            if verbose:
                print("  FAIL  %-24s import 失败: %s" % (name, _tail(r.stderr)))
            continue

        if name in SKIP_CLI:
            if verbose:
                print("  PASS  %-24s import ok（无 CLI）" % name)
            continue

        r = subprocess.run([sys.executable, path, "--help"],
                           cwd=HERE, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        combined = (r.stdout or "") + (r.stderr or "")
        ok_cli = r.returncode == 0 and "usage" in combined.lower()
        if not ok_cli:
            failures.append(name)
            if verbose:
                print("  FAIL  %-24s --help 失败: %s" % (name, _tail(combined)))
        elif verbose:
            print("  PASS  %-24s import + --help" % name)
    return failures


def _tail(text):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1][:160] if lines else "(no output)"


def main():
    print("冒烟测试：%d 个脚本" % (len(scripts()) - 1))
    failures = run_all()
    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("all smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
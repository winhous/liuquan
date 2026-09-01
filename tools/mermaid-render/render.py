#!/usr/bin/env python3
"""Mermaid 流程图渲染器：.mmd -> PNG + SVG（Playwright + 本地 mermaid.min.js）。

用法：
  uv run python tools/mermaid-render/render.py <输入.mmd> <输出前缀> [--width 1600] [--scale 2]

产出：<输出前缀>.png + <输出前缀>.svg（SVG 可浏览器打开无限放大）

依赖：本地 mermaid.min.js（tools/mermaid-render/node_modules/mermaid/dist/）
      + Playwright（PLAYWRIGHT_BROWSERS_PATH=/tmp/ms-playwright）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MERMAID_JS = REPO_ROOT / "tools" / "mermaid-render" / "node_modules" / "mermaid" / "dist" / "mermaid.min.js"

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ margin: 0; padding: 16px; background: white; }}
  #diagram {{ display: flex; justify-content: center; }}
  .mermaid {{ font-family: "Noto Sans CJK SC", sans-serif; }}
</style>
<script src="{mermaid_js}"></script>
</head>
<body>
<div id="diagram"><pre class="mermaid">{mmd_content}</pre></div>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    securityLevel: 'loose',
    theme: 'default',
    fontFamily: '"Noto Sans CJK SC", sans-serif',
    flowchart: {{ htmlLabels: true, curve: 'basis', padding: 16 }},
    themeVariables: {{
      fontFamily: '"Noto Sans CJK SC", sans-serif',
      fontSize: '15px',
      primaryColor: '#f0f5ff',
      primaryBorderColor: '#3b6fd4',
      primaryTextColor: '#1a2b4d',
      lineColor: '#5b6b8c',
      edgeLabelBackground: '#ffffff',
    }}
  }});
  window.__mermaidDone = false;
  mermaid.run({{ nodes: [document.querySelector('.mermaid')] }}).then(() => {{
    window.__mermaidDone = true;
  }});
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mmd", type=Path)
    ap.add_argument("out_prefix", type=Path)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--scale", type=float, default=2.0)
    args = ap.parse_args()

    if not MERMAID_JS.is_file():
        print(f"错误: 找不到 {MERMAID_JS}，请先 npm install mermaid@10", file=sys.stderr)
        return 1

    mmd_text = args.mmd.read_text(encoding="utf-8")
    # HTML 转义（防 </script> 截断）
    escaped = mmd_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = HTML_TEMPLATE.format(mermaid_js=MERMAID_JS.as_uri(), mmd_content=escaped)

    tmp_html = args.out_prefix.with_suffix(".tmp.html")
    tmp_html.write_text(html, encoding="utf-8")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": args.width, "height": 800})
        page.goto(tmp_html.as_uri())
        # 等 mermaid 渲染完成（最长 20s）
        deadline = time.time() + 20
        while time.time() < deadline:
            done = page.evaluate("window.__mermaidDone === true")
            if done:
                break
            time.sleep(0.2)
        else:
            print("警告: mermaid 渲染超时，可能内容有语法错误", file=sys.stderr)

        # 取实际渲染尺寸（SVG width=100%，需从 viewBox 解析真实尺寸）
        box = page.evaluate("""() => {
            const el = document.querySelector('#diagram .mermaid svg');
            if (!el) return null;
            const vb = el.getAttribute('viewBox');
            if (vb) {
                const parts = vb.split(/[\\s,]+/).map(Number);
                return { w: Math.ceil(parts[2]), h: Math.ceil(parts[3]) };
            }
            const r = el.getBoundingClientRect();
            return { w: Math.ceil(r.width), h: Math.ceil(r.height) };
        }""")
        if box is None:
            print("错误: mermaid 未渲染出 SVG（语法错误？）", file=sys.stderr)
            browser.close()
            return 2

        # 强制 SVG 按 viewBox 实际尺寸布局（width:100% 会被父容器压缩）
        page.evaluate(
            """(args) => {
                const el = document.querySelector('#diagram .mermaid svg');
                if (!el) return;
                el.setAttribute('width', args.w + 'px');
                el.setAttribute('height', args.h + 'px');
                el.style.width = args.w + 'px';
                el.style.height = args.h + 'px';
            }""",
            {"w": box["w"], "h": box["h"]},
        )
        page.set_viewport_size({"width": box["w"] + 80, "height": box["h"] + 80})
        page.wait_for_timeout(300)  # 等布局稳定
        # 直接截取 SVG 元素（比 full_page 可靠）
        svg_el = page.query_selector("#diagram .mermaid svg")
        if svg_el is None:
            print("错误: 找不到渲染后的 SVG 元素", file=sys.stderr)
            browser.close()
            return 2
        svg_el.screenshot(path=str(args.out_prefix) + ".png")

        # 导出 SVG（从页面取 innerHTML）
        svg = page.evaluate("""() => {
            const el = document.querySelector('#diagram .mermaid svg');
            return el ? el.outerHTML : null;
        }""")
        if svg:
            (args.out_prefix.parent / (args.out_prefix.name + ".svg")).write_text(
                svg, encoding="utf-8"
            )
        browser.close()

    tmp_html.unlink(missing_ok=True)
    print(f"完成: {args.out_prefix}.png + .svg（尺寸 {box['w']}x{box['h']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Render the research Markdown and Mermaid sources into an offline HTML report."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import subprocess
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
DIAGRAMS = ROOT / "diagrams"
CACHE = ROOT / ".cache" / "render"
MMDC = CACHE / "node_modules" / ".bin" / "mmdc"


def normalized(text: str) -> str:
    return text.strip().replace("\r\n", "\n")


def render_diagram(source: Path) -> Path:
    """Render using a pinned Mermaid CLI and the local Chrome installation."""
    target = source.with_suffix(".svg")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    stamp = CACHE / f"{source.stem}.sha256"
    if target.exists() and stamp.exists() and stamp.read_text() == digest:
        return target
    chrome = os.environ.get(
        "CHROME_EXECUTABLE",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    browser_config = CACHE / "puppeteer.json"
    browser_config.write_text(json.dumps({"executablePath": chrome}), encoding="utf-8")
    mermaid_config = CACHE / "mermaid.json"
    mermaid_config.write_text(
        json.dumps(
            {
                "theme": "base",
                "themeVariables": {
                    "fontFamily": "Arial, PingFang SC, Microsoft YaHei, sans-serif",
                    "fontSize": "16px",
                    "primaryColor": "#edf3ff",
                    "primaryTextColor": "#18273e",
                    "primaryBorderColor": "#5d80bf",
                    "lineColor": "#64748b",
                    "secondaryColor": "#e6f4ed",
                    "tertiaryColor": "#f6f8fc",
                },
                "flowchart": {"htmlLabels": True, "curve": "basis"},
                "sequence": {"wrap": True, "useMaxWidth": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(MMDC),
            "-i",
            str(source),
            "-o",
            str(target),
            "-p",
            str(browser_config),
            "-c",
            str(mermaid_config),
            "-b",
            "white",
            "-w",
            "1800",
            "-q",
        ],
        check=True,
    )
    stamp.write_text(digest, encoding="utf-8")
    return target


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    sources = {normalized(p.read_text()): p for p in sorted(DIAGRAMS.glob("*.mmd"))}
    pages = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    # Extract any additional diagrams embedded only in Markdown.
    for page in pages:
        for code in re.findall(r"```mermaid\n(.*?)```", page.read_text(), re.DOTALL):
            key = normalized(code)
            if key not in sources:
                name = "detail-" + hashlib.sha256(key.encode()).hexdigest()[:10]
                source = DIAGRAMS / f"{name}.mmd"
                source.write_text(key + "\n", encoding="utf-8")
                sources[key] = source
    rendered = {key: render_diagram(path) for key, path in sources.items()}
    md = MarkdownIt("commonmark", {"html": True}).enable("table")
    original_fence = md.renderer.rules["fence"]

    def fence(tokens, idx, options, env):
        token = tokens[idx]
        if token.info.strip() != "mermaid":
            return original_fence(tokens, idx, options, env)
        target = rendered[normalized(token.content)]
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
        href = Path(os.path.relpath(target, env["page"].parent)).as_posix()
        label = html.escape(target.stem)
        return (
            f'<figure><div class="figurebar"><span>{label}</span>'
            '<span><button class="zoom" data-step="-25" aria-label="缩小图形">−</button>'
            '<button class="zoom" data-step="25" aria-label="放大图形">+</button>'
            '<button class="reset" aria-label="适应图宽">适应宽度</button>'
            f'<a href="{href}" target="_blank">打开 SVG ↗</a></span></div>'
            f'<div class="canvas"><img alt="{label} 架构图" '
            f'src="data:image/svg+xml;base64,{encoded}" /></div></figure>'
        )

    md.renderer.rules["fence"] = fence
    sections = []
    nav = []
    page_anchors = {p.resolve(): f"chapter-{i}" for i, p in enumerate(pages)}
    for i, page in enumerate(pages):
        text = page.read_text()
        title = re.search(r"^# (.+)$", text, re.MULTILINE).group(1)
        rendered_page = md.render(text, {"page": page})

        def rewrite_link(match: re.Match[str], current_page: Path = page) -> str:
            href = match.group(1)
            if href.startswith(("http:", "https:", "#", "mailto:", "data:")):
                return match.group(0)
            target = (current_page.parent / href.split("#")[0]).resolve()
            if target in page_anchors:
                return f'href="#{page_anchors[target]}"'
            if target.is_relative_to(ROOT):
                return f'href="{target.relative_to(ROOT).as_posix()}"'
            return match.group(0)

        rendered_page = re.sub(r'href="([^"]+)"', rewrite_link, rendered_page)
        sections.append(f'<section id="chapter-{i}">{rendered_page}</section>')
        nav.append(f'<a href="#chapter-{i}"><b>{i:02}</b>{html.escape(title)}</a>')
    css = """
    :root{color-scheme:light;--ink:#17243c;--muted:#60718a;--line:#dce4ef;--blue:#215fcc}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f3f6fa;color:var(--ink);font:16px/1.85 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
    aside{position:fixed;inset:0 auto 0 0;width:252px;overflow:auto;background:#142239;color:#dde8fa;padding:30px 20px}
    .brand{font-size:13px;letter-spacing:2px;color:#8facd5}.navtitle{font-size:25px;font-weight:700;line-height:1.4;margin:14px 0 28px}aside a{color:#c8d7ed;display:block;text-decoration:none;padding:12px 9px;font-size:13px;line-height:1.6;border-radius:6px}aside a:hover{background:#273c5b;color:white}aside b{display:inline-block;color:#7ca5df;min-width:27px}aside .meta{font-size:12px;color:#8ea6c5;margin-top:30px}
    main{margin-left:252px;padding:35px 40px 80px;max-width:1530px}header{border-bottom:1px solid var(--line);margin-bottom:25px;padding-bottom:20px;display:flex;justify-content:space-between;gap:20px;align-items:center}header strong{font-size:13px;letter-spacing:1.5px}header span{color:var(--muted);font-size:12px}
    section{background:#fff;border:1px solid var(--line);border-radius:10px;padding:36px 42px;margin-bottom:28px;scroll-margin-top:18px}h1{font-size:30px;line-height:1.35;margin:0 0 26px}h2{font-size:23px;line-height:1.5;margin-top:38px;border-top:1px solid var(--line);padding-top:22px}h3{font-size:18px;margin-top:26px}p{margin:15px 0}a{color:var(--blue);text-underline-offset:3px;overflow-wrap:anywhere}li{margin:7px 0}blockquote{border-left:4px solid #7599d9;background:#f2f6ff;padding:3px 20px;margin:20px 0}
    table{border-collapse:collapse;width:100%;font-size:13px;line-height:1.7;display:block;overflow:auto;margin:22px 0}th,td{border:1px solid var(--line);padding:11px 13px;text-align:left;vertical-align:top;min-width:95px}th{background:#ecf2fa}tr:nth-child(even) td{background:#f9fbfd}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#edf2f7;border-radius:4px;padding:2px 4px;font-size:.87em;overflow-wrap:anywhere}pre{background:#142239;color:#e2ecfb;padding:20px;border-radius:8px;overflow:auto;font-size:13px;line-height:1.7}pre code{background:transparent;padding:0;overflow-wrap:normal}figure{margin:26px 0;border:1px solid #d9e3f1;border-radius:8px;overflow:hidden}.figurebar{display:flex;justify-content:space-between;gap:12px;align-items:center;background:#f3f6fb;font:12px/1.5 ui-monospace,monospace;padding:10px 12px}.figurebar>span:last-child{display:flex;gap:7px;align-items:center;flex-shrink:0}.figurebar button{cursor:pointer;color:#24446f;background:white;border:1px solid #cad8e9;border-radius:4px;padding:4px 8px}.canvas{overflow:auto;background:white;padding:16px}.canvas img{width:100%;max-width:none;display:block}
    @media(max-width:1000px){aside{position:static;width:100%;padding:18px}.navtitle{margin:5px 0;font-size:22px}aside nav{display:flex;flex-wrap:wrap}aside a{max-width:50%}aside .meta{display:none}main{margin:0;padding:20px}section{padding:25px}}
    @media print{aside,header,.figurebar{display:none}main{margin:0;padding:0;max-width:none}section{border:0;padding:0;page-break-after:always}body{background:white;font-size:11px}table{font-size:9px}a{color:inherit}figure{break-inside:avoid}.canvas img{width:100%!important}h1{font-size:24px}}
    """
    js = """
    document.querySelectorAll('.zoom,.reset').forEach(button=>button.addEventListener('click',()=>{
      const img=button.closest('figure').querySelector('img');
      const next=button.classList.contains('reset')?100:Math.max(50,Math.min(400,(parseInt(img.dataset.zoom)||100)+Number(button.dataset.step)));
      img.dataset.zoom=next;img.style.width=next+'%';
    }));
    """
    report = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Agent 框架输入到输出：Codex · Gemini CLI · Claude Code</title>"
        f'<style>{css}</style></head><body><aside><div class="brand">SOURCE RESEARCH</div>'
        '<div class="navtitle">从用户输入<br>到模型输出</div><nav>'
        + "".join(nav)
        + '</nav><div class="meta">2026-09-23<br>固定源码快照 · 官方资料<br>中文说明 · 可缩放架构图<br>离线阅读版</div></aside>'
        "<main><header><strong>CODEX / GEMINI CLI / CLAUDE CODE</strong><span>源码、协议与执行边界</span></header>"
        + "".join(sections)
        + f"</main><script>{js}</script></body></html>"
    )
    (ROOT / "index.html").write_text(report, encoding="utf-8")
    print(
        json.dumps(
            {
                "pages": len(pages),
                "diagrams": len(rendered),
                "html": str(ROOT / "index.html"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

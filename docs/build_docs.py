#!/usr/bin/env python3
"""docs/build_docs.py — Build bilingual HTML docs (zh + en) with language toggle."""

import re
import sys
from pathlib import Path

import markdown
from markdown.extensions.toc import TocExtension

DOCS_DIR = Path(__file__).parent
HTML_DIR = DOCS_DIR / "html"
EN_DIR   = DOCS_DIR / "en"
HTML_EN  = HTML_DIR / "en"

# (zh_name, en_name, description_zh, description_en, icon)
DOC_META: dict[str, tuple[str, str, str, str, str]] = {
    "project_flow":             ("项目总览",  "Project Overview",  "架构设计、数据流与核心决策",             "Architecture, data flow & design decisions",       "🗺️"),
    "system_flow":              ("系统流程",  "System Flow",       "PER Loop 完整步骤详解",                  "PER Loop step-by-step walkthrough",                "⚙️"),
    "failure_analysis":         ("错误记录",  "Bug Log",           "Bug 根因分析与修复方案",                 "Root cause analysis & fixes",                      "🐛"),
    "roadmap":                  ("路线图",    "Roadmap",           "迭代进度、当前状态与未来方向",           "Iteration progress, current status & future directions", "📍"),
    "interview_talking_points": ("面试要点",  "Talking Points",    "技术决策的面试讲解框架",                 "Framework for explaining technical decisions",      "🎯"),
}

DOC_ORDER = ["project_flow", "system_flow", "failure_analysis", "roadmap", "interview_talking_points"]

# ──────────────────────────────────────────────
# Source collection
# ──────────────────────────────────────────────

def _collect(lang: str) -> list[Path]:
    """Return ordered MD source files for a given language ('zh' or 'en')."""
    src_dir = EN_DIR if lang == "en" else DOCS_DIR
    all_files: dict[str, Path] = {}

    for f in src_dir.glob("*.md"):
        all_files[f.stem] = f

    # Also check docs/html/*.md for personal files not in main docs/
    if lang == "zh" and HTML_DIR.exists():
        for f in HTML_DIR.glob("*.md"):
            if f.stem not in all_files:
                all_files[f.stem] = f

    result: list[Path] = []
    seen: set[str] = set()
    for stem in DOC_ORDER:
        if stem in all_files:
            result.append(all_files[stem])
            seen.add(stem)
    for stem, path in sorted(all_files.items()):
        if stem not in seen:
            result.append(path)
    return result

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _meta(stem: str, lang: str) -> tuple[str, str, str]:
    """Return (display_name, description, icon) for a document."""
    m = DOC_META.get(stem)
    if not m:
        return (stem.replace("_", " ").title(), "", "📄")
    if lang == "en":
        return (m[1], m[3], m[4])
    return (m[0], m[2], m[4])


def _extract_title(md_text: str, stem: str, lang: str) -> str:
    match = re.search(r"^#\s+(.+)$", md_text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return _meta(stem, lang)[0]


def _toc_html(html_body: str) -> str:
    headers = re.findall(
        r'<h([23])[^>]*id="([^"]*)"[^>]*>(.*?)</h\1>',
        html_body, re.DOTALL | re.IGNORECASE,
    )
    if len(headers) < 2:
        return ""
    items = []
    for level, anchor, inner in headers:
        text = re.sub(r"<[^>]+>", "", inner).strip()
        cls  = "toc-l3" if level == "3" else "toc-l2"
        items.append(f'<li class="{cls}"><a href="#{anchor}" data-anchor="{anchor}">{text}</a></li>')
    return (
        '<div class="toc-inner">'
        '<p class="toc-title">On this page</p>'
        f'<ul>{"".join(items)}</ul>'
        '</div>'
    )


def _sidebar_links(all_sources: list[Path], current_stem: str, lang: str) -> str:
    links = []
    for src in all_sources:
        name, _, icon = _meta(src.stem, lang)
        active = ' class="active"' if src.stem == current_stem else ""
        links.append(
            f'<a href="{src.stem}.html"{active}>'
            f'<span class="nav-icon">{icon}</span>'
            f'<span>{name}</span>'
            f'</a>'
        )
    return "\n".join(links)


def _lang_toggle(stem: str, lang: str) -> str:
    """Return the language switcher HTML for the sidebar."""
    if lang == "zh":
        zh_class = "lang-active"
        en_class = ""
        en_href  = f"en/{stem}.html"
        zh_href  = f"{stem}.html"
    else:
        zh_class = ""
        en_class = "lang-active"
        en_href  = f"{stem}.html"
        zh_href  = f"../{stem}.html"
    return (
        '<div class="lang-switch">'
        f'<a href="{zh_href}" class="lang-btn {zh_class}">中文</a>'
        f'<a href="{en_href}" class="lang-btn {en_class}">EN</a>'
        '</div>'
    )

# ──────────────────────────────────────────────
# Assets (CSS + JS)
# ──────────────────────────────────────────────

_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --sidebar-w:252px;--toc-w:208px;
  --accent:#2563eb;--accent-light:#eff6ff;
  --sidebar-bg:#f8fafc;--border:#e2e8f0;
  --text:#1e293b;--muted:#64748b;--code-bg:#f1f5f9;
}
html{scroll-behavior:smooth}
body{display:flex;min-height:100vh;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:15px;line-height:1.75;color:var(--text);background:#fff}

/* ── Sidebar ── */
.sidebar{position:fixed;top:0;left:0;bottom:0;width:var(--sidebar-w);background:var(--sidebar-bg);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;z-index:200}
.sidebar-header{padding:22px 18px 16px;border-bottom:1px solid var(--border)}
.sidebar-brand{font-size:16px;font-weight:800;color:var(--text);text-decoration:none;display:flex;align-items:center;gap:8px;letter-spacing:-.4px}
.brand-icon{width:30px;height:30px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;color:#fff;flex-shrink:0}
.brand-text span{color:var(--accent)}
.sidebar-tagline{font-size:11px;color:var(--muted);margin-top:4px;padding-left:38px}

/* ── Language toggle ── */
.lang-switch{display:flex;gap:4px;padding:10px 18px;border-bottom:1px solid var(--border)}
.lang-btn{flex:1;text-align:center;padding:5px 0;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none;color:var(--muted);background:transparent;transition:all .12s;border:1px solid transparent}
.lang-btn:hover{color:var(--text);background:rgba(0,0,0,.05)}
.lang-btn.lang-active{color:var(--accent);background:var(--accent-light);border-color:#bfdbfe}

.sidebar-nav{padding:8px 0;flex:1}
.sidebar-section{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:12px 18px 4px}
.sidebar-nav a{display:flex;align-items:center;gap:9px;padding:7px 18px;font-size:13px;color:var(--muted);text-decoration:none;border-left:3px solid transparent;transition:all .12s}
.sidebar-nav a:hover{color:var(--text);background:rgba(0,0,0,.04)}
.sidebar-nav a.active{color:var(--accent);font-weight:600;border-left-color:var(--accent);background:var(--accent-light)}
.nav-icon{font-size:15px;width:20px;text-align:center;flex-shrink:0}
.sidebar-footer{padding:12px 18px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);line-height:1.6}

/* ── Layout ── */
.main{margin-left:var(--sidebar-w);flex:1;display:flex;justify-content:center;min-width:0}
.content-wrap{width:100%;max-width:820px;padding:52px 60px 100px;min-width:0;flex-shrink:1}
.toc-wrap{width:var(--toc-w);flex-shrink:0;padding:52px 0 52px 4px;align-self:flex-start;position:sticky;top:0;max-height:100vh;overflow-y:auto}

/* ── Right TOC ── */
.toc-inner{font-size:12.5px;line-height:1.6;padding-right:16px}
.toc-title{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.toc-inner ul{list-style:none;padding:0;border-left:1px solid var(--border)}
.toc-inner li{margin:0}
.toc-inner a{display:block;padding:4px 8px;color:var(--muted);text-decoration:none;border-left:2px solid transparent;margin-left:-1px;transition:all .12s}
.toc-inner a:hover{color:var(--accent)}
.toc-inner a.active{color:var(--accent);font-weight:600;border-left-color:var(--accent)}
.toc-l3 a{padding-left:18px;font-size:12px}

/* ── Page header ── */
.page-header{margin-bottom:36px;padding-bottom:24px;border-bottom:1px solid var(--border)}
.page-badge{display:inline-flex;align-items:center;gap:6px;background:var(--accent-light);color:var(--accent);font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;padding:3px 10px;border-radius:20px;margin-bottom:12px}
.page-title{font-size:28px;font-weight:800;line-height:1.25;letter-spacing:-.5px;margin-bottom:6px}
.page-desc{font-size:14px;color:var(--muted)}

/* ── Typography ── */
.content-wrap h2{font-size:20px;font-weight:700;margin:44px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border);scroll-margin-top:24px}
.content-wrap h3{font-size:16px;font-weight:600;margin:28px 0 8px;scroll-margin-top:24px}
.content-wrap h4{font-size:14px;font-weight:600;margin:18px 0 6px;color:var(--muted);scroll-margin-top:24px}
.content-wrap p{margin:10px 0}
.content-wrap ul,.content-wrap ol{margin:10px 0 10px 20px}
.content-wrap li{margin:5px 0}
.content-wrap blockquote{margin:20px 0;padding:14px 18px 14px 20px;background:#f0fdf4;border-left:4px solid #22c55e;border-radius:0 8px 8px 0;color:#15803d;font-style:normal}
.content-wrap blockquote p{margin:0}
.content-wrap table{border-collapse:collapse;width:100%;margin:20px 0;font-size:14px;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.content-wrap th{background:var(--sidebar-bg);font-weight:600;text-align:left;padding:10px 14px;border-bottom:1px solid var(--border);font-size:12.5px;letter-spacing:.2px;color:var(--muted);text-transform:uppercase}
.content-wrap td{padding:9px 14px;border-bottom:1px solid var(--border);vertical-align:top}
.content-wrap tr:last-child td{border-bottom:none}
.content-wrap tr:hover td{background:#fafbfd}
.content-wrap code{font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;font-size:12.5px;background:var(--code-bg);border:1px solid var(--border);border-radius:5px;padding:1px 6px;color:#0f172a}
.content-wrap pre{background:#1e293b;border-radius:10px;padding:20px;overflow-x:auto;margin:20px 0;box-shadow:0 4px 16px rgba(0,0,0,.12)}
.content-wrap pre code{background:none;border:none;padding:0;font-size:13px;color:#e2e8f0}
.content-wrap pre::-webkit-scrollbar{height:4px}
.content-wrap pre::-webkit-scrollbar-thumb{background:#475569;border-radius:2px}
.content-wrap hr{border:none;border-top:1px solid var(--border);margin:32px 0}
.content-wrap a{color:var(--accent);text-decoration:none}
.content-wrap a:hover{text-decoration:underline}
.content-wrap strong{font-weight:600}

@media(max-width:1200px){.toc-wrap{display:none}}
@media(max-width:768px){.sidebar{transform:translateX(-100%)}.main{margin-left:0}.content-wrap{padding:32px 20px 60px}}
"""

_JS = """
document.addEventListener('DOMContentLoaded', () => {
  if (window.hljs) document.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
  const tocLinks = document.querySelectorAll('.toc-inner a[data-anchor]');
  if (!tocLinks.length) return;
  const headers = Array.from(document.querySelectorAll('h2[id],h3[id]'));
  if (!headers.length) return;
  const setActive = id => tocLinks.forEach(a => a.classList.toggle('active', a.dataset.anchor === id));
  const observer = new IntersectionObserver(
    entries => entries.forEach(e => { if (e.isIntersecting) setActive(e.target.id); }),
    { rootMargin: '-10% 0px -80% 0px', threshold: 0 }
  );
  headers.forEach(h => observer.observe(h));
  if (headers[0]) setActive(headers[0].id);
});
"""

# ──────────────────────────────────────────────
# HTML templates
# ──────────────────────────────────────────────

_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="{lang_code}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · MacroLens Docs</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>{css}</style>
</head>
<body>
<aside class="sidebar">
  <div class="sidebar-header">
    <a class="sidebar-brand" href="{index_href}">
      <div class="brand-icon">M</div>
      <div class="brand-text">Macro<span>Lens</span></div>
    </a>
    <div class="sidebar-tagline">Research Agent Docs</div>
  </div>
  {lang_toggle}
  <nav class="sidebar-nav">
    <div class="sidebar-section">{docs_label}</div>
    {sidebar_links}
  </nav>
  <div class="sidebar-footer">{footer_text}</div>
</aside>
<main class="main">
  <article class="content-wrap">{content}</article>
  <div class="toc-wrap">{toc}</div>
</main>
<script>{js}</script>
</body>
</html>
"""

_INDEX_TEMPLATE = """\
<!DOCTYPE html>
<html lang="{lang_code}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MacroLens Docs</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--accent:#2563eb;--accent-light:#eff6ff;--border:#e2e8f0;--text:#1e293b;--muted:#64748b}}
html{{scroll-behavior:smooth}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f8fafc;color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:48px 20px}}
.container{{max-width:580px;width:100%}}
.hero{{text-align:center;margin-bottom:44px}}
.logo{{width:64px;height:64px;background:var(--accent);border-radius:18px;display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:900;color:#fff;margin:0 auto 20px;box-shadow:0 8px 24px rgba(37,99,235,.3)}}
.brand{{font-size:30px;font-weight:800;letter-spacing:-.6px;margin-bottom:6px}}
.brand span{{color:var(--accent)}}
.subtitle{{color:var(--muted);font-size:14px;margin-bottom:12px}}
.lang-switch{{display:inline-flex;gap:6px;border:1px solid var(--border);border-radius:8px;padding:4px;background:#fff;margin-top:10px}}
.lang-btn{{padding:5px 16px;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none;color:var(--muted);transition:all .12s}}
.lang-btn:hover{{color:var(--text)}}
.lang-btn.lang-active{{color:var(--accent);background:var(--accent-light)}}
.doc-list{{list-style:none;display:flex;flex-direction:column;gap:10px;margin-top:32px}}
.doc-card{{background:#fff;border:1px solid var(--border);border-radius:12px;overflow:hidden;transition:box-shadow .15s,border-color .15s}}
.doc-card:hover{{box-shadow:0 4px 20px rgba(0,0,0,.08);border-color:#c7d2fe}}
.doc-card a{{display:flex;align-items:center;gap:16px;padding:18px 22px;text-decoration:none;color:var(--text)}}
.doc-icon{{width:44px;height:44px;border-radius:10px;background:var(--accent-light);display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0}}
.doc-info{{flex:1}}
.doc-name{{font-weight:700;font-size:15px;margin-bottom:2px}}
.doc-desc{{font-size:13px;color:var(--muted)}}
.doc-arrow{{color:#c7d2fe;font-size:20px}}
.footer{{margin-top:28px;font-size:12px;color:var(--muted);text-align:center}}
</style>
</head>
<body>
<div class="container">
  <div class="hero">
    <div class="logo">M</div>
    <div class="brand">Macro<span>Lens</span></div>
    <div class="subtitle">{subtitle}</div>
    <div class="lang-switch">
      <a href="{zh_href}" class="lang-btn {zh_class}">中文</a>
      <a href="{en_href}" class="lang-btn {en_class}">EN</a>
    </div>
  </div>
  <ul class="doc-list">{doc_items}</ul>
  <div class="footer">{footer}</div>
</div>
</body>
</html>
"""

# ──────────────────────────────────────────────
# Builders
# ──────────────────────────────────────────────

def _build_page(md_path: Path, all_sources: list[Path], lang: str) -> str:
    raw   = md_path.read_text(encoding="utf-8")
    stem  = md_path.stem
    title = _extract_title(raw, stem, lang)
    name, desc, icon = _meta(stem, lang)

    md = markdown.Markdown(extensions=[
        "extra", "fenced_code", "tables", "nl2br",
        TocExtension(permalink=False, slugify=lambda s, sep: re.sub(r"[^\w-]", sep, s).lower()),
    ])
    body = md.convert(raw)

    header = (
        f'<div class="page-header">'
        f'<div class="page-badge">{icon} {name}</div>'
        f'<h1 class="page-title">{title}</h1>'
        + (f'<p class="page-desc">{desc}</p>' if desc else "")
        + "</div>"
    )
    body = re.sub(r"^<h1[^>]*>.*?</h1>\s*", "", body, count=1, flags=re.DOTALL)

    index_href   = "../index.html" if lang == "en" else "index.html"
    docs_label   = "Documents" if lang == "en" else "文档"
    footer_text  = "Auto-generated by build_docs.py" if lang == "en" else "由 build_docs.py 自动生成"

    return _PAGE_TEMPLATE.format(
        lang_code     = "en" if lang == "en" else "zh",
        title         = title,
        css           = _CSS,
        js            = _JS,
        index_href    = index_href,
        lang_toggle   = _lang_toggle(stem, lang),
        docs_label    = docs_label,
        sidebar_links = _sidebar_links(all_sources, stem, lang),
        footer_text   = footer_text,
        content       = header + body,
        toc           = _toc_html(body),
    )


def _build_index(all_sources: list[Path], lang: str) -> str:
    items = []
    for src in all_sources:
        name, desc, icon = _meta(src.stem, lang)
        items.append(
            f'<li class="doc-card"><a href="{src.stem}.html">'
            f'<div class="doc-icon">{icon}</div>'
            f'<div class="doc-info"><div class="doc-name">{name}</div>'
            f'<div class="doc-desc">{desc}</div></div>'
            f'<span class="doc-arrow">›</span></a></li>'
        )

    if lang == "en":
        return _INDEX_TEMPLATE.format(
            lang_code = "en",
            subtitle  = "Local Documentation · Auto-generated, not synced to GitHub",
            zh_href   = "../index.html", zh_class = "",
            en_href   = "index.html",   en_class  = "lang-active",
            doc_items = "\n".join(items),
            footer    = "docs/build_docs.py · Rebuilds automatically when MD files change",
        )
    else:
        return _INDEX_TEMPLATE.format(
            lang_code = "zh",
            subtitle  = "本地文档中心 · 自动生成，不同步 GitHub",
            zh_href   = "index.html",    zh_class = "lang-active",
            en_href   = "en/index.html", en_class  = "",
            doc_items = "\n".join(items),
            footer    = "docs/build_docs.py · 修改 MD 文件后自动重建",
        )

# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main() -> None:
    HTML_DIR.mkdir(exist_ok=True)
    HTML_EN.mkdir(exist_ok=True)

    zh_sources = _collect("zh")
    en_sources = _collect("en")

    for src in zh_sources:
        out = HTML_DIR / f"{src.stem}.html"
        out.write_text(_build_page(src, zh_sources, "zh"), encoding="utf-8")
        print(f"[zh] {src.name} -> {out.relative_to(HTML_DIR.parent)}")

    (HTML_DIR / "index.html").write_text(_build_index(zh_sources, "zh"), encoding="utf-8")
    print(f"[zh] index.html ({len(zh_sources)} docs)")

    for src in en_sources:
        out = HTML_EN / f"{src.stem}.html"
        out.write_text(_build_page(src, en_sources, "en"), encoding="utf-8")
        print(f"[en] {src.name} -> {out.relative_to(HTML_DIR.parent)}")

    (HTML_EN / "index.html").write_text(_build_index(en_sources, "en"), encoding="utf-8")
    print(f"[en] index.html ({len(en_sources)} docs)")


def hook_mode() -> None:
    """PostToolUse hook: rebuild only when a docs/*.md or docs/en/*.md was edited."""
    import json
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    fp = (data.get("tool_input") or data).get("file_path", "").replace("\\", "/")
    if "/docs/" in fp and fp.endswith(".md"):
        main()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--hook":
        hook_mode()
    else:
        main()

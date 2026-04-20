"""Purge unused CSS rules from stylesheets.

Token-based purge (PurgeCSS-style):
  1. Scan every content file (HTML + JS) for identifier-like tokens.
  2. For each CSS rule, keep it only if every class/ID/attribute token in
     any of its selectors is present in the content token set.
  3. @font-face, @charset, @import, @namespace, @page are always kept.
  4. @keyframes are kept only if their animation name is referenced in
     the remaining (post-purge) CSS.
  5. @media / @supports are recursed into.

Backups are written as <file>.bak next to each modified CSS file.
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
from pathlib import Path

import cssutils
from cssutils.css import CSSRule

cssutils.log.setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {"node_modules", "2025", "2026", "src", "scss", ".git", ".sass-cache", "scripts"}

CSS_TARGETS = [
    ROOT / "style.css",
    ROOT / "css" / "style.css",
    ROOT / "css" / "bootstrap.min.css",
    ROOT / "css" / "animate.css",
    ROOT / "css" / "material-design-iconic-font.min.css",
    ROOT / "css" / "font-awesome.min.css",
    ROOT / "css" / "owl.carousel.min.css",
    ROOT / "css" / "magnific-popup.css",
    ROOT / "css" / "default-assets" / "classy-nav.css",
]

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def collect_content_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.html", "js/**/*.js", "js/*.js"):
        for f in ROOT.glob(pattern):
            rel_parts = set(f.relative_to(ROOT).parts)
            if rel_parts & EXCLUDE_DIRS:
                continue
            if f.is_file():
                files.append(f)
    return files


def collect_tokens(files: list[Path]) -> set[str]:
    tokens: set[str] = set()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            print(f"warn: could not read {f}: {exc}", file=sys.stderr)
            continue
        tokens.update(TOKEN_RE.findall(text))
    return tokens


# PurgeCSS's default safelist: tokens that are always considered "used".
# We include common state classes that can be toggled dynamically by JS libs
# (bootstrap, owl, jarallax, wow, etc.) whose names may appear only in bundled
# minified JS as string literals we might miss. Keep this list tight.
SAFELIST = {
    "html", "body",
    "active", "show", "hide", "hidden", "fade", "in", "out",
    "open", "close", "opened", "closed", "collapsed", "collapsing", "collapse",
    "disabled", "selected", "checked",
    "loaded", "loading", "ready",
    "sr-only",
}


def selector_matches(selector_text: str, tokens: set[str]) -> bool:
    """Return True if every class/id/attr token in the selector is present
    in the content tokens (or safelist)."""
    # Require attribute-selector attribute names to be known.
    for attr in re.findall(r"\[([A-Za-z][A-Za-z0-9_-]*)", selector_text):
        if attr not in tokens and attr not in SAFELIST:
            return False

    # Strip attribute selectors now that we've checked them.
    s = re.sub(r"\[[^\]]*\]", "", selector_text)
    # Strip pseudo-classes / pseudo-elements (with optional parenthesized args).
    s = re.sub(r":[a-zA-Z-]+\([^)]*\)", "", s)
    s = re.sub(r"::?[a-zA-Z-]+", "", s)

    for cls in re.findall(r"\.([A-Za-z_-][A-Za-z0-9_-]*)", s):
        if cls not in tokens and cls not in SAFELIST:
            return False
    for ident in re.findall(r"#([A-Za-z_-][A-Za-z0-9_-]*)", s):
        if ident not in tokens and ident not in SAFELIST:
            return False
    return True


_KEEP_AS_IS = {
    CSSRule.IMPORT_RULE,
    CSSRule.FONT_FACE_RULE,
    CSSRule.CHARSET_RULE,
    CSSRule.NAMESPACE_RULE,
    CSSRule.PAGE_RULE,
}

_DROP = {
    CSSRule.COMMENT,
}


def purge_rules(rules, tokens: set[str]) -> list[str]:
    out: list[str] = []
    for rule in rules:
        rtype = rule.type
        if rtype == CSSRule.STYLE_RULE:
            kept_sels = [
                sel.selectorText
                for sel in rule.selectorList
                if selector_matches(sel.selectorText, tokens)
            ]
            if kept_sels:
                style_text = rule.style.cssText
                if style_text and style_text.strip():
                    out.append(f"{', '.join(kept_sels)} {{ {style_text} }}")
        elif rtype == CSSRule.MEDIA_RULE:
            inner = purge_rules(rule.cssRules, tokens)
            if inner:
                out.append(f"@media {rule.media.mediaText} {{\n" + "\n".join(inner) + "\n}")
        elif rtype in _KEEP_AS_IS:
            try:
                text = rule.cssText
                if text:
                    out.append(text)
            except Exception:
                pass
        elif rtype in _DROP:
            continue
        else:
            # @supports, @document, unknown at-rules: keep conservatively.
            try:
                text = rule.cssText
                if text:
                    out.append(text)
            except Exception:
                pass
    return out


KEYFRAMES_BLOCK_RE = re.compile(
    r"@(?:-[a-z]+-)?keyframes\s+([A-Za-z_-][A-Za-z0-9_-]*)\s*\{",
    re.IGNORECASE,
)


def find_balanced_block_end(text: str, open_brace_index: int) -> int:
    """Given the index of '{', return the index of its matching '}'."""
    depth = 0
    i = open_brace_index
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def drop_unused_keyframes(css_text: str) -> str:
    """Remove @keyframes NAME { ... } blocks where NAME is not referenced
    anywhere in the remaining CSS (outside keyframes blocks)."""
    # First, collect keyframes block ranges.
    blocks = []  # list of (start, end_inclusive, name)
    for m in KEYFRAMES_BLOCK_RE.finditer(css_text):
        name = m.group(1)
        brace = css_text.find("{", m.end() - 1)
        if brace < 0:
            continue
        end = find_balanced_block_end(css_text, brace)
        if end < 0:
            continue
        blocks.append((m.start(), end + 1, name))

    if not blocks:
        return css_text

    # Build text outside keyframes blocks.
    outside_parts = []
    cursor = 0
    for start, end, _ in blocks:
        outside_parts.append(css_text[cursor:start])
        cursor = end
    outside_parts.append(css_text[cursor:])
    outside = "".join(outside_parts)

    # Tokens used as animation references anywhere outside keyframes blocks.
    used_names: set[str] = set()
    for m in re.finditer(r"animation(?:-name)?\s*:\s*([^;}]+)", outside, re.IGNORECASE):
        for tok in re.findall(r"[A-Za-z_-][A-Za-z0-9_-]*", m.group(1)):
            used_names.add(tok)

    # Rebuild CSS keeping only used keyframes blocks.
    out_parts = []
    cursor = 0
    for start, end, name in blocks:
        out_parts.append(css_text[cursor:start])
        if name in used_names:
            out_parts.append(css_text[start:end])
        cursor = end
    out_parts.append(css_text[cursor:])
    return "".join(out_parts)


def purge_file(path: Path, tokens: set[str]) -> tuple[int, int] | None:
    if not path.exists():
        print(f"skip: {path.relative_to(ROOT)} (missing)")
        return None
    original = path.read_text(encoding="utf-8", errors="replace")
    original_bytes = len(original.encode("utf-8"))

    sheet = cssutils.parseString(original, validate=False)
    kept = purge_rules(sheet.cssRules, tokens)
    purged = "\n".join(kept) + "\n"
    purged = drop_unused_keyframes(purged)

    new_bytes = len(purged.encode("utf-8"))

    # Backup then write.
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(purged, encoding="utf-8")

    return original_bytes, new_bytes


def human(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def main() -> None:
    content_files = collect_content_files()
    print(f"scanning {len(content_files)} content file(s) for tokens...")
    tokens = collect_tokens(content_files)
    print(f"collected {len(tokens):,} unique tokens")

    total_before = 0
    total_after = 0
    rows: list[tuple[str, int, int]] = []
    for target in CSS_TARGETS:
        result = purge_file(target, tokens)
        if result is None:
            continue
        before, after = result
        total_before += before
        total_after += after
        rows.append((str(target.relative_to(ROOT)), before, after))

    print("\nresult:")
    for name, before, after in rows:
        pct = (1 - after / before) * 100 if before else 0
        print(f"  {name:<55} {human(before):>10} -> {human(after):>10}  (-{pct:4.1f}%)")
    if total_before:
        pct = (1 - total_after / total_before) * 100
        print(f"  {'TOTAL':<55} {human(total_before):>10} -> {human(total_after):>10}  (-{pct:4.1f}%)")


if __name__ == "__main__":
    main()



#!/usr/bin/env python3
"""apply-docs.py

Apply harvested YAML docs (docs-db/*.yaml) into source files by locating
HTML doc-id markers like:

  <!-- doc-id: Module.member -->

This first implementation is geared toward applying docs into Objective-C
header files (.h) that use /** ... */ doc blocks.

Modes:
  --dry-run   : compute changes and print a summary (no writes)
  --check     : like --dry-run but exits 1 if any changes would occur
  --write     : write changes to disk

Other:
  --strict    : fail if a referenced doc-id is missing from docs-db
  --verbose   : print per-file/per-doc-id detail and diffs

Example:
  ./apply-docs.py --docs-db docs-db --root ../ios-sdk --write

"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: PyYAML. Install with: pip install pyyaml\n"
        f"Original error: {e}"
    )


DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "build",
    "Build",
    "DerivedData",
    "Pods",
    "Carthage",
    "node_modules",
    ".idea",
    ".vscode",
}

DOC_ID_RE = re.compile(r"<!--\s*doc-id:\s*([A-Za-z0-9_.-]+)\s*-->")

# Matches /** ... */ blocks (Objective-C / JSDoc style)
DOCBLOCK_RE = re.compile(r"/\*\*([\s\S]*?)\*/([ \t]*\r?\n)?", re.MULTILINE)

# Regex for @example placeholder in doc description
EXAMPLE_PLACEHOLDER_RE = re.compile(r"^\s*@example\s+(?P<key>[A-Za-z0-9_.-]+)\s*$")


@dataclass(frozen=True)
class DocEntry:
    doc_id: str
    signature: Optional[str]
    description: str
    examples: Dict[str, dict]
    categories: List[str]
    yaml_path: str

@dataclass(frozen=True)
class DocblockStyle:
    open_line: str
    line_prefix: str   # for normal text lines
    blank_line: str    # for blank doc lines
    close_line: str

def detect_docblock_style(current_block: str, indent: str) -> DocblockStyle:
    # We always render Objective-C docblocks in the canonical style:
    #
    # /**
    #  * line
    #  */
    #
    # Even if the existing file uses the compact style (`*` with no leading space),
    # we normalize to the canonical style for readability and consistent diffs.
    return DocblockStyle(
        open_line=f"{indent}/**",
        line_prefix=f"{indent} * ",
        blank_line=f"{indent} *",
        close_line=f"{indent} */",
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def load_docs_db(docs_db_dir: Path) -> Dict[str, DocEntry]:
    if not docs_db_dir.exists() or not docs_db_dir.is_dir():
        raise SystemExit(f"--docs-db is not a directory: {docs_db_dir}")

    docs: Dict[str, DocEntry] = {}
    for p in sorted(docs_db_dir.glob("*.yaml")):
        data = yaml.safe_load(_read_text(p))
        if not isinstance(data, dict):
            continue

        doc_id = str(data.get("id", "")).strip()
        if not doc_id:
            continue

        signature = data.get("signature")
        if signature is not None:
            signature = str(signature)

        description = str(data.get("description", "") or "")
        examples = data.get("examples") or {}
        if not isinstance(examples, dict):
            examples = {}

        categories = data.get("categories") or []
        if not isinstance(categories, list):
            categories = []
        categories = [str(c) for c in categories]

        docs[doc_id] = DocEntry(
            doc_id=doc_id,
            signature=signature,
            description=description.rstrip(),
            examples=examples,
            categories=categories,
            yaml_path=str(p),
        )

    return docs


def iter_source_files(root: Path, ext_list: List[str], exclude_dirs: set[str]) -> Iterable[Path]:
    ext_set = {e if e.startswith(".") else f".{e}" for e in ext_list}

    for dirpath, dirnames, filenames in os.walk(root):
        # prune excluded dirs
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]

        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in ext_set:
                yield p


def normalize_docblock_indentation(block: str, indent: str) -> str:
    """Ensure each line starts with indent + ' * ' (or indent + ' */')."""
    lines = block.splitlines()
    out: List[str] = []
    for i, line in enumerate(lines):
        if i == 0:
            out.append(f"{indent}/**")
            continue
        if i == len(lines) - 1:
            out.append(f"{indent} */")
            continue
        # Interior lines should already contain ' * ...'
        if line.startswith(indent + " *"):
            out.append(line)
        else:
            stripped = line.lstrip()
            if stripped.startswith("*"):
                stripped = stripped[1:].lstrip()
            out.append(f"{indent} * {stripped}".rstrip())
    # IMPORTANT: do NOT force a trailing newline here.  The source file already
    # contains its own newline(s) after the `*/` token, and adding one here
    # causes an extra blank line to appear between the docblock and the next
    # declaration.
    return "\n".join(out)


def render_docblock(doc: DocEntry, doc_id: str, indent: str, *, example_lang: str, style: DocblockStyle) -> str:
    """Render a /** ... */ doc-block for the given doc entry.

    - Always renders a SINGLE example language (`example_lang`).
    - Never falls back to other languages.
    - If an `@example <key>` placeholder exists in the description but the
      example key is missing in YAML, emit a clear MISSING stub.
    - If the example key exists but the requested language is missing, emit a
      WARNING stub.
    """


    def emit_missing_example(example_key: str) -> None:
        # Placeholder referenced but missing from YAML
        filename = Path(doc.yaml_path).name
        lines.append(f"{style.line_prefix}```{example_lang}".rstrip())
        lines.append(f"{style.line_prefix}// MISSING example {example_key}")
        lines.append(f"{style.line_prefix}// Filename: {filename}")
        lines.append(f"{style.line_prefix}```".rstrip())

    def emit_missing_lang(example_key: str) -> None:
        lines.append(f"{style.line_prefix}```{example_lang}".rstrip())
        lines.append(
            f'{style.line_prefix}// WARNING:  No example block found for lang "{example_lang}" for {example_key}'
        )
        lines.append(f"{style.line_prefix}// Filename: {doc.yaml_path}")
        lines.append(f"{style.line_prefix}```".rstrip())

    lines: List[str] = []
    lines.append(style.open_line)
    lines.append(f"{style.line_prefix}<!-- doc-id: {doc_id} -->".rstrip())

    # Render description, inserting examples *in place* where placeholders occur.
    desc = (doc.description or "").strip("\n")
    seen_placeholder_keys: List[str] = []

    if desc:
        for raw_ln in desc.splitlines():
            # Preserve blank lines.
            if raw_ln == "":
                lines.append(style.blank_line)
                continue

            m_ex = EXAMPLE_PLACEHOLDER_RE.match(raw_ln)
            if m_ex:
                key = m_ex.group("key")
                if key not in seen_placeholder_keys:
                    seen_placeholder_keys.append(key)

                # Ensure a blank line before the example block.
                if lines and lines[-1] != style.blank_line:
                    lines.append(style.blank_line)

                ex = doc.examples.get(key)

                # Example header
                title = ""
                if isinstance(ex, dict):
                    title = str(ex.get("title") or "")
                if not title:
                    title = "Example"

                lines.append(f"{style.line_prefix}@example {title}".rstrip())

                if not isinstance(ex, dict):
                    emit_missing_example(key)
                else:
                    code_map = ex.get("code")
                    if not isinstance(code_map, dict):
                        code_map = {}

                    snippet = code_map.get(example_lang)
                    if isinstance(snippet, str) and snippet.strip():
                        snippet = snippet.rstrip("\n")
                        lines.append(f"{style.line_prefix}```{example_lang}".rstrip())
                        for ln in snippet.splitlines():
                            lines.append(f"{style.line_prefix}{ln}".rstrip())
                        lines.append(f"{style.line_prefix}```".rstrip())
                    else:
                        emit_missing_lang(key)

                # Ensure a blank line after the example block so following prose doesn't stick to it.
                lines.append(style.blank_line)
                continue

            # Normal prose line
            lines.append(f"{style.line_prefix}{raw_ln.rstrip()}".rstrip())

    # If no placeholders were present, render all examples at the end (deterministic order).
    if not seen_placeholder_keys and doc.examples:
        if lines and lines[-1] != style.blank_line:
            lines.append(style.blank_line)

        for key in sorted(doc.examples.keys()):
            ex = doc.examples.get(key)

            title = ""
            if isinstance(ex, dict):
                title = str(ex.get("title") or "")
            if not title:
                title = "Example"

            lines.append(f"{style.line_prefix}@example {title}".rstrip())

            if not isinstance(ex, dict):
                emit_missing_example(key)
            else:
                code_map = ex.get("code")
                if not isinstance(code_map, dict):
                    code_map = {}

                snippet = code_map.get(example_lang)
                if isinstance(snippet, str) and snippet.strip():
                    snippet = snippet.rstrip("\n")
                    lines.append(f"{style.line_prefix}```{example_lang}".rstrip())
                    for ln in snippet.splitlines():
                        lines.append(f"{style.line_prefix}{ln}".rstrip())
                    lines.append(f"{style.line_prefix}```".rstrip())
                else:
                    emit_missing_lang(key)

            lines.append(f"{style.blank_line}")
        # Trim trailing blank line
        while lines and lines[-1] == style.blank_line:
            lines.pop()

    lines.append(style.close_line)
    # IMPORTANT: do NOT force a trailing newline.  The surrounding source text
    # already contains its own newline after the `*/` token.
    return "\n".join(lines)


def find_docblocks_with_ids(text: str) -> List[Tuple[Tuple[int, int], str, str, str]]:
    """Return list of ((start,end), doc_id, indent) for each docblock containing a doc-id marker."""
    results: List[Tuple[Tuple[int, int], str, str]] = []

    for m in DOCBLOCK_RE.finditer(text):
        block_start, block_end = m.span()
        block_text = m.group(0)
        id_match = DOC_ID_RE.search(block_text)
        trailing = m.group(2) or ""
        if not id_match:
            continue
        doc_id = id_match.group(1)

        # Determine indentation from the '/**' line.
        # Look backwards from block_start to line start.
        line_start = text.rfind("\n", 0, block_start)
        if line_start == -1:
            line_start = 0
        else:
            line_start += 1
        indent = re.match(r"[ \t]*", text[line_start:block_start]).group(0)  # type: ignore

        results.append(((block_start, block_end), doc_id, indent, trailing))

    return results


@dataclass
class FileChange:
    path: Path
    original: str
    updated: str
    replaced_ids: List[str]
    missing_ids: List[str]


def apply_docs_to_text(
    text: str,
    docs: Dict[str, DocEntry],
    *,
    strict: bool,
    verbose: bool,
    example_lang: str,
    path_for_logs: Optional[Path] = None,
) -> Tuple[str, List[str], List[str]]:
    """Apply docs to all doc-id blocks in the text."""

    blocks = find_docblocks_with_ids(text)
    if not blocks:
        return text, [], []

    replaced: List[str] = []
    missing: List[str] = []

    # Replace from end -> start so indices remain valid.
    new_text = text
    for (start, end), doc_id, indent, trailing in reversed(blocks):
        entry = docs.get(doc_id)
        if entry is None:
            missing.append(doc_id)
            if strict:
                # In strict mode, we still continue building the new_text so check-mode can show diffs,
                # but we flag it as missing.
                continue
            else:
                continue

        current_block = new_text[start:end]
        style = detect_docblock_style(current_block, indent)
        rendered = render_docblock(entry, doc_id, indent, example_lang=example_lang, style=style)

        if current_block != rendered:            
            if verbose and path_for_logs is not None:
                print(f"[apply-docs] {path_for_logs}: updating doc-id {doc_id}")
            new_text = new_text[:start] + (rendered + trailing) + new_text[end:]
            replaced.append(doc_id)

    return new_text, list(reversed(replaced)), list(reversed(missing))


def unified_diff(a: str, b: str, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply docs-db YAML docs into source files containing <!-- doc-id: ... --> markers")

    p.add_argument("--docs-db", required=True, help="Path to docs-db directory (YAML files)")
    p.add_argument("--root", required=True, help="Root directory of source tree to scan")

    p.add_argument(
        "--ext",
        default=".h",
        help="Comma-separated list of file extensions to scan (default: .h). Example: .h,.m",
    )
    p.add_argument(
        "--lang",
        required=True,
        help='Single example language key to apply (eg: "objc", "ts", "kotlin")',
    )
    
    p.add_argument(
        "--exclude-dirs",
        default=",".join(sorted(DEFAULT_EXCLUDE_DIRS)),
        help="Comma-separated directory names to skip while walking (default: common build/vendor dirs)",
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Write changes to files")
    mode.add_argument("--dry-run", action="store_true", help="Do not write changes (default if no mode is specified)")
    mode.add_argument("--check", action="store_true", help="No writes; exit 1 if any changes would be made")

    p.add_argument("--strict", action="store_true", help="Fail if any referenced doc-id is missing from docs-db")
    p.add_argument("--verbose", action="store_true", help="Verbose logging (including per-id updates; diffs in dry-run/check)")

    args = p.parse_args(argv)

    # Default behavior: dry-run if neither --write nor --check passed
    if not args.write and not args.check and not args.dry_run:
        args.dry_run = True

    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    docs_db_dir = Path(args.docs_db).expanduser().resolve()
    root = Path(args.root).expanduser().resolve()

    ext_list = [e.strip() for e in str(args.ext).split(",") if e.strip()]
    exclude_dirs = {d.strip() for d in str(args.exclude_dirs).split(",") if d.strip()}
    
    example_lang = str(getattr(args, "lang", "") or "").strip() or None

    if not root.exists() or not root.is_dir():
        print(f"ERROR: --root is not a directory: {root}", file=sys.stderr)
        return 2

    docs = load_docs_db(docs_db_dir)
    if args.verbose:
        print(f"[apply-docs] loaded {len(docs)} docs from {docs_db_dir}")

    changes: List[FileChange] = []
    missing_total: Dict[str, List[Path]] = {}

    for path in iter_source_files(root, ext_list, exclude_dirs):
        original = _read_text(path)
        updated, replaced_ids, missing_ids = apply_docs_to_text(
            original,
            docs,
            strict=args.strict,
            verbose=args.verbose,
            path_for_logs=path,
            example_lang=args.lang,
        )

        if missing_ids:
            for mid in missing_ids:
                missing_total.setdefault(mid, []).append(path)

        if updated != original:
            changes.append(
                FileChange(
                    path=path,
                    original=original,
                    updated=updated,
                    replaced_ids=replaced_ids,
                    missing_ids=missing_ids,
                )
            )

    if missing_total:
        msg_lines = ["[apply-docs] Missing doc-ids referenced in source:"]
        for mid in sorted(missing_total.keys()):
            files = missing_total[mid]
            msg_lines.append(f"  - {mid}  (referenced in {len(files)} file(s))")
            if args.verbose:
                for f in files[:20]:
                    msg_lines.append(f"      - {f}")
                if len(files) > 20:
                    msg_lines.append(f"      ... ({len(files) - 20} more)")
        print("\n".join(msg_lines), file=sys.stderr)

        if args.strict:
            # Strict mode: missing ids are an error even if no changes.
            return 2

    if not changes:
        if args.verbose:
            print("[apply-docs] no changes")
        return 0

    # Summaries
    total_blocks = sum(len(c.replaced_ids) for c in changes)
    print(f"[apply-docs] {len(changes)} file(s) would change; {total_blocks} doc-block(s) updated")

    if args.verbose or args.dry_run or args.check:
        for c in changes:
            print(f"- {c.path}  ({len(c.replaced_ids)} block(s))")
            if args.verbose:
                for did in c.replaced_ids:
                    print(f"    • {did}")
            if args.verbose or args.dry_run or args.check:
                d = unified_diff(
                    c.original,
                    c.updated,
                    fromfile=str(c.path),
                    tofile=str(c.path) + " (updated)",
                )
                # Avoid dumping enormous diffs unless verbose is set.
                if args.verbose:
                    print(d)
                else:
                    # Print first ~200 lines of diff as a teaser.
                    diff_lines = d.splitlines()
                    if diff_lines:
                        print("\n".join(diff_lines[:200]))
                        if len(diff_lines) > 200:
                            print(f"... diff truncated ({len(diff_lines)} lines total). Use --verbose for full diff.")

    if args.check:
        return 1

    if args.write:
        for c in changes:
            _write_text(c.path, c.updated)
        print(f"[apply-docs] wrote {len(changes)} file(s)")
        return 0

    # dry-run (default)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
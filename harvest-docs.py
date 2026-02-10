from dataclasses import dataclass, field
from typing import Any, Optional

"""
TypeScript documentation harvester for Background Geolocation SDK.
Extracts JSDoc comments from TypeScript source files and generates YAML documentation database.
"""
from pathlib import Path
import argparse
import re
import sys

# Constants
TS_ROOT = Path("background-geolocation-types/src")

# Some internal/hidden interfaces are used only as mixins and are re-exported
# via the public `BackgroundGeolocation` interface.  We still want to harvest
# their member docs under the public surface area.
#
# NOTE: If more mixin-style interfaces appear in the future, extend this map.
DOCID_CONTAINER_ALIASES: dict[str, str] = {
    "BackgroundGeolocationEvents": "BackgroundGeolocation",
    "BackgroundGeolocationAPI": "BackgroundGeolocation",
}


def _resolve_doc_container(name: str | None) -> str | None:
    if not name:
        return None
    return DOCID_CONTAINER_ALIASES.get(name, name)

# Regular expressions - JSDoc patterns
DOC_BLOCK_RE = re.compile(r"/\*\*(.*?)\*/", re.DOTALL)
STAR_LINE_RE = re.compile(r"^\s*\*\s?(.*)$")
FENCE_RE = re.compile(r"^\s*```")
EXAMPLE_TAG_RE = re.compile(r"^@example(?:\s+(?P<label>.+?))?\s*$")
CATEGORY_TAG_RE = re.compile(r"^@category\s+(?P<cat>.+?)\s*$")
INTERNAL_TAG_RE = re.compile(r"@internal\b")
HIDDEN_TAG_RE = re.compile(r"@hidden\b")

# Regex to detect doc-id HTML comment lines inside JSDoc blocks
DOC_ID_TAG_RE = re.compile(r"<!--\s*doc-id:\s*(?P<id>[^>]+?)\s*-->")

# Regular expressions - TypeScript syntax
EXPORT_CONTAINER_RE = re.compile(r"^\s*export\s+(?P<kind>interface|class|enum)\s+(?P<name>[A-Za-z_]\w*)\b")
EXPORT_NAMED_RE = re.compile(r"^\s*export\s+(?:interface|class|enum|type)\s+(?P<name>[A-Za-z_]\w*)\b")
EXPORT_CONST_RE = re.compile(r"^\s*export\s+const\s+(?P<name>[A-Za-z_]\w*)\b")
MEMBER_RE = re.compile(r"^\s*(?:public|protected|private)?\s*(?:readonly\s+)?(?P<name>[A-Za-z_]\w*)\s*(?:\?|)\s*(?::|\()")
ENUM_MEMBER_RE = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*(?:=|,)(?:.*)$")
# Matches `export const Foo = {` (object-literal const namespace)
EXPORT_CONST_OBJECT_RE = re.compile(
    r"^\s*export\s+const\s+(?P<name>[A-Za-z_]\w*)\s*=\s*\{\s*$"
)

# Regular expressions - Comment patterns
LINE_COMMENT_RE = re.compile(r"^\s*//")
BLOCK_COMMENT_START_RE = re.compile(r"^\s*/\*")
BLOCK_COMMENT_END_RE = re.compile(r".*\*/\s*$")

# ========================================
# Data models
# ========================================

@dataclass
class ParsedExample:
    key: str
    title: str
    lang: str
    code: str


@dataclass
class ParsedDoc:
    id: str
    source_file: str
    signature: str
    categories: list[str] = field(default_factory=list)
    description: str = ""
    examples: dict[str, dict[str, Any]] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)

# ========================================
# YAML utilities
# ========================================

def _require_yaml():
    """Import yaml with helpful error message if not installed."""
    try:
        import yaml  # type: ignore
        return yaml
    except Exception:
        print("Missing dependency: pyyaml. Install it with: uv add pyyaml", file=sys.stderr)
        sys.exit(2)


def setup_yaml_literal_blocks():
    """Configure YAML dumper to use literal block scalars for multiline strings."""
    yaml = _require_yaml()

    class LiteralStr(str):
        """Marker type for YAML literal block scalars."""
        pass

    class CustomDumper(yaml.SafeDumper):
        pass

    def literal_str_representer(dumper, data):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")

    CustomDumper.add_representer(LiteralStr, literal_str_representer)

    def as_yaml_str(s):
        """Use literal block scalar for multiline strings to keep YAML readable."""
        return LiteralStr(s) if "\n" in s else s

    return yaml, CustomDumper, as_yaml_str


# ========================================
# String utilities
# ========================================

def _slugify(text: str) -> str:
    """
    Generate a markdown-style slug from text.
    Lowercase, replace non-alphanumeric with hyphens, collapse runs, strip hyphens.
    """
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "example"


def _normalize_lang(lang: str | None) -> str:
    """Normalize various fenced-code language labels into canonical keys."""
    if not lang:
        return "ts"
    s = lang.strip().lower()
    
    # Common aliases
    if s in {"ts", "typescript", "js", "javascript"}:
        return "ts"
    if s in {"objc", "obj-c", "objective-c", "objectivec"}:
        return "objc"
    if s in {"kt", "kts"}:
        return "kotlin"
    
    return s


def _sanitize_filename(doc_id: str) -> str:
    """Convert doc ID to safe filename."""
    return doc_id.replace("/", "_") + ".yaml"


# ========================================
# Doc block parsing
# ========================================

def extract_doc_blocks(text: str) -> list[str]:
    """Extract all JSDoc comment blocks from source text."""
    return DOC_BLOCK_RE.findall(text)


def normalize_doc_block(block: str) -> list[str]:
    """
    Normalize the inside of `/** ... */` while preserving indentation
    (especially inside fenced code blocks).
    """
    lines = []
    for raw in block.splitlines():
        raw = raw.rstrip()
        m = STAR_LINE_RE.match(raw)
        line = m.group(1).rstrip() if m else raw.rstrip()
        lines.append(line)
    
    # Trim leading/trailing blank lines
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    
    return lines


# ========================================
# Doc-id helpers for JSDoc blocks
# ========================================

def _update_jsdoc_doc_id_block(raw_block: str, doc_id: str) -> tuple[str, bool]:
    """Ensure a JSDoc block contains a leading `<!-- doc-id: ... -->` line.

    - If a doc-id comment already exists and matches, no change.
    - If it exists but differs, update it.
    - If multiple exist, keep only the first (updated) and remove the rest.
    - If it doesn't exist, insert it immediately after the opening `/**` line.

    Returns: (new_block, changed)
    """
    lines = raw_block.splitlines(keepends=False)
    if not lines:
        return raw_block, False

    # Determine indentation from the opening line `/**`
    m_open = re.match(r"^(\s*)/\*\*\s*$", lines[0])
    indent = m_open.group(1) if m_open else re.match(r"^(\s*)", lines[0]).group(1)

    # Locate existing doc-id lines.
    idxs: list[int] = []
    existing_id: str | None = None
    for i, ln in enumerate(lines):
        m = DOC_ID_TAG_RE.search(ln)
        if m:
            idxs.append(i)
            if existing_id is None:
                existing_id = m.group("id").strip()

    # Prefer the same `*` prefix used by the rest of the block so alignment matches.
    # Example: "  * " vs " * " depending on nesting/indentation.
    star_prefix = f"{indent} * "
    for probe in lines[1:6]:
        m_probe = re.match(r"^(\s*\*\s*)", probe)
        if m_probe:
            star_prefix = m_probe.group(1)
            break

    desired_line = f"{star_prefix}<!-- doc-id: {doc_id} -->".rstrip()

    if idxs:
        # Update the first occurrence with the computed star_prefix.
        first_i = idxs[0]
        lines[first_i] = desired_line

        # Remove additional occurrences (from bottom-up to keep indices valid)
        for j in reversed(idxs[1:]):
            del lines[j]

        changed = (existing_id != doc_id) or (len(idxs) > 1)
        return "\n".join(lines) + ("\n" if raw_block.endswith("\n") else ""), changed

    # Insert right after opening line.
    insert_at = 1
    lines.insert(insert_at, desired_line)
    return "\n".join(lines) + ("\n" if raw_block.endswith("\n") else ""), True


def _should_skip_doc_id_insertion(norm_lines: list[str]) -> bool:
    """Return True if the doc-block has @internal or @hidden flags."""
    _desc, _examples, _cats, flags = extract_description_examples_categories(norm_lines)
    return ("internal" in flags) or ("hidden" in flags)


# ========================================
# Example extraction helpers
# ========================================

@dataclass
class DocToken:
    kind: str
    value: Any
    idx: int
    raw: str


def tokenize_doc_lines(lines: list[str]) -> list[DocToken]:
    """Tokenize normalized doc-block lines into a simple stream for parsing."""
    toks: list[DocToken] = []
    for i, line in enumerate(lines):
        s = line.strip()

        m_cat = CATEGORY_TAG_RE.match(s)
        if m_cat:
            toks.append(DocToken("CATEGORY", m_cat.group("cat").strip(), i, line))
            continue

        # Support flags anywhere on a line (e.g. "@internal @hidden" or "Some text. @internal @hidden").
        has_internal = bool(INTERNAL_TAG_RE.search(s))
        has_hidden = bool(HIDDEN_TAG_RE.search(s))
        if has_internal:
            toks.append(DocToken("INTERNAL", True, i, line))
        if has_hidden:
            toks.append(DocToken("HIDDEN", True, i, line))

        # Strip any inline flag tags from the text that flows into `description`.
        if has_internal or has_hidden:
            cleaned = INTERNAL_TAG_RE.sub("", line)
            cleaned = HIDDEN_TAG_RE.sub("", cleaned)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            # If the line was only flags, drop it entirely.
            if not cleaned:
                continue
            # Otherwise, treat the remaining text as normal prose.
            toks.append(DocToken("TEXT", cleaned, i, cleaned))
            continue

        m_ex = EXAMPLE_TAG_RE.match(s)
        if m_ex:
            toks.append(DocToken("EXAMPLE", (m_ex.group("label") or "").strip(), i, line))
            continue

        if FENCE_RE.match(s):
            fence_lang = s[3:].strip() or None
            toks.append(DocToken("FENCE", fence_lang, i, line))
            continue

        toks.append(DocToken("TEXT", line, i, line))

    return toks

def _looks_like_title(line: str) -> bool:
    """
    Heuristic for "label lines" that title the next @example.
    Must end with ":" and not be a generic heading or markdown table.
    """
    s = line.strip()
    if not s or s.startswith("@") or FENCE_RE.match(s):
        return False
    
    # Avoid markdown table rows
    if s.startswith("|") and s.endswith("|"):
        return False
    
    # Must end with ":"
    if not s.endswith(":"):
        return False
    
    # Strip markdown heading markers and emphasis wrappers
    t = re.sub(r"^\s*#+\s*", "", s)
    for _ in range(2):
        if (t.startswith("**") and t.endswith("**")) or (t.startswith("__") and t.endswith("__")):
            t = t[2:-2].strip()
    
    # Reject generic section headings
    t = t.rstrip(":").strip().lower()
    if t in {"examples", "example", "overview"}:
        return False
    
    # Keep titles short; long lines ending with ":" are usually prose
    if len(s) > 60:
        return False
    
    return True


def _normalize_title(line: str) -> str:
    """Extract clean title from label line."""
    return line.strip().rstrip(":").strip()


def _extract_fenced_code(lines: list[str], start_idx: int) -> tuple[str, str, int]:
    """
    Extract a fenced code block starting at start_idx.
    Returns (lang, code, end_idx)
    """
    fence_line = lines[start_idx].strip()
    fence_lang = fence_line[3:].strip() or None
    
    # Extract code lines until closing fence
    code_lines = []
    end_idx = start_idx + 1
    while end_idx < len(lines) and not FENCE_RE.match(lines[end_idx].strip()):
        code_lines.append(lines[end_idx])
        end_idx += 1
    
    code = "\n".join(code_lines).rstrip()
    
    # Include closing fence in end position
    if end_idx < len(lines):
        end_idx += 1
    
    return fence_lang, code, end_idx


def extract_description_examples_categories(lines: list[str]):
    """Parse doc lines into description, examples, categories, and flags.

    Returns:
        description_lines: list[str]
        examples: list[ParsedExample]
        categories: list[str]
        flags: set[str]
    """
    toks = tokenize_doc_lines(lines)

    desc: list[str] = []
    examples: list[ParsedExample] = []
    categories: list[str] = []
    flags: set[str] = set()
    used_keys: set[str] = set()

    in_fence = False
    i = 0

    while i < len(toks):
        tok = toks[i]

        # Keep literal fences in description stream unless we are consuming an @example.
        if tok.kind == "FENCE":
            in_fence = not in_fence
            desc.append(tok.raw)
            i += 1
            continue

        if in_fence:
            desc.append(tok.raw)
            i += 1
            continue

        if tok.kind == "CATEGORY":
            categories.append(str(tok.value))
            i += 1
            continue

        if tok.kind == "INTERNAL":
            flags.add("internal")
            i += 1
            continue

        if tok.kind == "HIDDEN":
            flags.add("hidden")
            i += 1
            continue

        if tok.kind == "EXAMPLE":
            example_num = len(examples) + 1
            label_raw = str(tok.value or "").strip()

            if label_raw:
                base_key = _slugify(label_raw)
                title_from_tag: Optional[str] = label_raw
            else:
                base_key = f"example-{example_num}"
                title_from_tag = None

            # Look after @example for a label line (legacy "Foo:") if no label was on the tag.
            j = tok.idx + 1
            while j < len(lines) and not lines[j].strip():
                j += 1

            title = title_from_tag
            if title is None and j < len(lines) and _looks_like_title(lines[j]):
                title = _normalize_title(lines[j])
                j += 1
                while j < len(lines) and not lines[j].strip():
                    j += 1

            # Or look before @example if still missing.
            if title is None:
                k = len(desc) - 1
                while k >= 0 and not desc[k].strip():
                    k -= 1
                if k >= 0 and _looks_like_title(desc[k]):
                    title = _normalize_title(desc[k])
                    del desc[k:]

            # Find the fenced code block after @example
            fence_start = j
            while fence_start < len(lines):
                if EXAMPLE_TAG_RE.match(lines[fence_start].strip()):
                    break
                if FENCE_RE.match(lines[fence_start].strip()):
                    break
                fence_start += 1

            if fence_start >= len(lines) or not FENCE_RE.match(lines[fence_start].strip()):
                i += 1
                continue

            fence_lang, code, end_idx = _extract_fenced_code(lines, fence_start)
            norm_lang = _normalize_lang(fence_lang)

            # Ensure unique key
            ex_key = base_key
            suffix = 2
            while ex_key in used_keys:
                ex_key = f"{base_key}-{suffix}"
                suffix += 1
            used_keys.add(ex_key)

            # Put placeholder in description *exactly where @example appeared*
            desc.append(f"@example {ex_key}")

            examples.append(
                ParsedExample(
                    key=ex_key,
                    title=title or f"Example {example_num}",
                    lang=norm_lang,
                    code=code,
                )
            )

            # Advance token index to after extracted code block
            while i < len(toks) and toks[i].idx < end_idx:
                i += 1
            continue

        desc.append(tok.raw)
        i += 1

    while desc and not desc[-1].strip():
        desc.pop()

    return desc, examples, categories, flags


# ========================================
# TypeScript signature parsing
# ========================================

def _find_next_signature(text: str, end_pos: int) -> tuple[str | None, int | None]:
    """
    Find the next TypeScript signature line after a doc block.
    Returns (signature, absolute_line_number)
    """
    start_line_no = text[:end_pos].count("\n")
    lines = text.splitlines()
    in_block_comment = False
    
    for i in range(start_line_no + 1, len(lines)):
        raw = lines[i]
        s = raw.strip()
        
        if not s:
            continue
        
        # Handle multi-line block comments
        if in_block_comment:
            if BLOCK_COMMENT_END_RE.match(s):
                in_block_comment = False
            continue
        
        if BLOCK_COMMENT_START_RE.match(s) and not BLOCK_COMMENT_END_RE.match(s):
            in_block_comment = True
            continue
        
        # Skip single-line comments
        if LINE_COMMENT_RE.match(s):
            continue
        
        # Skip docblock closing markers
        if s == "*/" or (s.endswith("*/") and not s.rstrip("*/").strip()):
            continue
        
        # Found the signature
        return raw.rstrip(), i
    
    return None, None


def _infer_doc_id(signature: str, container: str | None, container_kind: str | None) -> str | None:
    """Infer documentation ID from signature and context."""
    s = signature.strip()
    container = _resolve_doc_container(container)
    
    # Check for export const
    m = EXPORT_CONST_RE.match(s)
    if m:
        return m.group("name")
    
    # Check for named exports
    m = EXPORT_NAMED_RE.match(s)
    if m:
        return m.group("name")
    
    # Check for members within containers
    # Check for members within containers
    if container:
        if container_kind == "enum":
            m = ENUM_MEMBER_RE.match(s)
            if m:
                return f"{container}.{m.group('name')}"

        # Object-literal const namespaces (e.g. `export const ActivityType = { Other: 1, ... }`)
        if container_kind == "const":
            m = MEMBER_RE.match(s)
            if m:
                return f"{container}.{m.group('name')}"

        # Interface/class members
        m = MEMBER_RE.match(s)
        if m:
            return f"{container}.{m.group('name')}"
    
    return None


# ========================================
# File scanning and seeding
# ========================================


def insert_doc_ids(limit: int | None = None) -> None:
    """Insert/update `<!-- doc-id: ... -->` at the top of each harvestable JSDoc block.

    This edits TypeScript source files in-place under TS_ROOT (skipping `legacy`).
    It will NOT insert doc-ids into blocks marked @internal or @hidden.
    """
    files_scanned = 0
    blocks_seen = 0
    blocks_updated = 0

    for ts_file in TS_ROOT.rglob("*.ts"):
        if "legacy" in ts_file.parts:
            continue

        files_scanned += 1
        text = ts_file.read_text(encoding="utf-8")
        lines_src = text.splitlines()

        # Precompute containers by scanning lines (same approach as seeding)
        containers_by_line: list[tuple[int, str, str]] = []
        for idx, line in enumerate(lines_src):
            m = EXPORT_CONTAINER_RE.match(line)
            if m:
                containers_by_line.append((idx, m.group("name"), m.group("kind")))
                continue
            m = EXPORT_CONST_OBJECT_RE.match(line)
            if m:
                containers_by_line.append((idx, m.group("name"), "const"))

        def container_for_signature(sig_line_no: int | None) -> tuple[str | None, str | None]:
            if sig_line_no is None:
                return None, None
            c_name, c_kind = None, None
            for li, name, kind in containers_by_line:
                if li <= sig_line_no:
                    c_name, c_kind = name, kind
                else:
                    break
            return c_name, c_kind

        # Build an edited version of the file by replacing blocks where needed.
        out_parts: list[str] = []
        last_end = 0
        changed_file = False

        for m in re.finditer(r"/\*\*(.*?)\*/", text, re.DOTALL):
            blocks_seen += 1
            raw_block = m.group(0)
            block_inner = m.group(1)

            signature, sig_line_no = _find_next_signature(text, m.end())
            if not signature:
                continue

            container_name, container_kind = container_for_signature(sig_line_no)
            doc_id = _infer_doc_id(signature, container_name, container_kind)
            if not doc_id:
                continue

            # Skip internal/hidden doc blocks.
            norm_lines = normalize_doc_block(block_inner)
            if _should_skip_doc_id_insertion(norm_lines):
                continue

            updated_block, did_change = _update_jsdoc_doc_id_block(raw_block, doc_id)
            if not did_change:
                continue

            # Emit text up to block, then updated block
            out_parts.append(text[last_end:m.start()])
            out_parts.append(updated_block)
            last_end = m.end()
            changed_file = True
            blocks_updated += 1

            if limit is not None and blocks_updated >= limit:
                break

        if limit is not None and blocks_updated >= limit:
            # If we bailed early on block updates, still complete this file's output
            out_parts.append(text[last_end:])
            new_text = "".join(out_parts) if changed_file else text
            if changed_file and new_text != text:
                ts_file.write_text(new_text, encoding="utf-8")
            break

        if changed_file:
            out_parts.append(text[last_end:])
            new_text = "".join(out_parts)
            if new_text != text:
                ts_file.write_text(new_text, encoding="utf-8")

    print(f"[insert-doc-ids] summary: files_scanned={files_scanned}, blocks_seen={blocks_seen}, blocks_updated={blocks_updated}")


def seed_docs(out_dir: Path, limit: int | None, prune_orphans: bool = False):
    """
    Scan TypeScript files and generate YAML documentation database.
    Merges with existing docs to preserve manually-added examples in other languages.
    """
    yaml, Dumper, as_yaml_str = setup_yaml_literal_blocks()
    
    out_dir.mkdir(parents=True, exist_ok=True)
    
    files_scanned = 0
    blocks_found = 0
    written = 0
    skipped = 0
    seen_doc_ids = set()
    generated_paths: set[Path] = set()

    for ts_file in TS_ROOT.rglob("*.ts"):
        if "legacy" in ts_file.parts:
            continue
        
        files_scanned += 1
        text = ts_file.read_text(encoding="utf-8")
        
        # Precompute containers by scanning lines
        # Precompute containers by scanning lines
        containers_by_line = []
        for idx, line in enumerate(text.splitlines()):
            m = EXPORT_CONTAINER_RE.match(line)
            if m:
                containers_by_line.append((idx, m.group("name"), m.group("kind")))
                continue

            # Treat `export const Foo = {` as a container so we can doc its members
            m = EXPORT_CONST_OBJECT_RE.match(line)
            if m:
                containers_by_line.append((idx, m.group("name"), "const"))
        
        def container_for_signature(sig_line_no: int | None) -> tuple[str | None, str | None]:
            """Find the containing class/interface/enum for a signature line."""
            if sig_line_no is None:
                return None, None
            
            c_name, c_kind = None, None
            for li, name, kind in containers_by_line:
                if li <= sig_line_no:
                    c_name, c_kind = name, kind
                else:
                    break
            return c_name, c_kind
        
        # Process each doc block
        for m in re.finditer(r"/\*\*(.*?)\*/", text, re.DOTALL):
            blocks_found += 1
            block_inner = m.group(1)
            
            # Find the signature this doc describes
            signature, sig_line_no = _find_next_signature(text, m.end())
            if not signature:
                skipped += 1
                continue
            
            # Infer the documentation ID
            container_name, container_kind = container_for_signature(sig_line_no)
            doc_id = _infer_doc_id(signature, container_name, container_kind)
            if not doc_id:
                skipped += 1
                continue
            
            # Prevent duplicates in this run
            if doc_id in seen_doc_ids:
                skipped += 1
                continue
            
            # Parse the doc block
            norm_lines = normalize_doc_block(block_inner)
            desc_lines, examples_list, categories, flags = extract_description_examples_categories(norm_lines)
            
            # Skip internal/hidden items
            if "internal" in flags or "hidden" in flags:
                skipped += 1
                continue
            
            # Build the YAML payload
            description = "\n".join(desc_lines).strip()
            examples = {}
            for ex in examples_list:
                examples[ex.key] = {
                    "title": ex.title,
                    "code": {
                        ex.lang: as_yaml_str(ex.code),
                    },
                }
            
            payload = {
                "id": doc_id,
                "source_file": str(ts_file),
                "signature": signature.strip(),
            }
            
            if categories:
                payload["categories"] = categories
            if description:
                payload["description"] = as_yaml_str(description)
            if examples:
                payload["examples"] = examples
            
            out_path = out_dir / _sanitize_filename(doc_id)
            
            # Merge with existing doc to preserve manually-added examples
            if out_path.exists():
                try:
                    existing = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    existing = {}
                
                # Preserve existing description unless we parsed a new one
                if "description" not in payload and existing.get("description"):
                    payload["description"] = existing.get("description")
                
                # Merge examples by key, preserving other languages
                if existing.get("examples"):
                    payload.setdefault("examples", {})
                    for ex_key, ex_val in existing["examples"].items():
                        if ex_key not in payload["examples"]:
                            # Keep the entire existing example
                            payload["examples"][ex_key] = ex_val
                        else:
                            # Merge code maps for same example key
                            existing_code = (ex_val or {}).get("code") or {}
                            new_code = (payload["examples"][ex_key] or {}).get("code") or {}
                            merged_code = dict(existing_code)
                            merged_code.update(new_code)  # New TypeScript code wins
                            
                            payload["examples"][ex_key].setdefault(
                                "title", (ex_val or {}).get("title")
                            )
                            payload["examples"][ex_key]["code"] = merged_code
            
            # Write YAML file
            out_path.write_text(
                yaml.dump(
                    payload,
                    Dumper=Dumper,
                    sort_keys=False,
                    allow_unicode=True,
                    width=1000,
                ),
                encoding="utf-8",
            )
            generated_paths.add(out_path)

            written += 1
            seen_doc_ids.add(doc_id)
            
            if limit is not None and written >= limit:
                break
        
        if limit is not None and written >= limit:
            break
    
    # Optionally delete YAML files in out_dir that were not generated in this run.
    if prune_orphans:
        pruned = 0
        for p in out_dir.glob("*.yaml"):
            if p not in generated_paths:
                try:
                    p.unlink()
                    pruned += 1
                except Exception:
                    pass
        if pruned:
            print(f"[seed] pruned orphan yaml files: {pruned}")

    # Print summary
    print(f"[seed] summary: files_scanned={files_scanned}, blocks_found={blocks_found}, "
          f"written={written}, skipped={skipped}")
    
    if written == 0 and any(out_dir.iterdir()):
        print(f"Note: No files written. Existing files detected in '{out_dir}'. "
              "You may have already seeded this directory.")


# ========================================
# Diagnostic commands
# ========================================

def parse_file(path: Path):
    """Simple diagnostic: count doc blocks in a file."""
    text = path.read_text(encoding="utf-8")
    blocks = extract_doc_blocks(text)
    if blocks:
        print(f"{path}: {len(blocks)} doc blocks")


def dump_file(path: Path, max_blocks: int):
    """Dump normalized doc blocks from a file."""
    text = path.read_text(encoding="utf-8")
    blocks = extract_doc_blocks(text)
    print(f"{path}: {len(blocks)} doc blocks\n")
    
    for i, block in enumerate(blocks[:max_blocks], start=1):
        lines = normalize_doc_block(block)
        n_examples = sum(1 for ln in lines if EXAMPLE_TAG_RE.match(ln.strip()))
        print(f"----- BLOCK {i} (examples: {n_examples}) -----")
        for ln in lines:
            print(ln)
        print()


def dump_extracted_file(path: Path, max_blocks: int):
    """Dump extracted description/examples summary for a file."""
    text = path.read_text(encoding="utf-8")
    blocks = extract_doc_blocks(text)
    print(f"{path}: {len(blocks)} doc blocks\n")
    
    for i, block in enumerate(blocks[:max_blocks], start=1):
        lines = normalize_doc_block(block)
        desc, examples, categories, flags = extract_description_examples_categories(lines)
        
        print(f"----- BLOCK {i} -----")
        if categories:
            print(f"categories: {categories}")
        print(f"examples: {len(examples)}")
        for ex in examples:
            first = ex.code.splitlines()[0] if ex.code else ""
            print(f"  - {ex.key}: {ex.title}  (lang={ex.lang})")
            print(f"    first line: {first}")
        print(f"description lines: {len(desc)}")
        if flags:
            print(f"flags: {sorted(flags)}")
        
        preview = desc[:8]
        if preview:
            print("description preview:")
            for ln in preview:
                print(f"  {ln}")
        print()


def resolve_dump_path(arg: str) -> Path:
    """Resolve a path argument, trying both absolute and relative to TS_ROOT."""
    p = Path(arg)
    if p.exists():
        return p
    return TS_ROOT / arg


# ========================================
# Main entry point
# ========================================

def main():
    parser = argparse.ArgumentParser(
        description="TypeScript documentation harvester for Background Geolocation SDK"
    )
    parser.add_argument(
        "--dump",
        help="Dump normalized doc blocks from a single file (path relative to TS_ROOT is OK)",
    )
    parser.add_argument(
        "--dump-extracted",
        help="Dump extracted description/examples summary for a single file",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed a YAML docs database to --out-dir (default: docs-db)",
    )
    parser.add_argument(
        "--insert-doc-ids",
        action="store_true",
        help="Insert/update <!-- doc-id: ... --> at the top of each harvestable JSDoc block (edits TS sources in-place)",
    )
    parser.add_argument(
        "--out-dir",
        default="docs-db",
        help="Output directory for --seed (default: docs-db)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on number of YAML files to write (for --seed)",
    )
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=2,
        help="Max number of doc blocks to dump (only for --dump / --dump-extracted)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="When seeding, delete YAML files in --out-dir that were not generated in this run",
    )
    args = parser.parse_args()
    
    if not TS_ROOT.exists():
        raise RuntimeError(f"TS root not found: {TS_ROOT}")
    
    # Handle dump commands
    if args.dump or args.dump_extracted:
        target = args.dump_extracted or args.dump
        path = resolve_dump_path(target)
        
        if "legacy" in path.parts:
            print(f"Refusing to read legacy file: {path}", file=sys.stderr)
            sys.exit(2)
        
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(2)
        
        if args.dump_extracted:
            dump_extracted_file(path, max_blocks=args.max_blocks)
        else:
            dump_file(path, max_blocks=args.max_blocks)
        return

    if args.insert_doc_ids:
        insert_doc_ids(limit=args.limit)
        return
    
    # Handle seed command
    if args.seed:
        seed_docs(Path(args.out_dir), limit=args.limit, prune_orphans=args.prune)
        return
    
    # Default mode: list doc-block counts (skip legacy)
    for ts_file in TS_ROOT.rglob("*.ts"):
        if "legacy" in ts_file.parts:
            continue
        parse_file(ts_file)


if __name__ == "__main__":
    main()
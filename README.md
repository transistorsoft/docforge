# Docforge — harvest once, apply everywhere

Docforge is a small tooling pair that lets you:

1. **Harvest** JSDoc from a TypeScript API surface into a portable **YAML docs database** (`docs-db/*.yaml`).
2. **Apply** those harvested docs into other source trees (currently focused on **Objective‑C `.h` headers**) by looking for `<!-- doc-id: ... -->` markers.

This repo contains two scripts:

- `harvest.py` — scans TypeScript, extracts/normalizes docs, and writes `docs-db/*.yaml`.
- `apply-docs.py` — scans source files (eg. iOS SDK headers), finds `doc-id` markers, and rewrites the surrounding docblocks with the harvested content.

---

## Requirements

- Python 3.10+
- `pyyaml`

If you use **uv** (recommended):

```bash
uv add pyyaml
```

Or with pip:

```bash
python -m pip install pyyaml
```

---

## The docs database

`harvest.py` emits one YAML file per symbol, named after the doc id:

- `Foo.yaml`
- `Foo.bar.yaml`

Each YAML entry looks like:

```yaml
id: BackgroundGeolocation.onLocation
source_file: background-geolocation-types/src/core/api/BackgroundGeolocation.ts
signature: 'onLocation(cb: ...): Subscription;'
description: |-
  Subscribe to location events.

  @example example-1
examples:
  example-1:
    title: Example 1
    code:
      ts: |-
        // ...
      objc: |-
        // ...
```

Important conventions:

- `description` is **markdown-ish** and may contain **example placeholders** like `@example example-1`.
- `examples.<key>.code` is a map of **language → snippet** (`ts`, `objc`, `kotlin`, …).
- When applying, Docforge inserts the chosen language snippet **at the placeholder location**.

---

## Harvesting docs (TypeScript → YAML)

### 1) Seed a docs database

```bash
# Harvest from the default TypeScript root (background-geolocation-types/src)
uv run harvest.py --seed --out-dir docs-db

# Or harvest from a custom TypeScript root
uv run harvest.py --seed --source /path/to/your/types/src --out-dir docs-db
```

Useful options:

- `--source PATH` — TypeScript source root to harvest (default: `background-geolocation-types/src`).
- `--limit N` — stop after writing N YAML files (debugging).
- `--prune` — delete YAML files in `--out-dir` that were **not** generated in this run (keeps the db tidy).

### 2) Insert doc-id markers into TypeScript (optional, but recommended)

To make it easy to reference symbols later, you can insert/update a `doc-id` marker at the top of each harvestable JSDoc block:

```bash
# Insert into the default TS root
uv run harvest.py --insert-doc-ids

# Or target a custom TS root
uv run harvest.py --insert-doc-ids --source /path/to/your/types/src
```

This produces JSDoc blocks like:

```ts
/**
 * <!-- doc-id: ActivityType -->
 * iOS Activity Type used with {@link GeoConfig.activityType}.
 */
export const ActivityType = {
  // ...
}
```

Notes:

- `--insert-doc-ids` skips blocks marked `@internal` or `@hidden`.
- Some internal/hidden mixin-style interfaces may still have **public member docs**; Docforge can alias containers when harvesting (see code comments in `harvest.py`).

### 3) Diagnostic helpers

```bash
# Dump normalized doc blocks from a file (paths are relative to --source)
uv run harvest.py --dump core/api/BackgroundGeolocation.ts --max-blocks 3

# Or with an explicit source root
uv run harvest.py --dump --source /path/to/your/types/src core/api/BackgroundGeolocation.ts --max-blocks 3

# Dump extracted description/examples summary
uv run harvest.py --dump-extracted core/api/BackgroundGeolocation.ts --max-blocks 3
```

---

## Applying docs (YAML → Objective‑C headers)

`apply-docs.py` walks a source tree, finds Objective‑C docblocks containing:

```c
<!-- doc-id: Module.member -->
```

…and replaces the surrounding `/** ... */` block with the harvested description + examples.

### Basic usage

Dry-run (no writes):

```bash
uv run apply-docs.py --docs-db docs-db --root ../ios-sdk --ext .h --lang objc --dry-run
```

Write changes:

```bash
uv run apply-docs.py --docs-db docs-db --root ../ios-sdk --ext .h --lang objc --write
```

### Modes

Exactly one of these behaviors is used:

- `--dry-run` — compute changes and print a summary (no writes). *(Default if no mode is specified.)*
- `--check` — like `--dry-run`, but exits **1** if any changes would be made (useful for CI).
- `--write` — apply changes to disk.

### Key switches

- `--docs-db PATH` *(required)*: directory containing `*.yaml` docs.
- `--root PATH` *(required)*: root of the source tree to scan.
- `--ext .h` *(default: `.h`)*: comma-separated list of extensions to scan.
- `--exclude-dirs a,b,c` *(optional)*: directory names to skip (defaults include `Pods`, `DerivedData`, `node_modules`, etc.).
- `--lang <key>` *(required)*: **single** example language to apply (eg. `objc`).
- `--verbose`: print per-file/per-id updates and full diffs.
- `--strict`: fail if a referenced doc-id is missing from the docs-db.

### What happens to examples

The YAML `description` can contain placeholders like:

```
@example example-1
```

When applying:

- The placeholder is replaced in-place with a rendered block:
  - `@example <Title>`
  - a fenced code block in your selected `--lang`

If an example is referenced but missing:

```objc
// MISSING example example-1
// Filename: BackgroundGeolocation.onLocation.yaml
```

If the example exists, but not for the requested language:

```objc
// WARNING:  No example block found for lang "objc" for example-1
// Filename: /absolute/path/to/docs-db/BackgroundGeolocation.onLocation.yaml
```

### Output formatting

`apply-docs.py` normalizes Objective‑C docblocks to the canonical style:

```c
/**
 * <!-- doc-id: BackgroundGeolocation.onLocation -->
 * Subscribe to location events.
 *
 * @example Example 1
 * ```objc
 * // ...
 * ```
 */
```

This ensures consistent alignment and clean diffs, even if the original header used a compact `/**\n* ...` style.

---

## Typical workflow

1. **Harvest** from TypeScript:

   ```bash
   uv run harvest.py --seed --out-dir docs-db --prune
   # (optional) override the TS root
   # uv run harvest.py --seed --source /path/to/your/types/src --out-dir docs-db --prune
   ```

2. Add/edit non-TS examples (eg. `objc`) directly in `docs-db/*.yaml` as needed.

3. **Apply** into the iOS SDK headers:

   ```bash
   uv run apply-docs.py --docs-db docs-db --root ../ios-sdk --ext .h --lang objc --write
   ```

4. Use `--check` in CI to enforce that generated docs stay up to date:

   ```bash
   uv run apply-docs.py --docs-db docs-db --root ../ios-sdk --ext .h --lang objc --check
   ```

---

## Tips

- If you’re missing doc-ids in a destination source tree, add markers like:

  ```c
  /**
   * <!-- doc-id: BackgroundGeolocation.onLocation -->
   */
  - (void)onLocation:...;
  ```

  Then re-run `apply-docs.py`.

- Keep `docs-db/` in version control — it’s the canonical “single source of truth” that can be applied to multiple SDKs.

---

## License

TBD

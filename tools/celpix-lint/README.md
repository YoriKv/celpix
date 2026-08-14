# celpix-lint

Static checks for hand-edited `.celpix` project files.

A celPix project **loads tolerantly by design**. An unrecognised `kind` reads as
`"file"`. A preset id nothing answers to degrades to the stage default, and that
fallback is what the next save writes. A malformed glyph record is skipped, one
line at a time. A tile binding onto a missing entry leaves the map unbound. Each
of those is the right call for a loader — a project that will not open is worse
than one that opens degraded — and each of them is silent.

This reports what a load would silently change. It is aimed squarely at project
files edited by hand or by an agent, where the JSON is written without the UI
that normally makes these states unreachable.

## Running it

```bash
# From the celPix repo, no install:
uvx --from ./tools/celpix-lint celpix-lint sample-projects/tmnt2/tmnt.celpix

# A whole tree of projects:
uvx --from ./tools/celpix-lint celpix-lint sample-projects/

# Installed as a tool, for use anywhere:
uv tool install ./tools/celpix-lint
celpix-lint my-hack.celpix
```

Exit status is **1** when anything at or above `--fail-on` (default `error`) is
found, **0** otherwise, and **2** for the linter's own failures — a bad argument,
no such file. "The project is broken" and "the run is broken" are never the same
answer, so this is safe in a pre-commit hook or a CI step.

### Options

| Flag | What it does |
| --- | --- |
| `--format json` | Machine-readable, every finding carrying a JSON Pointer at the value it is about. This is the form to hand an agent that is going to fix the file. |
| `--min-severity error\|warning\|info` | Hide findings below a level. Default `info` — everything. |
| `--fail-on error\|warning\|info\|never` | What earns exit 1. Default `error`. |
| `--live` | Resolve plugin ids against the installed celPix rather than the shipped snapshot (see below). |
| `--no-files` | Skip every check that touches the disk, for a project whose ROMs are not here. |
| `--select` / `--ignore` | Comma-separated codes. A bare letter selects a level (`E`), a prefix a family (`E3`, `W51`). |

## Severity means what the loader does

Not how bad it looks:

- **error** — the loader will silently drop or misread this. Something the
  author wrote will not be in the project they open.
- **warning** — it degrades gracefully, but the result is probably not what was
  meant: an unbound tilemap, a palette falling back to the generated default.
- **info** — the project is correct and opens as written. The file is merely not
  in the form celPix itself would write it: a redundant default, an id that a
  re-save would rewrite.

So `--fail-on error` is the useful default, and `--fail-on info` is a
"normalize this file" gate rather than a correctness one.

## What it catches

Roughly, by family:

| Codes | About |
| --- | --- |
| `F0xx` | The file is not a readable project at all — I/O, JSON syntax, wrong shape. Fatal; nothing else runs. |
| `E1xx` `W1xx` | The document: `version`, `current`, the project-wide view settings. |
| `E2xx` `W2xx` | Entry shape — the kind, the keys that kind actually reads, the scalars. |
| `E3xx` | The files on disk: missing references, offsets past the end, one file open twice. |
| `E4xx` | Plugin and preset ids, and containers framing the wrong kind of entry. |
| `E5xx` | References between entries: parents, joined regions, tile bindings, composite pieces. |
| `E6xx` | `session` and `palette`, and whether the mode and the block agree. |
| `E7xx` | The `view` block, the tile rearrangement, the pinned palette regions. |
| `E8xx` | The `font` alphabet. |

The findings that have paid for themselves so far, on this repo's own sample
projects: bookmarks into a four-chip joined region that carried no `extra_paths`
of their own and so resolved 0x24000 bytes past the end of chip one; a `current`
index naming a palette entry, which cannot be shown, so the project opened on
nothing; a `palette_mode` of `"offset"` with no `palette` block, which reads
colors from byte 0 instead of the offset that was meant.

**What no linter can catch is a reference that shifted.** `current`,
`tile_source.entry_index` and `pieces[].entry_index` are positions in `entries`,
so inserting or moving a record slides every reference past it onto its
neighbour. `E1xx`/`E5xx` report one that ends up out of range or naming something
it may not name; one that lands on another perfectly bindable entry is
indistinguishable from the binding that was meant, and opens quietly wrong. The
answer is upstream — resolve positions in one pass when generating the file, and
rearrange by re-running the generator or by dragging rows in the app
(`docs/design/project-format.md` §1).

## Where it gets the schema

**Restated, not imported** — see `src/celpix_lint/schema.py`. Two reasons, and
the second is the real one:

1. It has to run where celPix is not installed.
2. **The reader cannot be used to lint.** By the time `projectfile.py` hands back
   an `Entry`, the evidence of the mistake is gone: the unknown kind is already
   `"file"`, the bad glyph is already skipped. Checking the *document* rather
   than the parse result is the only way to see what was written as opposed to
   what was understood.

## Where it gets the plugin ids

Two sources, and the report says which answered, because they support different
claims:

- **The shipped snapshot** (`data/registry.json`, default) knows only what celPix
  ships. An id it has never heard of might be a typo or might be a plugin you
  have installed, and it cannot tell which — so it says the weaker thing
  (`W405`, "not one of celPix's built-in ids") rather than claiming the id is
  missing.
- **`--live`** imports the installed celPix and asks its registry, your own
  dropped plugins included. That source *can* say an id resolves nowhere
  (`E404`).

Either way, the `plugins/` folder beside the project file is read first. A
project travels with its formats, so a preset only that folder provides is not
missing at all. Skipping this step produced 420 false positives across this
repo's sample projects, which is the noise level at which a linter stops being
read.

### Keeping the snapshot honest

The snapshot is a copy, so it can go stale. That is paid for by a test in
celPix's own suite — `tests/test_lint_snapshot.py` — which fails the moment the
built-in registry and the snapshot disagree. When it does:

```bash
export UV_PROJECT_ENVIRONMENT=.venv-linux
uv run tools/celpix-lint/generate_snapshot.py
```

## Developing

```bash
cd tools/celpix-lint
uv run --with pytest pytest        # or: PYTHONPATH=src python -m pytest
uv run ruff check . && uv run ruff format .
```

The tests write real project files to a temp directory rather than feeding dicts
to the checks, because half of what is being tested is the reading — path
resolution, JSON tolerance, the file-size arithmetic.

The negative cases carry most of the weight: a linter over a format this
tolerant is only useful while it is quiet on correct files. Every false positive
found while building this one was a legal state read as an illegal one — a
signed `base_index`, a `slice_length` of `null`, an offset into a region joined
from several ROM chips — and each has a test so it stays fixed.

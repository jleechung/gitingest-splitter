#!/usr/bin/env python3
"""
adaptive_gitingest.py

Recursively runs gitingest on a repository, adaptively splitting big directories
into per-subdirectory digests based on a max line count, with a max recursion
depth.

Requires:
    - gitingest CLI installed and on PATH (pip install gitingest)

Example:

    python adaptive_gitingest.py my-repo \
        --digest-dir my-repo-digest \
        --max-lines 20000 \
        --max-depth 1 \
        -e node_modules \
        -e dist \
        -e .git

This will generate multiple digest-*.txt files inside my-repo-digest/, plus an
index file listing which digest corresponds to which directory.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from fnmatch import fnmatch
import uuid


def extract_local_patterns(exclude_patterns: List[str], current_dir_name: str) -> List[str]:
    """
    Extract patterns that should apply when inside a specific directory.
    
    E.g., 'datasetcard/*.txt' becomes '*.txt' when current_dir_name is 'datasetcard'
         '**/datasetcard/*.txt' becomes '*.txt' when current_dir_name is 'datasetcard'
    """
    local_patterns = []
    for pattern in exclude_patterns:
        # Split by '/' and find if current directory name is in the pattern
        parts = pattern.split('/')
        for i, part in enumerate(parts):
            # Match exact directory name or '**' wildcard
            if (part == current_dir_name or part == '**') and i < len(parts) - 1:
                # Get everything after this directory
                local_pattern = '/'.join(parts[i+1:])
                # If the part was '**', continue matching in subdirectories
                if part == '**':
                    local_patterns.append(pattern)  # Keep the full pattern
                local_patterns.append(local_pattern)
    return local_patterns


def run_gitingest(
    source: Path,
    output_path: Path,
    exclude_patterns: List[str],
    include_patterns: List[str],
    max_size: Optional[int],
    branch: Optional[str],
    gitingest_bin: str,
) -> None:
    """Run gitingest CLI for a given source directory."""
    cmd = [gitingest_bin, str(source), "-o", str(output_path)]

    if max_size is not None:
        # passed through to gitingest --max-size
        cmd += ["-s", str(max_size)]

    for pat in exclude_patterns:
        cmd += ["-e", pat]

    for pat in include_patterns:
        cmd += ["-i", pat]

    if branch:
        cmd += ["-b", branch]

    # Let errors surface to caller
    subprocess.run(cmd, check=True)


def count_lines(path: Path) -> int:
    """Return the number of lines in a text file."""
    lines = 0
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for _ in f:
            lines += 1
    return lines


def dir_is_excluded(dirname: str, exclude_patterns: List[str]) -> bool:
    """
    Decide whether a directory should be entirely skipped based on its name.

    We treat exclude patterns as simple globs applied to the directory name.
    """
    for pat in exclude_patterns:
        if fnmatch(dirname, pat) or fnmatch(dirname + "/", pat):
            return True
    return False


def digest_filename(root_name: str, rel_dir: Path) -> str:
    """
    Create a stable digest filename from the root name and a relative directory.

    Examples:
        root_name="my-repo", rel_dir="."         -> "digest-my-repo.txt"
        root_name="my-repo", rel_dir="foo/bar"   -> "digest-my-repo-foo-bar.txt"
    """
    if str(rel_dir) == ".":
        return f"digest-{root_name}.txt"
    parts = [p for p in rel_dir.parts if p not in (".", "")]
    suffix = "-".join(parts)
    return f"digest-{root_name}-{suffix}.txt"


def ingest_dir(
    dir_path: Path,
    rel_dir: Path,
    depth: int,
    *,
    root_dir: Path,
    root_name: str,
    digest_dir: Path,
    max_lines: int,
    max_depth: int,
    exclude_patterns: List[str],
    include_patterns: List[str],
    max_size: Optional[int],
    branch: Optional[str],
    gitingest_bin: str,
    digests_index: List[Dict[str, Any]],
) -> None:
    """
    Core recursive function that:
      1. Ingests dir_path as a whole to a temporary file.
      2. If small enough OR at max depth => keep single digest.
      3. Else => delete temporary and split into:
         - one digest for local files only
         - one digest per immediate subdirectory (recurse)
    """
    # 1) First, try ingesting the whole directory to a temporary file
    tmp_name = f".tmp-{root_name}-{uuid.uuid4().hex}.txt"
    tmp_path = digest_dir / tmp_name

    print(f"[depth={depth}] Analyzing {dir_path} as a whole...")
    run_gitingest(
        source=dir_path,
        output_path=tmp_path,
        exclude_patterns=exclude_patterns,
        include_patterns=include_patterns,
        max_size=max_size,
        branch=branch,
        gitingest_bin=gitingest_bin,
    )

    total_lines = count_lines(tmp_path)
    print(f"  -> {total_lines} lines")

    # If small enough OR at max depth, keep this digest as-is
    if total_lines <= max_lines or depth >= max_depth:
        final_name = digest_filename(root_name, rel_dir)
        final_path = digest_dir / final_name
        tmp_path.replace(final_path)

        digests_index.append(
            {
                "rel_dir": "." if str(rel_dir) == "." else str(rel_dir),
                "digest_file": final_name,
                "line_count": total_lines,
                "depth": depth,
                "split": False,
            }
        )
        print(f"  -> Keeping whole-dir digest: {final_name}")
        return

    # 2) Too big AND we are allowed to split (depth < max_depth)
    print(f"  -> Too big and depth < max_depth, splitting into subdirectories...")
    tmp_path.unlink(missing_ok=True)

    # Prepare local-files-only digest for this directory
    child_dirs = [p for p in dir_path.iterdir() if p.is_dir()]

    # Exclude all child directories for the "local files" digest
    local_excludes = list(exclude_patterns)

    # Add directory-specific exclude patterns
    extra_patterns = extract_local_patterns(exclude_patterns, dir_path.name)
    local_excludes.extend(extra_patterns)

    for child in child_dirs:
        # Use a glob that excludes everything under that child directory
        # e.g. "datasets/**", "nn/**", etc.
        local_excludes.append(f"{child.name}/**")

    # Run gitingest again, this time for local files only
    local_tmp_name = f".tmp-local-{root_name}-{uuid.uuid4().hex}.txt"
    local_tmp_path = digest_dir / local_tmp_name

    print(f"  -> Generating digest for local files in {dir_path} (excluding subdirs)...")
    run_gitingest(
        source=dir_path,
        output_path=local_tmp_path,
        exclude_patterns=local_excludes,
        include_patterns=include_patterns,
        max_size=max_size,
        branch=branch,
        gitingest_bin=gitingest_bin,
    )

    local_lines = count_lines(local_tmp_path)
    final_name = digest_filename(root_name, rel_dir)
    final_path = digest_dir / final_name
    local_tmp_path.replace(final_path)

    digests_index.append(
        {
            "rel_dir": "." if str(rel_dir) == "." else str(rel_dir),
            "digest_file": final_name,
            "line_count": local_lines,
            "depth": depth,
            "split": True,  # this directory was split into subdirs
        }
    )
    print(f"  -> Created local-files digest: {final_name} ({local_lines} lines)")

    # 3) Recurse into child directories
    for child in sorted(child_dirs, key=lambda p: p.name):
        if dir_is_excluded(child.name, exclude_patterns):
            print(f"  -> Skipping excluded directory: {child.name}")
            continue

        child_rel = rel_dir / child.name if str(rel_dir) != "." else Path(child.name)
        ingest_dir(
            dir_path=child,
            rel_dir=child_rel,
            depth=depth + 1,
            root_dir=root_dir,
            root_name=root_name,
            digest_dir=digest_dir,
            max_lines=max_lines,
            max_depth=max_depth,
            exclude_patterns=exclude_patterns,
            include_patterns=include_patterns,
            max_size=max_size,
            branch=branch,
            gitingest_bin=gitingest_bin,
            digests_index=digests_index,
        )


def write_index_file(
    root_dir: Path,
    root_name: str,
    digest_dir: Path,
    max_lines: int,
    max_depth: int,
    digests_index: List[Dict[str, Any]],
) -> None:
    """Write a simple index/overview file listing all generated digests."""
    index_name = f"digest-{root_name}-index.txt"
    index_path = digest_dir / index_name

    lines: List[str] = []
    lines.append(f"Digest index for repository: {root_dir}")
    lines.append("")
    lines.append(f"Max lines per digest: {max_lines}")
    lines.append(f"Max recursion depth: {max_depth}")
    lines.append("")
    lines.append("Generated digests:")
    lines.append("")

    for entry in sorted(digests_index, key=lambda e: (e["depth"], e["rel_dir"])):
        rel_dir = entry["rel_dir"]
        digest_file = entry["digest_file"]
        line_count = entry["line_count"]
        depth = entry["depth"]
        split = entry["split"]
        split_note = " (split into subdirs)" if split else ""
        lines.append(
            f"- depth={depth}  dir={rel_dir:<30} -> {digest_file}  "
            f"({line_count} lines){split_note}"
        )

    lines.append("")
    lines.append("Note: Each digest file is produced by gitingest for that directory.")
    lines.append("Directories marked '(split into subdirs)' also have digests for each")
    lines.append("of their immediate subdirectories, subject to the configured depth.")

    index_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote digest index: {index_path}")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Adaptively split gitingest digests by directory size and depth.",
    )
    parser.add_argument(
        "root",
        help="Root directory of the repository (e.g. path/to/my-repo)",
    )
    parser.add_argument(
        "--digest-dir",
        help="Directory to place all digest files (default: <root>-digest)",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=20000,
        help="Maximum lines allowed in a single digest before splitting (default: 20000)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=1,
        help="Maximum directory depth (relative to root) to split recursively (default: 1)",
    )
    parser.add_argument(
        "-e",
        "--exclude-pattern",
        action="append",
        default=[],
        help="Pattern to exclude (passed to gitingest -e / --exclude-pattern). "
             "Can be used multiple times.",
    )
    parser.add_argument(
        "-i",
        "--include-pattern",
        action="append",
        default=[],
        help="Pattern to include (passed to gitingest -i / --include-pattern). "
             "Can be used multiple times.",
    )
    parser.add_argument(
        "-s",
        "--max-size",
        type=int,
        default=None,
        help="Maximum file size to process in bytes (passed to gitingest -s / --max-size).",
    )
    parser.add_argument(
        "-b",
        "--branch",
        type=str,
        default=None,
        help="Branch to ingest (passed to gitingest -b / --branch).",
    )
    parser.add_argument(
        "--gitingest-bin",
        type=str,
        default="gitingest",
        help='Name or path of the gitingest executable (default: "gitingest").',
    )
    return parser.parse_args(argv)


def main(argv: List[str] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    args = parse_args(argv)

    root_dir = Path(args.root).resolve()
    if not root_dir.is_dir():
        print(f"Error: root directory does not exist: {root_dir}", file=sys.stderr)
        sys.exit(1)

    root_name = root_dir.name
    digest_dir = (
        Path(args.digest_dir).resolve()
        if args.digest_dir
        else (root_dir.parent / f"{root_name}-digest").resolve()
    )
    digest_dir.mkdir(parents=True, exist_ok=True)

    print(f"Root directory: {root_dir}")
    print(f"Digest directory: {digest_dir}")
    print(f"Max lines per digest: {args.max_lines}")
    print(f"Max depth: {args.max_depth}")
    print(f"Global exclude patterns: {args.exclude_pattern}")
    print(f"Global include patterns: {args.include_pattern}")
    print()

    digests_index: List[Dict[str, Any]] = []

    try:
        ingest_dir(
            dir_path=root_dir,
            rel_dir=Path("."),
            depth=0,
            root_dir=root_dir,
            root_name=root_name,
            digest_dir=digest_dir,
            max_lines=args.max_lines,
            max_depth=args.max_depth,
            exclude_patterns=args.exclude_pattern,
            include_patterns=args.include_pattern,
            max_size=args.max_size,
            branch=args.branch,
            gitingest_bin=args.gitingest_bin,
            digests_index=digests_index,
        )
    except FileNotFoundError as e:
        print(
            f"Error: {e}. Is 'gitingest' installed and on your PATH?\n"
            "Install it with: pip install gitingest",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error running gitingest: {e}", file=sys.stderr)
        sys.exit(e.returncode)

    write_index_file(
        root_dir=root_dir,
        root_name=root_name,
        digest_dir=digest_dir,
        max_lines=args.max_lines,
        max_depth=args.max_depth,
        digests_index=digests_index,
    )


if __name__ == "__main__":
    main()

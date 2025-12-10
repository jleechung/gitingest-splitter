
# gitingest-splitter

Adaptive multi-file splitter for [`gitingest`](https://github.com/coderamp-labs/gitingest).

It runs `gitingest` on a repo, and whenever a directory’s digest would be too large
(over a configurable line limit), it splits that directory into:

- one digest for the directory’s **local files only**
- one digest per **immediate subdirectory**

It also respects a max recursion depth so you can avoid splitting too deep.

## Installation

From a cloned repo:

```bash
pip install .
````

## Usage

```bash
adaptive-gitingest path/to/repo \
  --digest-dir path/to/repo-digest \
  --max-lines 20000 \
  --max-depth 1 \
  -e node_modules \
  -e dist \
  -e .git
```

This produces multiple `digest-*.txt` files in `repo-digest/` plus an index file
(`digest-<repo-name>-index.txt`) listing which digest corresponds to which directory.

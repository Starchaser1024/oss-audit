Fixes #7461

## Why

`LocalSandbox.write_file` translates every container path inside the bytes it stores into the host path behind the mount, and `read_file` reverses that translation for the files it wrote (`local_sandbox.py:1222`, gated on `_agent_written_paths`). The forward half exists so a script the agent writes can run under a host interpreter — the example in #1935 is `open("/mnt/user-data/outputs/out.txt")` inside a written `.py` file — but nothing scopes it to content that will actually be executed.

The reproduction in #7461 dictates three exact lines into `/mnt/user-data/outputs/README.md` and asks the agent to read the file back. On `main` the stored file holds the host mount spelling (`/workspace/project/users/default/threads/<thread-id>/user-data/outputs/results.csv` on Linux or `<host-user-dir>/.../user-data/outputs/results.csv` on Windows), while `read_file` returns `/mnt/user-data/outputs/results.csv`; the agent then reports that the file contains the exact dictated lines. Because the stored file is the deliverable that the user downloads, the virtual path must be preserved byte-for-byte in non-executable documents.

## What changed

Container-path translation in file content is now limited to executable content, classified with `deerflow.skills.package_files.is_code_file` (`backend/packages/harness/deerflow/skills/package_files.py:54`) — the shared rule that the skill installer, SkillScan, and `skill_manage_tool` already use to separate code from static support files.

- `local_sandbox.py` adds `_content_path_translation_applies` (including `.awk` and non-blocking `O_NONBLOCK` shebang inspection for extensionless append targets) before calling `_resolve_paths_in_content`. Documents, data files, and configs are written byte-for-byte as given.
- Executable content preserves #1935 behavior: `.py`/`.sh`/`.awk` and extensionless `#!` scripts continue to receive host path translation on write and reverse translation on read.

## Bug fix verification

- Test path: `backend/tests/test_local_sandbox_content_path_translation.py` covers document suffixes (`README.md`, `notes.txt`, `data.csv`, `settings.json`, `export.yaml`), extensionless `README`, FIFO/append classification, `.awk` scripts, `.py`/`.sh`, and shebang-only `run`.
- RED on unmodified `main` (`f41670016`): **7 failed, 1 skipped, 11 deselected** (assertion mismatches on stored bytes).
- GREEN on fix (`d0c5170dd`): **21 passed** (POSIX) / **19 passed, 2 skipped** (Windows); adjacent `-k local_sandbox` **281 passed**; Ruff check + format clean.

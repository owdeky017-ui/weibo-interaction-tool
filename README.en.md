# Weibo Interaction Query Tool

**English** · [中文](./README.md)

A complete development project, built from zero all the way to a packaged distribution.
The full arc:

| Stage | When | What happened |
|---|---|---|
| Origin | 2026-08-15 | Wanted to quantify one Weibo blogger's writing habits → plain `urllib` against the mobile API |
| Iteration | same day | Paging through the API was unreliable → Selenium driving system Edge → then Playwright, injecting the paging loop into the page context, which finally yielded 1,461 posts |
| Analysis | same day | `analyze.py`: emoji, filler words, punctuation habits, sentence patterns, posting-time distribution |
| Pivot | later | From "what one person posted" to "what happened between two people": reposts, comments, comment replies, likes |
| Product | later | tkinter GUI + QR login + one-click export, packaged with PyInstaller as a portable distribution |
| Provenance | this month | The source was lost and only the packaged artefact remained → read the bytecode out of the PYZ and reconstructed 9 modules |
| Engineering | this month | Rebuilt into a complete source project: performance, credential encryption, three-layer verification, constraint funnelled into a compile-time constant (this repo) |

This repository is the current form of that line: fetching, analysis, UI, packaging and
verification all live here. Clone it to keep developing, or to rebuild the distribution
yourself. The earlier PyInstaller distribution is kept on a local disk as a reference and was
never modified. MD5 checksums were verified.

The deliverable is a Windows desktop tool. After scanning a QR code to log in, it queries all
interactions between the currently logged-in account and one other Weibo user (reposts,
comments, comment replies, likes) and exports them as Excel, CSV, and a self-contained HTML log.

User A is locked to the account you scanned in with, and the input box is read-only. You only
fill in User B. That constraint is deliberate; see §6.

> Don't want to install Python? A ready-to-run Windows build is attached to
> [Releases](https://github.com/owdeky017-ui/weibo-interaction-tool/releases/latest).
> Download the zip, unzip it, and double-click `WeiboInteractionQuery.exe`.
> No Python and no dependency installs required. For the source, or to build it
> yourself, see §8.

---

## 1. Performance

### 1.1 Early exit on match (reposts / likes)

The early implementation paged through everything (up to 30 pages × 20 items for reposts,
18 pages × 50 for likes) before checking whether the target user appeared anywhere in the
result. It now checks as it pages and returns on the page where the target is found.

Measured (`smoke_test.py`, test 2):

- Reposts, target on page 1 → requests 30 → 1
- Likes, target on page 2 → requests 18 → 2
- When the target does not exist, it still pages through everything. Completeness is unchanged.

> Also fixed: the `start_ts` parameter on `fetch_reposts` / `fetch_attitudes` was never
> referenced by the function body (a dead parameter). Removed and replaced with a
> `target_uid` that actually does something.

### 1.2 Comments no longer scanned twice for no reason

The early version walked both the chronological (`flow=1`) and the popularity (`flow=0`)
ordering, up to 10 pages each. If the chronological pass already retrieved all `total_number`
comments the API declared, both orderings must have seen the same set, so the popularity pass
is skipped. It is only re-run when the chronological pass was truncated by the page cap,
returned fewer than `total_number`, or the API did not report `total_number` at all. Coverage
is never reduced.

### 1.3 Token-bucket rate limiting (replacing a fixed `sleep`)

The early version slept `min_interval` before every request, serialising everything into a
straight line even when concurrency was available. Now a token bucket: the average QPS is
identical (1.2 / 0.6 / 0.25 s per request), but short bursts within the budget overlap the
waiting of concurrent requests. Measured average QPS does not exceed the configured rate.

### 1.4 Concurrent scanning (I/O-bound; no extra requests)

Each Weibo post needs four independent datasets: reposts, comments, likes, and pending-review
comments. These were strictly sequential. They now go out on a thread pool. Worker threads
perform network I/O only and never touch shared state; results are handled back on the main
thread. Concurrency is controlled by `config.SPEED_WORKERS`:

| Speed | Request interval | Worker threads |
|---|---|---|
| Slow (safest) | 1.2 s | 1 (strictly serial) |
| Medium | 0.6 s | 2 |
| Fast (risk of throttling) | 0.25 s | 3 |

Measured: 3 tasks concurrently in 303 ms, against 900 ms serial. `RiskControlError` is collected
and re-raised on the calling thread, so the outer "wait 180 s then retry" semantics still hold.

### 1.5 Checkpoints moved to incremental SQLite writes

The early version kept the whole checkpoint in one JSON file, so every save was
"read everything → merge in memory by `_key` → write everything", at O(number of records).
A single checkpoint file had already reached 665 KB.

Now SQLite (`checkpoint.py`):

- Deduplication is handled by `INSERT OR IGNORE` on the primary key; no in-memory merge
- A save only writes records not yet persisted, at O(new records)
- No more `os.replace` for atomicity; a power cut can no longer corrupt the whole checkpoint
- An existing `.json` checkpoint with the same name is imported automatically on first use.
  No history is lost, and the original file is left untouched.

### 1.6 pandas / numpy removed

Excel and CSV export now use `openpyxl` plus the standard-library `csv` module.
Distribution size dropped by roughly 19 MB (pandas 13 MB + numpy 6 MB), with faster cold start.

> Related finding: the 120-character truncation of post text in the early version was dead code.
> The truncated copy only fed a summary sheet and never the body, while Excel/CSV wrote the full
> record. Behaviour is now consistent: full text is kept.

---

## 2. Robustness, maintainability, security

- Credentials are now encrypted with Windows DPAPI. The early version stored the login state,
  including `SUB` / `SUBP` cookies, in plaintext at `data/cookies.json`. Anyone who could read
  that file could impersonate the account. It is now encrypted with `CryptProtectData`, with
  additional entropy bound to this application. The ciphertext is bound to the current Windows
  user and machine, so it cannot be decrypted elsewhere. Existing plaintext files are detected
  and upgraded in place, transparently to the user. Byte-by-byte tampering across the payload
  region was verified to be detected.
- Checkpoint save failures are no longer silent. `_save_checkpoint` was
  `except Exception: pass`, so a failed write was invisible and users believed their progress
  was saved when it was not. It now reports the failure explicitly.
- The two A/B scan loops were extracted into one method, `_scan_batch`. `run()` previously held
  two copy-pasted blocks.
- `pause_event` / `stop_event` are now initialised in `__init__`. They were only assigned inside
  `run()`, which made `_scan_weibo` / `_scan_comments` impossible to call standalone
  (unit tests hit `AttributeError`).
- Account-consistency check on re-login. If a re-login uses a different account, the program
  aborts with a clear message. Otherwise "User A" changes identity while the checkpoint's
  records and `scanned_mids` still refer to the old user pair, silently mixing two people's
  records.

---

## 3. How the "User A is locked" rule is implemented

"User A must be the account you scanned in with" is a hard product constraint. A single
compile-time constant holds it: `build_mode.A_MODE`, which `build.py` writes into
`build_mode.py` before compiling. The constraint therefore has exactly one decision point, so a
change to it cannot miss a branch.

| Branch point | Behaviour |
|---|---|
| GUI user-section heading | "2. Select the user to compare against" |
| User A input box | Read-only, auto-filled after login |
| GUI config memory | Only B is remembered (A always comes from the login) |
| GUI `_start` validation | Validates B only, and B cannot be the logged-in account itself |
| CLI `--u1` | Hidden (`argparse.SUPPRESS`); passing it raises a clear error |
| CLI interactive prompts | Asks for B only |
| Logged-in account uid | Is User A by definition (never empty) |
| Re-login after session expiry | Verifies account consistency; aborts if it differs |

The last row matters for data correctness. Under the "User A = logged-in account" rule,
re-scanning with a different account changes A, so the checkpoint history and `scanned_mids` no
longer describe the same user pair, and continuing would interleave two people's records. So
the program aborts instead of resuming.

Funnelling this into a constant also buys testability: tests can replace `build_mode`
in-process and re-import the modules to exercise every value it can take. They build the real
GUI and read the actual widget state (`smoke_test.py` test 7, see §4).

---

## 4. Verification: three layers

Correctness of the packaged artefact is verified at three layers, each answering a different
question.

### Layer 1: source behaviour (`smoke_test.py`, 119 assertions)

```powershell
python smoke_test.py
```

| Test | Coverage |
|---|---|
| 1 | Excel / CSV / HTML output after removing pandas (sheet order, headers, long text not truncated, empty-record branch, summary pivot) |
| 2 | Early exit on match for reposts / likes: page count on hit; no early exit when the target is absent |
| 3 | Skip conditions for the dual comment ordering (`has_more` + `total_number`, three scenarios) |
| 4 | Incremental SQLite checkpoints: `.json` → `.db` rewrite, incremental append, primary-key dedup, `_key=None` not deduplicated, legacy JSON migration, corrupt-file tolerance |
| 5 | Token bucket: burst budget, average rate, configured QPS not exceeded |
| 6 | `_run_tasks` concurrency, single-task serial path, `RiskControlError` raised on the calling thread |
| 7 | Constraint branches: CLI argument parsing plus building the real GUI and reading widget state (`importlib.reload` to swap `build_mode`) |
| 8 | Credential encryption: round-trip, byte-level tamper detection, in-place plaintext upgrade, readable errors on decryption failure, plaintext fallback when DPAPI is unavailable |

There is also a `pytest` suite (`tests/`, 210 cases) covering edge cases across client,
analyzer, exporter, checkpoint, login, fetchers and utils, run with coverage in CI.

### Layer 2: packaged artefact structure (`build.py`)

Runs automatically on every build, printing `[OK]` / `[missing]` / `[error]`:

- Structure complete (`exe` / `_internal/` / `data/checkpoints/` / `data/output/` / usage notes)
- Reads the `build_mode` compile-time constant directly out of the PYZ embedded in the exe,
  confirming it matches the intended value
- `data/` carries no runtime data (your login state never ends up in a distributed build)
- The produced exe's SHA-256 matches the expected value

### Layer 3: frozen runtime (`python build.py --probe`)

Run standalone, or automatically after a build (skip with `--skip-probe`):

```powershell
python build.py --probe
```

This compiles `probe_frozen.py` into a console exe using the same PyInstaller options as
`gui_app` and runs it, answering "do these actually work once packaged?". PyInstaller can only
prove that files are present, not that the runtime works:

```
[OK  ] import every dependency
        —— requests 2.34.2 / openpyxl 3.1.5 / sqlite3 3.50.4 / tk 8.6 / PIL 12.3.0
[OK  ] HTTPS request + CA certificate chain
        —— HTTP 302, 419 bytes (SSL verification passed)
[OK  ] SQLite checkpoint read/write
[OK  ] Excel / CSV / HTML export
[OK  ] tkinter + PIL.ImageTk
[OK  ] frozen path resolution (BASE_DIR = exe directory)
6 passed / 0 failed
```

> This layer exists specifically to catch the class of problems that only appear when you
> actually run the thing: a missing `cacert.pem` in the certificate chain, a wrong `sqlite3.dll`
> version, `PIL.ImageTk` failing to find `_imaging`.

Two standalone scripts are available for re-checking at any time:

```powershell
python verify_package.py     # reads the build_mode constant out of the PYZ in a packaged exe
#   [OK] scan-login build: build_mode = 'self' (expected 'self')

python verify_assemble.py    # reuses compiled artefacts from .build to redo assemble + self-check
#   [OK] embedded mode = 'self' (expected 'self')
#   [OK] usage notes match the target definition
#   [OK] data/ contents = ['checkpoints', 'output']
```

---

## 5. Things that bit me

All of these are "looked fine when written, broke at runtime" problems. Worth recording.

### 5.1 A second PyInstaller build fails: `SAFE_DELETE_BULK_CONFIRM_REQUIRED`

`build.py` ran fine the first time and errored out the second. Under `--noconfirm`,
PyInstaller deletes the existing `dist/<name>` first, and that directory contains thousands of
files, which trips a "bulk delete needs confirmation" guard.

The fix was to remove the delete step. Each build writes to a fresh `dist_<timestamp>`
directory and moves the result into place afterwards. `--clean` also changed from default to
opt-in. Clearing the cache only makes the next build slower, so it should never have been the
default.

### 5.2 Garbled non-ASCII output in the frozen runtime

`probe_frozen.py` printed mojibake once compiled to an exe. The cause was not PyInstaller:
under a frozen runtime, stdout uses the system locale encoding (GBK on a Chinese Windows
install), while the caller read it as UTF-8. Fixed by forcing
`sys.stdout.reconfigure(encoding="utf-8")` at startup. This only shows up when a parent process
reads a child's stdout. Running the script locally looks perfectly fine.

### 5.3 Not seeing `urllib3` in `_internal/` does not mean it was not bundled

On first inspecting the build output I could not find `urllib3`, `idna`, or `PIL/ImageTk.py`
under `_internal/`, and concluded dependencies were missing.

They were not. Pure-Python modules are compiled into the PYZ embedded in the exe (729 modules
in this case). Only packages with `.pyd` extensions land in `_internal/` as directories. To
confirm completeness you have to open the PYZ and count modules; looking at the directory is
misleading.

The same inspection confirmed `pandas` / `numpy` were genuinely excluded, which was the point
of the dependency-removal work.

### 5.4 DPAPI's integrity protection has a boundary

After switching credentials to Windows DPAPI I ran a byte-by-byte tamper test: all 274 bytes of
the payload region were detected. But bytes 4 to 19 of a DPAPI blob are not integrity-protected.
Those are the `dwFlags` field plus a 16-byte description, a documented property of the API that
does not touch the ciphertext. Without measuring it, it is easy to assume the whole blob is
tamper-proof and build a threat model on a false premise.

### 5.5 "The packaged exe starts" is not "the packaged exe works"

The first round of verification only established that the exe survived 12 seconds without
crashing. That covers the startup path and nothing further. A missing `cacert.pem` in the
certificate chain, a wrong `sqlite3.dll` version, or `PIL.ImageTk` failing to find `_imaging`
all surface only when you actually run it. That is precisely why layer 3 in §4 exists.

### 5.6 CI had never actually run on GitHub, and went red twice on its first real run

The repository was initialised without committing `models.py`, `tests/`, or
`.github/workflows/ci.yml`. A fresh clone had neither a CI definition nor a runnable pytest
suite.

Once those were added, CI executed for the first time and immediately exposed two problems:

1. `ruff check .` reported 87 issues in `smoke_test.py` (missing signature annotations, `open()`
   without `with`, list concatenation instead of unpacking, over-long lines).
2. A subtler one: `mypy` passed on my Windows machine and failed on the Linux CI runner.
   `login.py` uses `ctypes.windll` and `gui_app.py` uses `os.startfile`; those symbols do not
   exist in Linux typeshed, and code after `if sys.platform != "win32"` is judged unreachable
   from Linux. `mypy` picks its type stubs from the host it runs on, so one codebase gave two
   verdicts on two machines. The fix is to pin the target platform explicitly in
   `pyproject.toml` (`platform = "win32"`), which makes the result independent of the host.

The right test is "can a fresh clone build and run this".

---

## 6. Security and scope: what this tool does not do

Locking User A to the logged-in account in the scan-login build is a deliberate design
constraint:

- It can only ever query interactions that involve you. Querying two accounts you have no
  relationship to is not possible.
- Captured data never leaves the machine: no upload, no server, no account system.
- Credentials are encrypted at rest under `data/`, and that directory is excluded via `.gitignore`.

Why it is not a hosted service. Turning "scan to log in, then scrape" into a public website
is entirely feasible technically, but it would require a server to store every visitor's Weibo
session (functionally indistinguishable from a phishing site), it violates Weibo's terms of
service, and offering "look up the interactions between person X and person Y" publicly is a
privacy problem in itself. So this stays a local desktop tool.

---

## 7. Project site

`site/` is a static site explaining the architecture, the measured performance numbers, and the
verification method. It has zero external resources and can be opened offline by
double-clicking.

```powershell
python make_sample_report.py      # regenerate the sample report
python -m http.server 8766 --directory site    # local preview
```

`site/sample/sample-report.html` is generated by the real `exporter.export()`. Only the input is
synthetic (52 records spanning 7 months, covering every interaction type and both directions).
Its layout, filtering, and statistics behave exactly like a user's own export.

---

## 8. Environment and usage

Dependencies are grouped by purpose (`optional-dependencies` in `pyproject.toml`):

| Group | Contents | Purpose |
|---|---|---|
| core | `requests` / `openpyxl` | scraping and export |
| `gui` | `Pillow` | rendering the login QR code in the window |
| `dev` | `ruff` / `mypy` / `pytest` / `pytest-cov` | static checks and tests |
| `build` | `pyinstaller` | packaging |

```powershell
pip install -e ".[dev,gui]"     # development environment
pip install -e ".[build]"       # packaging environment (use a separate venv if you like)
```

Running from source (behaviour is determined by the current value in `build_mode.py`):

```powershell
python gui_app.py                        # graphical interface
python main.py --u2 <user B> --days 30   # command line
```

Tests and static checks:

```powershell
python smoke_test.py    # 119 assertions: export / pruning / checkpoints / rate limiting /
                        # concurrency / constraint branches / encryption
pytest                  # 210 cases (tests/)
ruff check .            # lint
ruff format --check .   # formatting
mypy                    # type check (targets listed under [tool.mypy] files in pyproject.toml)
```

Packaging and artefact re-verification:

```powershell
python build.py               # build + assemble + three-layer self-check
                              # (PyInstaller cache is reused by default)
python build.py --mode self   # build the self mode only
python build.py --clean       # drop the analysis cache and rebuild
python build.py --probe       # frozen-runtime self-check only, no packaging
python verify_package.py      # read the build_mode constant out of the embedded PYZ
                              # (requires the packaging environment)
```

Machine-specific directories used by `build.py` (packaging interpreter, reference directory,
output directories) are not hardcoded. They resolve in three steps: environment variable, then
`build.local.json`, then a portable default.

| Environment variable | Config key | Default |
|---|---|---|
| `WEIBO_PACK_PY` | `pack_python` | auto-detect `venv-pack/`, else the current interpreter |
| `WEIBO_REFERENCE_DIR` | `reference_dir` | empty (skip the structure comparison) |
| `WEIBO_OUT_<MODE>` | `out.<mode>` | `<project>/dist/<mode>` |

Copy `build.local.example.json` to `build.local.json` to override; the latter is
git-ignored. To define extra build targets locally, add `modes.<name>` to that file
(`label` / `desc` / `out` / `readme`); `--mode` picks them up automatically.

> When Tk cannot start (no graphical session, tcl resources unreadable), `smoke_test.py`
> skips the GUI section and prints `[SKIP]`; the environment problem is not counted as a
> failure, matching the semantics of the `tk_root` fixture in `tests/`.
> `requirements.txt` lists the packaging environment's runtime dependencies, matching
> "core + `gui`" above.

---

## 9. Layout

```
weibo-interaction-opt/
├── README.md         # Chinese README
├── README.en.md      # this file
├── LICENSE           # MIT
├── build.py          # one-shot build (write A_MODE → PyInstaller → assemble → three-layer check)
├── build.local.example.json  # template for local path overrides (copy to build.local.json, git-ignored)
├── build_mode.py     # compile-time constant A_MODE (written by build.py; do not edit by hand)
├── probe_frozen.py   # frozen-runtime self-check (compiled and run by build.py --probe)
├── verify_package.py # re-reads the embedded build_mode value from a packaged directory
├── verify_assemble.py# reuses compiled artefacts to redo the assemble + self-check path
├── smoke_test.py     # smoke test (119 assertions, single-file linear script)
├── tests/            # pytest suite (210 cases)
│   ├── conftest.py
│   ├── _support.py
│   └── test_*.py     # client / analyzer / exporter / checkpoint / login / fetchers / utils / build / build_modes
├── .github/workflows/ci.yml   # CI: ruff + mypy (Linux) · pytest + coverage (Windows)
├── gui_app.py        # GUI entry point
├── main.py           # CLI entry point
├── login.py          # QR login + DPAPI-encrypted persistence + manual cookies
├── client.py         # API client (token-bucket rate limiting / per-thread Session / endpoints)
├── fetchers.py       # Weibo item parsing, @-mention extraction
├── analyzer.py       # interaction detection and aggregation (concurrent scan + resume)
├── checkpoint.py     # checkpoint storage (incremental SQLite + legacy JSON migration)
├── exporter.py       # Excel/CSV/HTML export (openpyxl + csv, no pandas)
├── models.py         # data structures (WeiboItem / CommentItem / InteractionRecord, ...)
├── server.py         # local log-refresh server (optional, not bundled into the exe)
├── utils.py          # Weibo timestamp parsing
├── config.py         # configuration
├── 使用说明.txt       # usage notes for the source tree (regenerated by build.py for each target)
├── make_sample_report.py  # generates the showcase sample report (real exporter, synthetic input)
├── site/             # project showcase site (static, zero external resources)
│   ├── index.html
│   └── sample/sample-report.html
├── .build/           # PyInstaller intermediates (deletable; removing them slows the next build)
└── data/             # cookies, checkpoints, output (created at runtime; not committed)
```

---

## 10. Notes and limitations

1. Likes. Weibo's web frontend exposes no public endpoint for a post's like list. The tool
   falls back to the mobile API and tries its best; when the endpoint is unavailable it pauses
   that scan and retries every 50 posts.
2. @-mentions. Parsed from post text and `@` links, matched by exact uid with nickname
   normalisation as a fallback.
3. Nested comment replies. Replies under each comment are scanned by default; this gets
   noticeably slower on heavily commented posts and can be disabled with `--no-replies`.
4. Rate limiting. Weibo throttles unauthenticated, high-frequency requests. On detection the
   tool waits 180 seconds and retries; if it triggers repeatedly, drop to the "Slow" setting.
5. Visibility. Only public posts and their public interactions are reachable. Private
   accounts and deleted content cannot be retrieved.
6. All timestamps are Beijing time (UTC+8).

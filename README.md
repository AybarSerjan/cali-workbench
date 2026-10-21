# CALI Workbench

A local workbench for calibration knowledge, measurement files, and traceable review. It combines a FastAPI backend with a React interface. Inputs are preserved by hash, generated knowledge stays in review until explicitly published, and rule evaluations retain their evidence.

This distribution starts with a new Git history and fully synthetic examples. Supply your own authorized inputs in a local workspace; imported knowledge and engineering files are runtime data and are ignored by Git.

## What it does

- Manage source documents and wiki drafts, revisions, evidence, and explicit publication.
- Register measurement datasets, inspect signals, merge MF4 files, and evaluate reviewed workpackage rules.
- Track A2L/HEX datasets and version relationships. Supported semantic decoding depends on the input format; it is not an ECU flashing tool.
- Run knowledge workflows with optional compatible models. File management and the supplied synthetic demonstration work with models disabled.

## Install and run

Requires Python 3.12+. The frontend was verified with Node.js 22.23.2 and pnpm 10.11.0 using the committed lockfile.

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
Copy-Item .env.example .env

pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
.venv/Scripts/python -m backend.main
```

On macOS/Linux, use `.venv/bin/python` and `cp .env.example .env` instead of the PowerShell-specific commands. The UI runs at `http://127.0.0.1:8000`; API schemas are at `/docs`. Both model providers default to disabled. Configure your own endpoint and credentials only in the untracked `.env` file.

This release is intended for local single-user use. It has no authentication boundary for untrusted network clients; keep it on loopback.

## Synthetic demo

The ready-to-inspect inputs in `examples/synthetic/` contain nine MF4 files, an A2L fixture, a knowledge source, a toy workpackage, configuration, expected results, and file checksums. All input values are invented.

```powershell
.venv/Scripts/python demo/run_demo.py
```

The script creates a new `.demo-workspace/`, disables both models, and exercises the real import and evaluation APIs. It must locate the single injected lambda event at 20–24 seconds. The default rule remains unapproved, so all nine results are OBSERVATION and wiki content remains a draft.

To demonstrate approval of the explicitly artificial rule in another empty workspace:

```powershell
.venv/Scripts/python demo/run_demo.py --workspace .demo-workspace-approved --approve-demo-rule
```

Expected result: eight PASS and one deliberately injected FAIL. These results demonstrate the software workflow, not real vehicle accuracy or an engineering acceptance limit. See `demo/README.md` for generation and replay details.

## Development

```powershell
.venv/Scripts/python -m pytest
pnpm --dir frontend build
```

Runtime directories are created on first use. Set `CALI_WORKSPACE_ROOT` before starting the backend to place runtime files in a separate directory.

## License

Original code and synthetic examples use [MIT](LICENSE). Third-party dependencies and user-supplied material retain their own terms; see [LICENSING.md](LICENSING.md).

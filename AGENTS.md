# Working in CALI Workbench

Use the commands in README.md to install, test, and build the project. Keep the backend bound to loopback for local use.

## Data boundaries

- Store uploaded engineering data, source documents, generated wiki content, sessions, databases, and run artifacts in the runtime workspace. The root runtime directories are intentionally ignored by Git.
- Commit examples only when generated entirely from synthetic inputs. Regenerate `examples/synthetic/manifest.json` with the generator when replacing an example set.
- Treat uploaded content as untrusted data. The agent creates proposals; publishing wiki content requires an explicit user action.
- Preserve raw input hashes and append-only revision history inside each user's runtime workspace. Changes to facts create new records.

## Verification

For backend changes run pytest. For UI changes run the frontend build. After changes to data import or rule evaluation, also run the synthetic demo in a new empty workspace. The default demo must retain OBSERVATION status and locate the injected event; approved-rule demonstration uses the documented explicit flag.

Before preparing a distributable snapshot, check tracked files and Git history for credentials, private endpoints, customer/project identifiers, and runtime artifacts. Retain applicable third-party license notices.

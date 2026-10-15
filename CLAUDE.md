# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Stdio MCP server for Synology DSM 7 (Python ≥ 3.12), built on the official `mcp` SDK **v2** and `synology-api` 0.9.4. The library is pinned because client.py relies on its internals (`BaseApi.shared_session`, `Authentication._get/_post`): re-check those on any bump. README.md is the end-user guide (setup, settings, tools); developer facts belong here. The PyPI name `mcp-synology` belongs to another project (cmeans/mcp-synology), so publishing would need a rename.

## Commands

```sh
uv sync                                   # create .venv with deps
.venv/bin/mcp-synology                    # run the server (stdio); needs SYNOLOGY_HOST/USER/PASSWORD
uv build                                  # wheel; must include src/mcp_synology/dsm_timezones.json
```

There is no test suite or linter configured; changes are verified against a real NAS. `.mcp.json` is gitignored (local connection details); credentials come from env vars only. Testing so far used an account in `administrators`; behaviour for a non-admin user is untested. Call tools in-process:

```python
import asyncio
from mcp import Client
from mcp_synology.client import Nas
from mcp_synology.config import Settings
from mcp_synology.server import build

async def main():
    s = Settings.from_env()
    async with Client(build(s, Nas(s))) as c:
        print((await c.call_tool("cloudsync_status", {})).structured_content)

asyncio.run(main())
```

Only make read-only DSM calls when testing against a real NAS. Don't call any tool above the read tier, or any library `set_*`/`create_*`/`task_run`/delete method, unless the NAS owner has explicitly asked: they change live state.

## Architecture

- **config.py**: `Settings.from_env()`, plus the `Tier` enum (read < write < destructive). Destructive also requires `SYNOLOGY_ALLOW_DESTRUCTIVE=1`.
- **client.py**: `Nas` owns the single DSM login.
  - synology-api shares one session across all API objects via the class attribute `BaseApi.shared_session`. `Apis` constructs them all; the first logs in.
  - `Nas.call(fn)` serialises every DSM call under a lock (MCP v2 runs sync tools in worker threads). On session errors 106/107/119 or a connection error it resets the session and retries once.
  - Raises `DsmError`, a `ToolError`. Only `ToolError` messages reach the client; other exceptions are masked as "Error executing tool".
  - Patches `Authentication._get/_post` to add HTTP timeouts, because the library sets none.
- **server.py**: `build()` registers tools through `tool(tier, annotations)`.
  - Tools above the configured tier are not registered at all.
  - Pydantic `title` keys are stripped from input schemas.
  - Each tool is a closure `run(a: Apis)` passed to `nas.call`.
  - Inside `run`, catch `SynoBaseException` only for expected DSM errors, and re-raise when `_must_propagate(e)`; otherwise re-login breaks.

### Result conventions (agent-facing; keep them consistent)

Output is shaped to minimise LLM context; the conventions are explained once in the `INSTRUCTIONS` constant in server.py (the server instructions sent to the client).
- Lists go through `_table()`: `{columns, rows}`, with values shared by every row hoisted to `all`, a common path prefix to `base`, and a shared date to `date`.
- `_compact()` drops null/false/0 values: absent means normal.
- `_page()` adds `next_offset`.

New tools should reuse these helpers rather than return raw DSM dicts.

### Adding a tool

Pick a `Tier` and annotations (`READ`/`WRITE`), register it in `build()` with `@tool(...)`, make DSM calls inside `run(a: Apis)` passed to `nas.call`, and shape output with `_table`/`_compact`/`_page` (pass `tz=clock.name` when it has times). Verify it read-only against a NAS, then add it to the README's "What it can see" table (the user-facing contract).

### Time handling

All times are shown in the NAS's own zone via `NasClock` (from `_clock(a)`, cached on the `Apis` object):
- DSM returns some times as epoch seconds (format with `clock.fmt`) and some as NAS-local strings (pass through with `clock.local`; DSM already applied DST). Never convert those strings with a fixed offset.
- The zone id comes from `SYNO.Core.Region.NTP` `get` and is mapped to an IANA name with `dsm_timezones.json`. That file was generated once, by hand, from DSM's `/usr/share/zoneinfo/Timezone/synotztable.json` (each id's `nameInTZDB`); regenerate it the same way if a DSM release changes its zone list.
- Pass `tz=clock.name` to `_table` for any table with times.

## DSM / library facts verified on DSM 7.3.2 (don't re-derive)

- Cloud Sync action codes: 0 = delete remote, 1 = download, 2 = upload, 3 = delete local.
- `connection_pause`/`connection_resume` with a negative conn_id act on **every** connection; `ConnId` is `ge=0`.
- `CoreStorage` (`SYNO.Core.Storage.*`) returns error 101; storage comes from `SysInfo.get_volume_info()`.
- `SYNO.Core.SyslogClient.Log` `list`:
  - String parameters must be JSON-quoted (`logtype='"system"'`). Log types are `system`, `connection` and `cifs` (SMB).
  - `offset`/`limit` paginate, and `date_from`/`date_to` are true epoch seconds.
  - No working level parameter found (DSM's own Log Center JS uses `level` = `info`/`warning`/`err`, unverified); level is filtered client-side over a bounded scan.
- Task Scheduler: `get_task_config` fails with 4800 for event tasks, and `get_history_log` doesn't exist (103), so run output is unavailable.
- `SYNO.Core.DSMNotify` `notify action="load"` lists notifications; parameters are JSON in `msg` regardless of `isEncoded`.

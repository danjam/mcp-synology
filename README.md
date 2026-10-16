# mcp-synology

Ask your AI assistant about your Synology NAS. This is an [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) check your NAS's health, storage, Cloud Sync progress, scheduled tasks, logs and files, in plain language.

> "Is my cloud sync finished? How long is left?"
> "Did last night's backup task succeed?"
> "Any disk or RAID warnings this month?"
> "Who failed to log in over SSH last week?"
> "What's in /media/film?"

It is **read-only by default**. Actions that change anything are switched off unless you turn them on.

## Requirements

- A Synology NAS running **DSM 7**. Developed and tested against DSM 7.3; other DSM 7 releases are untested.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which provides `uvx` and fetches Python and everything else automatically.
- A DSM user account. See [Choosing an account](#choosing-an-account).

## Setup

You need four details: your NAS's address, the DSM port, and a username and password.

The port is the one you use to open DSM in a browser: 5001 for HTTPS by default, or 5000 for HTTP. If you changed it, it's under **Control Panel → Login Portal → DSM**.

### Claude Code

```sh
claude mcp add synology \
  -e SYNOLOGY_HOST=192.168.1.10 \
  -e SYNOLOGY_PORT=5001 \
  -e SYNOLOGY_USER=your-user \
  -e SYNOLOGY_PASSWORD='your-password' \
  -- uvx --from git+https://github.com/danjam/mcp-synology mcp-synology
```

Add `-s user` to make it available in every project. Run `/mcp` inside Claude Code to check that it connected.

### Claude Code, per project (`.mcp.json`)

To share the server with everyone working in a project, put a `.mcp.json` file in the project root. Claude Code expands `${VAR}` from your environment, so credentials stay out of the file:

```json
{
  "mcpServers": {
    "synology": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/danjam/mcp-synology", "mcp-synology"],
      "env": {
        "SYNOLOGY_HOST": "192.168.1.10",
        "SYNOLOGY_PORT": "5001",
        "SYNOLOGY_PERMISSION": "read",
        "SYNOLOGY_USER": "${SYNOLOGY_USER}",
        "SYNOLOGY_PASSWORD": "${SYNOLOGY_PASSWORD}"
      }
    }
  }
}
```

Set `SYNOLOGY_USER` and `SYNOLOGY_PASSWORD` in your shell profile. Claude Code asks you to approve a project's servers the first time.

### Claude Desktop and other MCP clients

Add this to your client's MCP configuration (for Claude Desktop: **Settings → Developer → Edit Config**):

```json
{
  "mcpServers": {
    "synology": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/danjam/mcp-synology", "mcp-synology"],
      "env": {
        "SYNOLOGY_HOST": "192.168.1.10",
        "SYNOLOGY_PORT": "5001",
        "SYNOLOGY_USER": "your-user",
        "SYNOLOGY_PASSWORD": "your-password"
      }
    }
  }
}
```

Restart the client after saving.

### Self-signed certificate

If your NAS uses DSM's default self-signed certificate rather than one from a certificate authority, connecting fails with a certificate verification error. Add:

```
SYNOLOGY_VERIFY_SSL=0
```

This skips the certificate check. Only do that on a network you trust.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `SYNOLOGY_HOST` | (required) | NAS IP address or hostname |
| `SYNOLOGY_USER` | (required) | DSM username |
| `SYNOLOGY_PASSWORD` | (required) | DSM password |
| `SYNOLOGY_PORT` | `5001`, or `5000` without HTTPS | DSM port |
| `SYNOLOGY_HTTPS` | `1` | `0` to connect over plain HTTP |
| `SYNOLOGY_VERIFY_SSL` | `1` | `0` to accept a self-signed certificate |
| `SYNOLOGY_PERMISSION` | `read` | `read` or `write`; see below |

## Permissions

| Level | What the assistant can do |
|---|---|
| `read` (default) | Look only. Nothing on the NAS is changed. |
| `write` | Also pause and resume Cloud Sync connections. |

Tools above your chosen level are hidden from the assistant entirely, so it can't call them by mistake. A `destructive` level is reserved for future tools that delete data or shut the NAS down. It needs both `SYNOLOGY_PERMISSION=destructive` and `SYNOLOGY_ALLOW_DESTRUCTIVE=1`, and no tools use it yet.

## Choosing an account

- **Task Scheduler, Log Center and notifications show system-wide information**, including tasks that run as root. They have been tested only with an account in the `administrators` group. With a regular account they may fail or show less.
- **Two-factor authentication isn't supported.** An account with 2FA enabled can't log in. If you use 2FA on your own account, create a separate account for the assistant.
- A dedicated account also makes the assistant's sign-ins easy to tell apart from yours in DSM's connection log.

## What it can see

| Area | You can ask about |
|---|---|
| **System** | Model, DSM version, uptime, temperature, CPU and memory load, disk and network activity |
| **Storage** | Volume usage and free space, drive health (status, SMART, temperature) |
| **Network** | Hostname, gateway, DNS servers, and each network port's IP address, MAC, link speed and MTU |
| **Packages** | Installed packages, whether each is running, and available updates from Package Center, including third-party sources you've added such as SynoCommunity (flagging security releases) |
| **Hardware & power** | Disk hibernation timers, recent disk wake-ups, hibernation debug mode, fan mode, power schedule, restart after power loss, Wake-on-LAN and UPS |
| **Cloud Sync** | Each connection's status, files still to sync, cloud quota, sync folders, bandwidth limits and sync schedule, a time-remaining estimate, live transfers with each file's speed, and the full sync log (filter by upload, download or delete, or by file name) |
| **Task Scheduler** | Every scheduled task with its schedule, next run and last result, and the run history of any task, e.g. "did my nightly task succeed every night this week?" |
| **Log Center** | System log (hardware, disks, network, updates), connection log (sign-ins and failed logins) and SMB file-access log, filtered by severity, keyword and date range, or summarised (how many, since when, how often, per day or hour) |
| **Security** | Security Advisor's latest scan and failing checks, and installed certificates with their expiry dates |
| **Notifications** | DSM's desktop notifications, e.g. security advisories and available updates |
| **Files** | Shared folders, folder listings and file details, with sizes and dates |

Times are shown in the NAS's own time zone, with the zone named in each answer.

## Troubleshooting

| Error | Fix |
|---|---|
| `missing required env vars: ...` | Set the listed settings in your MCP client's configuration. |
| `DSM error 400: No such account or incorrect password` | Check the username and password. Repeated failures can trigger DSM's auto-block (**Control Panel → Security → Protection**). |
| `DSM error 403` / `406` (two-factor authentication) | The account uses 2FA, which isn't supported; use an account without it. |
| `DSM error 407: Blocked IP source` | DSM's auto-block has blocked your computer. Unblock it under **Control Panel → Security → Protection**. |
| `SSLCertVerificationError` | Self-signed certificate: set `SYNOLOGY_VERIFY_SSL=0`. |
| `Connection refused` or a timeout | Wrong host or port, or HTTPS/HTTP mismatch. Use the port you use in the browser, and set `SYNOLOGY_HTTPS=0` for an HTTP-only port. |

## Limitations

- Task Scheduler shows each run's result (success or exit code) but not its output. DSM 7.3 doesn't expose that through its API.
- Filtering logs by severity reads up to 20,000 entries per request, so rare entries in a very large log may take several requests to reach.
- Cloud Sync has no per-connection speed figure: DSM reports each file's speed, which doesn't add up to a reliable total. Network traffic for the whole NAS is under System.
- DSM doesn't report whether a disk is spun down right now. You can see when disks last woke up, and the hibernation settings.
- The hibernation debug log itself isn't readable through DSM's API, only whether debug mode is on.
- Only one NAS per server entry. Add another entry with a different name for a second NAS.

## Acknowledgements

Built on [synology-api](https://github.com/N4S4/synology-api) and the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

## License

MIT. See [LICENSE](LICENSE).

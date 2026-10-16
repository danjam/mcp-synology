"""MCP tools for Synology DSM.

Results are shaped for an LLM reader: lists are column/row tables, values shared by
every row are hoisted into ``all``, a common path prefix into ``base``, and normal or
empty values are omitted. The conventions are explained once, in the server instructions.
"""

from __future__ import annotations

import json
import posixpath
import re
import time
from datetime import datetime, timedelta, timezone, tzinfo
from importlib import resources
from collections.abc import Callable
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from synology_api.exceptions import SynoBaseException

from .client import SESSION_ERRORS, Apis, Nas, data
from .config import Settings, Tier

# Cloud Sync log/activity action codes (verified against DSM 7.3).
CLOUDSYNC_ACTIONS = {0: "delete remote", 1: "download", 2: "upload", 3: "delete local"}

# No rate/ETA estimate when the newest log entry is older than this: the connection is idle.
RATE_STALE_S = 900

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

INSTRUCTIONS = """\
Synology DSM at {host}, permission tier {tier}.
Results: lists are {{columns, rows}}; "all" = values shared by every row; "base" = path prefix stripped from row paths; \
times are the NAS's local time, "tz" names its zone; "date" = date shared by every row time; "next_offset" = more results, pass as offset. \
Absent fields mean normal/none/0. Sizes are decimal. Folder names end with "/".
Paths start with a shared folder (filestation_list_shares). conn_id = Cloud Sync connection (cloudsync_status); \
sess_id = one of its sync tasks."""

ConnId = Annotated[int, Field(ge=0)]


def _size(n: Any) -> str | None:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1000
    return None


# DSM time zone ids (Control Panel > Regional Options) to tz database names, from DSM's own
# /usr/share/zoneinfo/Timezone/synotztable.json (DSM 7.3).
DSM_ZONES: dict[str, str] = json.loads(resources.files(__package__).joinpath("dsm_timezones.json").read_text())
CLOCK_TTL_S = 300
NAS_TIME_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M")


class NasClock:
    """All times are shown in the NAS's own zone. DSM returns some times as epoch seconds (formatted
    here with the zone's rules, so each row gets the right DST offset) and some as NAS-local strings
    (passed through, since DSM already applied its rules)."""

    def __init__(self, zone: tzinfo, name: str) -> None:
        self.zone, self.name = zone, name

    def fmt(self, epoch: Any) -> str | None:
        if not epoch:
            return None
        return datetime.fromtimestamp(int(float(epoch)), self.zone).strftime("%Y-%m-%d %H:%M:%S")

    def _parse(self, s: Any) -> datetime | None:
        if not isinstance(s, str):
            return None
        for f in NAS_TIME_FORMATS:
            try:
                return datetime.strptime(s, f)
            except ValueError:
                continue
        return None

    def local(self, s: Any) -> str | None:
        """Normalise a NAS-local time string; unparseable values pass through unchanged."""
        d = self._parse(s)
        return d.strftime("%Y-%m-%d %H:%M:%S") if d else s

    def epoch(self, s: Any) -> int | None:
        # Wall-clock times on DST-change days are resolved with fold=0: a repeated hour (autumn) means
        # its first occurrence, a skipped hour (spring) is read with the pre-change offset. DSM's
        # strings carry no more information, so durations spanning the repeated hour can be short.
        d = self._parse(s)
        return int(d.replace(tzinfo=self.zone).timestamp()) if d else None

    def parse_when(self, s: str, end_of_day: bool = False) -> int | None:
        """"24h" / "7d" / "30m" ago, or "YYYY-MM-DD[ HH:MM[:SS]]" in the NAS's zone, to epoch seconds.
        With end_of_day, a bare date means the end of that day (for "until")."""
        s = s.strip()
        if not s:
            return None
        if s[:-1].isdigit() and s[-1] in "mhd":
            return int(time.time()) - int(s[:-1]) * {"m": 60, "h": 3600, "d": 86400}[s[-1]]
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                d = datetime.strptime(s, f).replace(tzinfo=self.zone)
            except ValueError:
                continue
            if end_of_day and f == "%Y-%m-%d":
                d += timedelta(days=1, seconds=-1)
            return int(d.timestamp())
        raise ToolError(f'cannot parse time {s!r}; use "24h", "7d", "30m" or "YYYY-MM-DD[ HH:MM]"')


def _clock(a: Apis) -> NasClock:
    """The NAS's zone, from SYNO.Core.Region.NTP. Cached per Apis (reset on re-login, used only under
    Nas' lock) for CLOCK_TTL_S, so a zone change in DSM shows up within minutes."""
    cache = a.__dict__.setdefault("clock", {})
    if time.time() - cache.get("at", 0) > CLOCK_TTL_S:
        info = a.sysinfo.gen_list.get("SYNO.Core.Region.NTP") or {"path": "entry.cgi", "maxVersion": 1}
        try:
            r = data(a.sysinfo.request_data("SYNO.Core.Region.NTP", info["path"],
                                            {"version": info["maxVersion"], "method": "get"}))
        except SynoBaseException as e:
            if _must_propagate(e):
                raise
            r = {"error": f"SYNO.Core.Region.NTP failed: DSM error {getattr(e, 'error_code', '?')}"}
        cache["clock"], cache["at"] = _make_clock(r), time.time()
    return cache["clock"]


def _make_clock(r: dict[str, Any]) -> NasClock:
    dsm_id = r.get("timezone")
    if dsm_id in DSM_ZONES:
        try:
            return NasClock(ZoneInfo(DSM_ZONES[dsm_id]), DSM_ZONES[dsm_id])
        except ZoneInfoNotFoundError:
            pass
    # Unknown zone id: fall back to the NAS's current fixed offset (NAS-local fields minus its own epoch).
    try:
        y, mo, d = (int(x) for x in str(r["date"]).split("/"))
        local = datetime(y, mo, d, int(r["hour"]), int(r["minute"]), int(r.get("second", 0)), tzinfo=timezone.utc)
        off = round((local.timestamp() - int(r["timestamp"])) / 900) * 900
        zone = timezone(timedelta(seconds=off))  # ValueError beyond +-24h, e.g. a zero timestamp
    except (KeyError, TypeError, ValueError):
        zone = None
    if zone is None:
        why = r.get("error") or f"zone {dsm_id!r} unknown and NAS clock unreadable"
        return NasClock(timezone.utc, f"UTC (NAS zone unknown: {why})")
    return NasClock(zone, f"{dsm_id or 'unknown zone'} (fixed {zone.tzname(None)}, DST not applied)")


def _action(code: Any) -> Any:
    return CLOUDSYNC_ACTIONS.get(code, code)


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None, False, empty or 0 (absent means normal)."""
    return {k: v for k, v in d.items() if v not in (None, False, "", [], {}, 0) or v is True}


def _table(rows: list[dict[str, Any]], columns: list[str], *, path_col: str | None = None,
           time_col: str | None = None, tz: str | None = None) -> dict[str, Any]:
    """Turn a list of dicts into {columns, rows}, hoisting shared values, a common path
    prefix and a shared date, and dropping columns that are empty in every row."""
    out: dict[str, Any] = {}
    if not rows:
        return {"rows": []}
    rows = [dict(r) for r in rows]

    if path_col:
        for r in rows:
            if r.get(path_col):
                r[path_col] = posixpath.normpath(r[path_col])
        paths = [r[path_col] for r in rows if r.get(path_col)]
        if len(paths) > 1:
            base = posixpath.commonpath([posixpath.dirname(p) for p in paths])
            if base not in ("", "/"):
                out["base"] = base + "/"
                for r in rows:
                    if r.get(path_col):
                        r[path_col] = r[path_col][len(base) + 1:]

    dates = {r[time_col][:10] for r in rows if r.get(time_col)} if time_col else set()
    if dates:
        if tz:
            out["tz"] = tz
        if len(dates) == 1:
            out["date"] = dates.pop()
            for r in rows:
                if r.get(time_col):
                    r[time_col] = r[time_col][11:]

    cols = [c for c in columns if any(r.get(c) not in (None, "", 0, False) for r in rows)]
    shared = {}
    if len(rows) > 1:
        for c in cols:
            first = rows[0].get(c)
            if c != path_col and all(r.get(c) == first for r in rows):
                shared[c] = first
    if shared:
        out["all"] = shared
    cols = [c for c in cols if c not in shared]
    out["columns"] = cols
    out["rows"] = [[r.get(c) for c in cols] for r in rows]
    return out


def _page(out: dict[str, Any], total: Any, offset: int, returned: int) -> dict[str, Any]:
    out["total"] = total
    if isinstance(total, int) and offset + returned < total:
        out["next_offset"] = offset + returned
    return out


def _strip_titles(node: Any, in_properties: bool = False) -> Any:
    """Remove pydantic's generated "title" keys from a JSON schema (they repeat the names)."""
    if isinstance(node, dict):
        return {k: _strip_titles(v, k == "properties") for k, v in node.items()
                if in_properties or k != "title"}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def build(settings: Settings, nas: Nas) -> MCPServer:
    mcp = MCPServer("synology", instructions=INSTRUCTIONS.format(host=settings.host, tier=settings.tier.name.lower()))

    def tool(tier: Tier, annotations: ToolAnnotations):
        """Register the tool only if the configured tier allows it."""

        def deco(fn):
            if settings.tier >= tier:
                mcp.add_tool(fn, annotations=annotations)
                registered = mcp._tool_manager.get_tool(fn.__name__)
                registered.parameters = _strip_titles(registered.parameters)
            return fn

        return deco

    # ---- system -------------------------------------------------------------

    @tool(Tier.READ, READ)
    def system_info() -> dict[str, Any]:
        """NAS hardware and DSM version: model, CPU, RAM, uptime, temperature, crash/upgrade flags, USB devices."""

        def run(a: Apis) -> dict[str, Any]:
            info = data(a.sysinfo.get_system_info())
            status = data(a.sysinfo.sys_status())
            return _compact({
                "model": info.get("model"),
                "dsm_version": info.get("firmware_ver"),
                "firmware_date": info.get("firmware_date"),
                "uptime": info.get("up_time"),
                "time": info.get("time"),
                # DSM's description is a fixed label ("(GMT+00:00) London" even during BST), so name the zone.
                "time_zone": _clock(a).name,
                "cpu": " ".join(str(info.get(k, "")) for k in ("cpu_vendor", "cpu_family", "cpu_series")).strip(),
                "cpu_cores": info.get("cpu_cores"),
                "ram_mb": info.get("ram_size"),
                "temperature_c": info.get("sys_temp"),
                "temperature_warning": bool(info.get("sys_tempwarn") or info.get("temperature_warning")),
                "system_crashed": status.get("is_system_crashed"),
                "upgrade_ready": status.get("upgrade_ready"),
                "usb_devices": [" ".join(filter(None, (d.get("producer"), d.get("product"))))
                                for d in info.get("usb_dev", [])],
            })

        return nas.call(run)

    @tool(Tier.READ, READ)
    def system_utilization() -> dict[str, Any]:
        """Live load: CPU %, load average, memory, per-disk and per-network-interface throughput."""

        def run(a: Apis) -> dict[str, Any]:
            u = a.sysinfo.get_all_system_utilization()
            if isinstance(u, dict) and "data" in u:
                u = u["data"]
            cpu, mem = u.get("cpu", {}), u.get("memory", {})

            disks, idle = [], []
            for d in u.get("disk", {}).get("disk", []):
                if d.get("utilization") or d.get("read_byte") or d.get("write_byte"):
                    disks.append({"name": d.get("display_name"), "device": d.get("device"),
                                  "util_pct": d.get("utilization"), "read_per_s": _size(d.get("read_byte")),
                                  "write_per_s": _size(d.get("write_byte"))})
                else:
                    idle.append(d.get("display_name") or d.get("device"))
            nics = []
            for n in u.get("network", []):
                if n.get("device") == "total":
                    continue
                if n.get("rx") or n.get("tx"):
                    nics.append({"device": n.get("device"), "rx_per_s": _size(n.get("rx")),
                                 "tx_per_s": _size(n.get("tx"))})
                else:
                    idle.append(n.get("device"))

            return _compact({
                "cpu_pct": {"user": cpu.get("user_load"), "system": cpu.get("system_load"),
                            "other": cpu.get("other_load")},
                # DSM reports load averages multiplied by 100.
                "load_avg_1_5_15": [cpu.get(f"{m}min_load", 0) / 100 for m in (1, 5, 15)],
                "memory": _compact({
                    "total": _size((mem.get("memory_size") or 0) * 1024),
                    "used_pct": mem.get("real_usage"),
                    "cached": _size((mem.get("cached") or 0) * 1024),
                    "swap_used_pct": mem.get("swap_usage"),
                }),
                "disks": _table(disks, ["name", "device", "util_pct", "read_per_s", "write_per_s"]) if disks else None,
                "network": _table(nics, ["device", "rx_per_s", "tx_per_s"]) if nics else None,
                "idle": idle,
            })

        return nas.call(run)

    @tool(Tier.READ, READ)
    def storage_status() -> dict[str, Any]:
        """Disk and volume health: volume usage and free space; drive model, status, SMART, temperature."""

        def run(a: Apis) -> dict[str, Any]:
            d = data(a.sysinfo.get_volume_info())
            volumes = []
            for v in d.get("vol_info", []):
                total, used = float(v.get("total_size") or 0), float(v.get("used_size") or 0)
                volumes.append({
                    "name": v.get("name"),
                    "status": v.get("status"),
                    "total": _size(total),
                    "used": _size(used),
                    "free": _size(total - used),
                    "used_pct": round(100 * used / total, 1) if total else None,
                    "encrypted": v.get("is_encrypted"),
                })
            drives = [
                {
                    "name": h.get("longName") or h.get("name"),
                    "device": h.get("id"),
                    "model": h.get("model"),
                    "serial": h.get("serial"),
                    "size": _size(h.get("size_total")),
                    "status": h.get("overview_status") or h.get("status"),
                    "smart": h.get("smart_status"),
                    "temp_c": h.get("temp"),
                    "ssd": h.get("isSsd"),
                }
                for h in d.get("hdd_info", [])
            ]
            return {
                "volumes": _table(volumes, ["name", "status", "total", "used", "free", "used_pct", "encrypted"]),
                "drives": _table(drives, ["name", "device", "model", "serial", "size", "status", "smart", "temp_c",
                                          "ssd"]),
            }

        return nas.call(run)

    # ---- Cloud Sync ---------------------------------------------------------

    def _rate(a: Apis, clock: NasClock, cid: int, remaining: int) -> dict[str, Any] | str:
        items = data(a.cloudsync.get_connection_logs(cid, limit=200)).get("items", [])
        ts = [i["time"] for i in items if i.get("time")]
        if len(ts) < 2 or max(ts) <= min(ts):
            return "not enough recent log entries"
        if time.time() - max(ts) > RATE_STALE_S:
            return f"idle: newest log entry is {clock.fmt(max(ts))} ({clock.name})"
        rate = (len(ts) - 1) / (max(ts) - min(ts))
        return _compact({
            "files_per_s": round(rate, 2),
            "window_s": max(ts) - min(ts),
            "eta_h": round(remaining / rate / 3600, 1) if remaining else None,
        })

    @tool(Tier.READ, READ)
    def cloudsync_status(estimate_rate: bool = True) -> dict[str, Any]:
        """Cloud Sync overview (Dropbox, Google Drive, OneDrive, S3, ...): per connection status, files left, cloud quota, sync tasks, bandwidth limits, sync schedule, and a files/s rate with ETA. Start here for "is the sync done / how long left". DSM has no per-connection bytes/s figure; per-file rates are in cloudsync_activity, NAS-wide network traffic in system_utilization."""

        def run(a: Apis) -> dict[str, Any]:
            d = data(a.cloudsync.get_connections())
            conns = []
            for c in d.get("conn", []):
                cid = c["id"]
                q = data(a.cloudsync.get_connection_information(cid))
                cfg = _optional(lambda: data(a.cloudsync.get_connection_settings(cid))) or {}
                sessions = data(a.cloudsync.get_tasks(cid)).get("sess", [])
                tasks = [
                    {
                        "sess_id": s.get("sess_id"),
                        "local": s.get("local_sync_path"),
                        "remote": s.get("remote_sync_path"),
                        "direction": s.get("sync_direction"),
                        "status": s.get("sync_status"),
                        "error": s.get("error_desc"),
                    }
                    for s in sessions
                ]
                conns.append(_compact({
                    "conn_id": cid,
                    "name": c.get("task_display_name") or c.get("task_name"),
                    "provider": c.get("type"),
                    "account": c.get("user_name"),
                    "status": c.get("status"),
                    "unfinished_files": c.get("unfinished_files"),
                    "quota": (f"{_size(q['quota_used'])} of {_size(q['quota_total'])} "
                              f"({100 * q['quota_used'] / q['quota_total']:.1f}%)") if q.get("quota_total") else None,
                    "tasks": _table(tasks, ["sess_id", "local", "remote", "direction", "status", "error"]),
                    "limits": (_compact({"upload": _limit_str(cfg.get("max_upload_speed")),
                                         "download": _limit_str(cfg.get("max_download_speed"))}) or "unknown")
                    if cfg else "unknown",
                    "sync_schedule": _sync_schedule(cfg) if cfg else "unknown",
                    "poll_interval_s": cfg.get("pull_event_period"),
                    "rate": _rate(a, _clock(a), cid, c.get("unfinished_files") or 0) if estimate_rate and c.get(
                        "unfinished_files") else None,
                }))
            return _compact({"status": d.get("tray_status"), "paused": d.get("is_pause"), "connections": conns})

        return nas.call(run)

    @tool(Tier.READ, READ)
    def cloudsync_activity(limit: Annotated[int, Field(ge=1, le=200)] = 10) -> dict[str, Any]:
        """Cloud Sync live activity: files transferring right now (with progress and each file's own transfer rate) and the latest completed changes. Per-file rates don't add up to a connection's throughput (files start and finish between polls)."""

        def run(a: Apis) -> dict[str, Any]:
            d = data(a.cloudsync.get_recently_modified())
            processing = d.get("processing_items") or []
            history = d.get("history_items") or []
            out: dict[str, Any] = {}
            if processing:
                out["transferring"] = _table(
                    [{"status": i.get("status"), "path": i.get("path"), "sess_id": i.get("session_id"),
                      "size": _size(i.get("total_size")), "done": _size(i.get("current_size")),
                      "rate": f"{_size(_num(i.get('bit_rate')))}/s" if _num(i.get("bit_rate")) else None}
                     for i in processing[:limit]],
                    ["status", "sess_id", "path", "size", "done", "rate"], path_col="path")
                out["transferring"]["total"] = len(processing)
            if history:
                out["recent"] = _table(
                    [{"action": _action(i.get("action")), "path": i.get("path"), "sess_id": i.get("session_id")}
                     for i in history[:limit]],
                    ["action", "sess_id", "path"], path_col="path")
            if d.get("server_merge_items"):
                out["server_merge_items"] = d["server_merge_items"]
            return out or {"idle": True}

        return nas.call(run)

    @tool(Tier.READ, READ)
    def cloudsync_logs(
        conn_id: ConnId,
        limit: Annotated[int, Field(ge=1, le=500)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        keyword: Annotated[str, Field(description="case-sensitive substring of the path")] = "",
        action: Literal["any", "upload", "download", "delete local", "delete remote"] = "any",
    ) -> dict[str, Any]:
        """Cloud Sync log history for one connection, newest first: what was uploaded, downloaded or deleted, and errors. Filter by action or keyword."""
        codes = {v: k for k, v in CLOUDSYNC_ACTIONS.items()}

        def run(a: Apis) -> dict[str, Any]:
            clock = _clock(a)
            d = data(a.cloudsync.get_connection_logs(conn_id, keyword=keyword, offset=offset, limit=limit,
                                                     action=codes.get(action, -1)))
            items = d.get("items", [])
            out = _table(
                [{"time": clock.fmt(i.get("time")), "action": _action(i.get("action")), "path": i.get("path"),
                  "type": i.get("file_type"), "sess_id": i.get("session_id"), "level": i.get("log_level"),
                  "error_code": i.get("error_code"), "message": i.get("message")}
                 for i in items],
                ["time", "action", "sess_id", "type", "path", "level", "error_code", "message"],
                path_col="path", time_col="time", tz=clock.name)
            return _page(out, d.get("total"), offset, len(items))

        return nas.call(run)

    @tool(Tier.WRITE, WRITE)
    def cloudsync_pause(conn_id: ConnId) -> dict[str, Any]:
        """Pause syncing for one Cloud Sync connection."""
        # The ge=0 bound matters: synology-api treats a negative id as "every connection".
        if conn_id < 0:
            raise ToolError(f"conn_id must be a connection id from cloudsync_status, got {conn_id}")
        nas.call(lambda a: data(a.cloudsync.connection_pause(conn_id)))
        return {"paused": conn_id}

    @tool(Tier.WRITE, WRITE)
    def cloudsync_resume(conn_id: ConnId) -> dict[str, Any]:
        """Resume syncing for one paused Cloud Sync connection."""
        if conn_id < 0:
            raise ToolError(f"conn_id must be a connection id from cloudsync_status, got {conn_id}")
        nas.call(lambda a: data(a.cloudsync.connection_resume(conn_id)))
        return {"resumed": conn_id}

    # ---- Task Scheduler -----------------------------------------------------

    @tool(Tier.READ, READ)
    def taskscheduler_tasks() -> dict[str, Any]:
        """DSM Task Scheduler: every scheduled and event-triggered task (including root's) with schedule, next run and the last run's result and duration. Use to confirm a nightly job (e.g. a backup) succeeded."""

        def run(a: Apis) -> dict[str, Any]:
            clock = _clock(a)
            rows = []
            for t in data(a.tasks.get_task_list(limit=500)).get("tasks", []):
                tid, typ = t.get("id"), t.get("type", "")
                row: dict[str, Any] = {"task_id": tid, "name": t.get("name"), "type": typ, "owner": t.get("owner"),
                                       "disabled": not t.get("enable", True)}
                nxt = t.get("next_trigger_time")
                if typ.startswith("event"):
                    row["schedule"] = f"on {nxt}" if nxt else "event"
                else:
                    try:
                        cfg = data(a.tasks.get_task_config(tid, real_owner=t.get("real_owner") or t.get("owner"),
                                                           type=typ))
                        row["schedule"] = _schedule(cfg.get("schedule") or {})
                    except SynoBaseException as e:
                        if _must_propagate(e):
                            raise
                        row["schedule"] = f"unavailable (DSM error {getattr(e, 'error_code', '?')})"
                    row["next_run"] = clock.local(nxt)
                try:
                    runs = data(a.tasks.get_task_results(tid)) if not typ.startswith("event") else []
                except SynoBaseException as e:
                    if _must_propagate(e):
                        raise
                    runs = []
                if runs:
                    row.update(_run(runs[0], clock))
                rows.append(row)
            out = _table(rows, ["task_id", "name", "type", "owner", "disabled", "schedule", "next_run",
                                "last_run", "result", "duration"])
            out["tz"] = clock.name  # schedules, next_run and last_run are all NAS-local
            return out

        return nas.call(run)

    @tool(Tier.READ, READ)
    def taskscheduler_runs(task_id: Annotated[int, Field(ge=0, description="from taskscheduler_tasks")],
                           limit: Annotated[int, Field(ge=1, le=200)] = 10) -> dict[str, Any]:
        """Run history of one scheduled task, newest first: start time, duration and exit result. Also shows the task's action (the command, for script tasks)."""

        def run(a: Apis) -> dict[str, Any]:
            clock = _clock(a)
            task = next((t for t in data(a.tasks.get_task_list(limit=500)).get("tasks", []) if t.get("id") == task_id),
                        None)
            if task is None:
                raise ToolError(f"no task with task_id {task_id}; see taskscheduler_tasks")
            runs = data(a.tasks.get_task_results(task_id)) or []
            out = _table([_run(r, clock, "start") for r in runs[:limit]], ["start", "result", "duration"],
                         time_col="start", tz=clock.name)
            return {"task": _compact({"name": task.get("name"), "owner": task.get("owner"),
                                      "action": task.get("action")}), **out, "total": len(runs)}

        return nas.call(run)

    # ---- Log Center ---------------------------------------------------------

    @tool(Tier.READ, READ)
    def logcenter_logs(
        log: Literal["system", "connection", "smb"] = "system",
        level: Literal["any", "info", "warn", "err"] = "any",
        keyword: Annotated[str, Field(description="substring of the message (smb: of the path)")] = "",
        since: Annotated[str, Field(description='"24h", "7d", "30m" or "YYYY-MM-DD[ HH:MM]" (NAS local time)')] = "",
        until: Annotated[str, Field(description="same formats as since; a bare date includes that whole day")] = "",
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        summarize: Annotated[Literal["none", "day", "hour"], Field(
            description="instead of entries: per-event count, first/last seen and median interval, plus counts per "
                        "day or hour (periods with none are omitted). Events are distinct message texts, so use "
                        "keyword to group variants (e.g. different IPs)")] = "none",
    ) -> dict[str, Any]:
        """DSM Log Center, newest first. system: hardware, disk/RAID, network link, updates, services. connection: sign-ins and failed logins (DSM, SSH, SMB, NFS...). smb: SMB file operations (who, IP, path). For "since when / how many / how often" questions use summarize (cheap) with keyword and since/until."""
        params: dict[str, Any] = {"logtype": json.dumps(LOGTYPES[log])}
        if keyword:
            params["keyword"] = json.dumps(keyword)

        def fetch(a: Apis, start: int, count: int) -> tuple[int, list[dict[str, Any]]]:
            return _syslog(a, start, count, **params)

        def run(a: Apis) -> dict[str, Any]:
            clock = _clock(a)
            lo, hi = clock.parse_when(since), clock.parse_when(until, end_of_day=True)
            if lo is not None or hi is not None:
                params["date_from"] = lo or 0
                params["date_to"] = hi or int(time.time()) + 86400
            if summarize != "none":
                return _log_summary(a, clock, fetch, offset, level, log, summarize)
            if level == "any":
                total, items = fetch(a, offset, limit)
                next_raw = offset + len(items) if offset + len(items) < total else None
                scanned = None
            else:
                # DSM's list API exposes no working level filter, so filter here over a bounded scan.
                items, pos, total, scanned = [], offset, None, 0
                while len(items) < limit and scanned < LEVEL_SCAN_MAX:
                    total, page = fetch(a, pos, LEVEL_SCAN_PAGE)
                    if not page:
                        total = pos  # nothing more to read; no next_offset
                        break
                    for i, it in enumerate(page):
                        if it.get("level") == level:
                            items.append(it)
                            if len(items) == limit:
                                page = page[:i + 1]
                                break
                    pos += len(page)
                    scanned += len(page)
                next_raw = pos if total is not None and pos < total else None
            if log == "smb":
                rows = [{"time": clock.local(i.get("time")), "user": i.get("username"), "ip": i.get("ip"),
                         "cmd": i.get("cmd"),
                         "path": (i.get("descr") or "") + ("/" if i.get("isdir") == "true" else ""),
                         "size": None if i.get("isdir") == "true" else i.get("filesize")} for i in items]
                out = _table(rows, ["time", "user", "ip", "cmd", "path", "size"], path_col="path", time_col="time",
                             tz=clock.name)
            else:
                rows = [{"time": clock.local(i.get("time")), "level": i.get("level"), "who": i.get("who"),
                         "event": i.get("descr")} for i in items]
                out = _table(rows, ["time", "level", "who", "event"], time_col="time", tz=clock.name)
            out["total" if level == "any" else "total_unfiltered"] = total
            if next_raw is not None:
                out["next_offset"] = next_raw
            if scanned and len(items) < limit and next_raw is not None:
                out["note"] = f"level filter scanned {scanned} entries; continue with next_offset"
            return out

        return nas.call(run)

    @tool(Tier.READ, READ)
    def dsm_notifications(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
        """DSM desktop notifications, newest first (e.g. Security Advisor, package and DSM updates). Titles are DSM message ids; params are their values."""

        def run(a: Apis) -> dict[str, Any]:
            clock = _clock(a)
            d = data(a.sysinfo.request_data("SYNO.Core.DSMNotify", "entry.cgi",
                                            {"version": 1, "method": "notify", "action": '"load"'}))
            items = sorted(d.get("items") or [], key=lambda i: i.get("time") or 0, reverse=True)
            rows = []
            for i in items[:limit]:
                params: dict[str, Any] = {}
                for m in i.get("msg") or []:
                    try:
                        params.update(json.loads(m))
                    except (TypeError, ValueError):
                        params.setdefault("text", m)
                params = {k.strip("%").lower(): v for k, v in params.items() if k not in NOTIFY_DROP_PARAMS}
                rows.append({"time": clock.fmt(i.get("time")), "level": NOTIFY_LEVELS.get(i.get("level"), i.get("level")),
                             "title": i.get("title"),
                             "params": "; ".join(f"{k}={v}" for k, v in params.items()) or None})
            out = _table(rows, ["time", "level", "title", "params"], time_col="time", tz=clock.name)
            out["total"] = len(items)
            return out

        return nas.call(run)

    # ---- Network ------------------------------------------------------------

    @tool(Tier.READ, READ)
    def network_config() -> dict[str, Any]:
        """Network setup (static configuration): hostname, default gateway, DNS servers, and each interface's IP/subnet, MAC, link status, negotiated speed/duplex and MTU. Live throughput is in system_utilization."""

        def run(a: Apis) -> dict[str, Any]:
            net = data(a.sysinfo.network_status())
            info = data(a.sysinfo.get_network_info())
            macs = {n.get("id"): n.get("mac") for n in info.get("nif") or []}
            gw = net.get("gateway_info") or {}
            ifaces = []
            for e in data(a.sysinfo.network_ethernet()) or []:
                up = e.get("status") == "connected"
                ifaces.append({
                    "name": e.get("ifname"), "status": e.get("status"), "ip": e.get("ip"), "mask": e.get("mask"),
                    "mac": macs.get(e.get("ifname")),
                    "speed_mbps": e.get("speed") if up and (e.get("speed") or 0) > 0 else None,
                    "duplex": ("full" if e.get("duplex") else "half") if up else None,
                    "mtu": e.get("mtu"), "addressing": "dhcp" if e.get("use_dhcp") else "static",
                    "ipv6": ", ".join(str(x.get("address", x)) if isinstance(x, dict) else str(x)
                                      for x in e.get("ipv6") or []) or None,
                    "vlan": e.get("vlan_id") if e.get("enable_vlan") else None,
                })
            bonds = data(a.sysinfo.network_bond()) or []
            return _compact({
                "hostname": net.get("server_name") or info.get("hostname"),
                "workgroup": info.get("workgroup"),
                "gateway": net.get("gateway"),
                "gateway_interface": gw.get("ifname"),
                "ipv6_gateway": net.get("v6gateway"),
                "dns": [d for d in (net.get("dns_primary"), net.get("dns_secondary")) if d],
                "dns_source": "manual" if net.get("dns_manual") else "dhcp",
                "interfaces": _table(ifaces, ["name", "status", "ip", "mask", "mac", "speed_mbps", "duplex", "mtu",
                                              "addressing", "ipv6", "vlan"]),
                "bonds": [_compact(b) for b in bonds] if isinstance(bonds, list) else None,
            })

        return nas.call(run)

    # ---- Packages -----------------------------------------------------------

    @tool(Tier.READ, READ)
    def packages() -> dict[str, Any]:
        """Installed packages with version, running/stopped status and available updates (new version, and whether it is a security release), checked against Package Center's catalogue and any third-party feeds configured there. Packages in neither (system components, manual installs) show update_to "not in catalogue"."""

        def run(a: Apis) -> dict[str, Any]:
            installed = data(a.packages.list_installed(additional=["status"])).get("packages") or []
            out: dict[str, Any] = {}
            try:
                catalogue = data(a.packages.list_installable())
                latest = {p.get("id") or p.get("package"): p for p in catalogue.get("packages") or []}
                # Third-party feeds added in Package Center (e.g. SynoCommunity) are a separate listing:
                # blloadothers=true returns only those, fetched live (a few seconds). Optional.
                others = _optional(lambda: _raw(a, "SYNO.Core.Package.Server", "list", blforcereload="false",
                                                blloadothers="true"))
                if others is None:
                    out["third_party_feeds"] = "not checked (feed listing failed)"
                for p in (others or {}).get("packages") or []:
                    latest.setdefault(p.get("id") or p.get("package"), p)
            except SynoBaseException as e:
                if _must_propagate(e):
                    raise
                latest = None
                out["updates_available"] = (f"unknown: Package Center catalogue unavailable "
                                            f"(DSM error {getattr(e, 'error_code', '?')})")
            rows = []
            for p in sorted(installed, key=lambda p: (p.get("name") or p.get("id") or "").lower()):
                add = p.get("additional") or {}
                row = {"name": p.get("name") or p.get("id"), "id": p.get("id"), "version": p.get("version"),
                       "status": add.get("status")}
                if latest is not None:
                    new = latest.get(p.get("id"))
                    if new is None:
                        row["update_to"] = "not in catalogue"  # system component or manual install: not checked
                    elif _version_key(new.get("version")) > _version_key(p.get("version")):
                        row["update_to"] = new.get("version")
                        row["security_update"] = bool(new.get("is_security_version"))
                rows.append(row)
            out["packages"] = _table(rows, ["name", "id", "version", "status", "update_to", "security_update"])
            if latest is not None:
                out["updates_available"] = sum(1 for r in rows if r.get("update_to") not in (None, "not in catalogue"))
            return out

        return nas.call(run)

    # ---- Hardware & power ---------------------------------------------------

    @tool(Tier.READ, READ)
    def hardware_power(wake_events: Annotated[int, Field(ge=0, le=100)] = 10) -> dict[str, Any]:
        """Disk hibernation timers (internal and USB disks), hibernation debug settings, recent disk wake-ups from the system log, fan mode, power-on/off schedule, restart after power loss, Wake-on-LAN and UPS status. DSM does not report whether a disk is currently spun down."""

        def run(a: Apis) -> dict[str, Any]:
            clock = _clock(a)
            hib = data(a.sysinfo.hardware_hibernation())
            support = _optional(lambda: _raw(a, "SYNO.Core.SupportForm.Service", "get")) or {"error": "unknown"}
            usb = (_optional(lambda: _raw(a, "SYNO.Core.ExternalDevice.Storage.USB", "list",
                                          additional=json.dumps(["all"]))) or {}).get("devices") or []
            fan = data(a.sysinfo.hardware_fan_speed())
            schedule = _optional(lambda: _raw(a, "SYNO.Core.Hardware.PowerSchedule", "load"))
            recovery = _optional(lambda: _raw(a, "SYNO.Core.Hardware.PowerRecovery", "get"))
            ups = data(a.sysinfo.hardware_ups())

            wakes: dict[str, Any] | str | None = None
            if wake_events and not hib.get("enable_log"):
                wakes = "not logged: the HDD wake-up log is off (Control Panel > Hardware & Power)"
            elif wake_events:
                total, items = _syslog(a, 0, wake_events, logtype=json.dumps("system"),
                                       keyword=json.dumps("woke up from hibernation"))
                wakes = _table([{"time": clock.local(i.get("time")), "event": i.get("descr")} for i in items],
                               ["time", "event"], time_col="time", tz=clock.name)
                wakes["total"] = total

            return _compact({
                # Settings are stated explicitly (on/off), since the question is usually "is X enabled?".
                "hibernation": {
                    "internal_disks_idle": _minutes(hib.get("internal_hd_idletime")),
                    "usb_disks_idle": _minutes(hib.get("usb_idletime")),
                    "wake_up_log": _onoff(hib.get("enable_log")),
                    "auto_poweroff": _onoff(hib.get("auto_poweroff_enable")),
                    "debug_mode": support.get("error") or (
                        f"on (level {support.get('hibernation_debug_level')})" if support.get("hibernation_debug_en")
                        else "off"),
                },
                "usb_disks": [_compact({"id": d.get("dev_id"), "name": d.get("dev_title"),
                                        "product": " ".join(filter(None, (d.get("producer"), d.get("product")))),
                                        "shares": [p.get("share_name") for p in d.get("partitions") or []
                                                   if p.get("share_name")],
                                        "status": d.get("status")}) for d in usb],
                "recent_wake_ups": wakes,
                "fan_mode": fan.get("dual_fan_speed") or fan.get("fan_speed"),
                "power": {
                    "restart_after_power_failure": _onoff(recovery.get("rc_power_config")) if recovery else "unknown",
                    "wake_on_lan": (", ".join(f"LAN {w.get('idx')}" for w in recovery.get("wol") or [] if w.get("enable"))
                                    or "off") if recovery else "unknown",
                    "power_on_schedule": (schedule.get("poweron_tasks") or "none") if schedule else "unknown",
                    "power_off_schedule": (schedule.get("poweroff_tasks") or "none") if schedule else "unknown",
                },
                "ups": _compact({
                    "enabled": ups.get("enable"), "connected": ups.get("usb_ups_connect"), "mode": ups.get("mode"),
                    "model": " ".join(filter(None, (ups.get("manufacture"), ups.get("model")))),
                    "status": ups.get("status"),
                }) | {"charge_pct": ups.get("charge"), "runtime_s": ups.get("runtime")}  # 0 matters here
                if ups.get("enable") or ups.get("usb_ups_connect") else "not configured",
            })

        return nas.call(run)

    # ---- Security -----------------------------------------------------------

    @tool(Tier.READ, READ)
    def security_advisor() -> dict[str, Any]:
        """Security Advisor: time and overall result of the latest scan, results per category, and every failing check with its details (e.g. out-of-date DSM or packages)."""

        def run(a: Apis) -> dict[str, Any]:
            clock = _clock(a)
            st = data(a.sysinfo.get_security_scan_status())
            rules = (data(a.sysinfo.get_security_scan_rules()).get("items") or {}).values()
            cats = []
            for name, c in sorted((st.get("items") or {}).items()):
                fails = {k: v for k, v in (c.get("fail") or {}).items() if v}
                cats.append({"category": name, "result": c.get("failSeverity"), "checks": c.get("total"),
                             "failed": ", ".join(f"{v} {k}" for k, v in fails.items()) or None})
            failing = [{"category": r.get("category"), "severity": r.get("severity"), "check": r.get("strId"),
                        "details": "; ".join(f"{k.strip('@').lower()}={v}"
                                             for k, v in ((r.get("action") or {}).get("actionVar") or {}).items())
                        or None}
                       for r in rules if r.get("status") == "fail"]
            return _compact({
                "last_scan": clock.fmt(st.get("lastScanTime")), "tz": clock.name,
                "result": st.get("sysStatus"),
                "running": (st.get("sysProgress") is not None and _num(st.get("sysProgress")) < 100) or None,
                "categories": _table(cats, ["category", "result", "checks", "failed"]),
                "failing": _table(failing, ["category", "severity", "check", "details"]) if failing else None,
            })

        return nas.call(run)

    @tool(Tier.READ, READ)
    def certificates() -> dict[str, Any]:
        """Installed TLS certificates: subject and alternative names, issuer, validity dates, days until expiry, and which services use each one."""

        def run(a: Apis) -> dict[str, Any]:
            clock = _clock(a)
            rows = []
            for c in _raw(a, "SYNO.Core.Certificate.CRT", "list").get("certificates") or []:
                subj, iss = c.get("subject") or {}, c.get("issuer") or {}
                expires = _cert_epoch(c.get("valid_till"))
                rows.append({
                    "id": c.get("id"), "description": c.get("desc"), "default": c.get("is_default"),
                    "subject": subj.get("common_name"),
                    "alt_names": ", ".join(subj.get("sub_alt_name") or []) or None,
                    "issuer": iss.get("common_name") or iss.get("organization"),
                    "valid_from": clock.fmt(_cert_epoch(c.get("valid_from"))) or c.get("valid_from"),
                    "expires": clock.fmt(expires) or c.get("valid_till"),
                    "days_left": int((expires - time.time()) // 86400) if expires else None,
                    "broken": c.get("is_broken"),
                    "services": ", ".join(s.get("display_name") or s.get("service") or "" for s in c.get("services") or []) or None,
                })
            return _table(rows, ["id", "description", "default", "subject", "alt_names", "issuer", "valid_from",
                                 "expires", "days_left", "broken", "services"]) | {"tz": clock.name}

        return nas.call(run)

    # ---- File Station -------------------------------------------------------

    @tool(Tier.READ, READ)
    def filestation_list_shares() -> dict[str, Any]:
        """Shared folders (the top-level paths on the NAS), grouped by free space on their volume."""

        def run(a: Apis) -> dict[str, Any]:
            d = data(a.filestation.get_list_share(additional=["volume_status"]))
            by_free: dict[str, list[str]] = {}
            for s in d.get("shares", []):
                vs = (s.get("additional") or {}).get("volume_status") or {}
                by_free.setdefault(_size(vs.get("freespace")) or "unknown", []).append(s.get("name"))
            return {"shares_by_free_space": by_free}

        return nas.call(run)

    @tool(Tier.READ, READ)
    def filestation_list(
        path: Annotated[str, Field(description='folder path, e.g. "/media/film"')],
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
        pattern: Annotated[str, Field(description='glob, e.g. "*.jpg"')] = "",
        sort_by: Literal["name", "size", "mtime", "type"] = "name",
        sort_direction: Literal["asc", "desc"] = "asc",
    ) -> dict[str, Any]:
        """List a folder's contents with size and modified time."""

        def run(a: Apis) -> dict[str, Any]:
            d = data(a.filestation.get_file_list(folder_path=path, offset=offset, limit=limit, pattern=pattern or None,
                                                 sort_by=sort_by, sort_direction=sort_direction,
                                                 additional=["size", "time"]))
            files = d.get("files", [])
            clock = _clock(a)
            out = _table([_file(f, clock) for f in files], ["name", "size", "modified"], time_col="modified",
                         tz=clock.name)
            return _page(out, d.get("total"), offset, len(files))

        return nas.call(run)

    @tool(Tier.READ, READ)
    def filestation_info(paths: list[str]) -> dict[str, Any]:
        """Size, modified time and real volume path for specific files or folders."""

        def run(a: Apis) -> dict[str, Any]:
            d = data(a.filestation.get_file_info(path=paths, additional_param=["size", "time", "real_path", "type"]))
            clock = _clock(a)
            rows = [{**_file(f, clock), "path": f.get("path")} for f in d.get("files", [])]
            return _table(rows, ["path", "size", "modified", "real_path", "error_code"], time_col="modified",
                          tz=clock.name)

        return nas.call(run)

    return mcp


LOGTYPES = {"system": "system", "connection": "connection", "smb": "cifs"}
LEVEL_SCAN_PAGE = 1000
LEVEL_SCAN_MAX = 20000
SUMMARY_MAX_EVENTS = 20
SUMMARY_MAX_BUCKETS = 400
# Redundant (host, OS, the NAS-local date/time of the time column) or HTML link fragments.
NOTIFY_DROP_PARAMS = {"%HOSTNAME%", "%OSNAME%", "%DATE%", "%TIME%", "%PRE_APP_LINK%", "%POST_APP_LINK%"}
NOTIFY_LEVELS = {"NOTIFICATION_INFO": "info", "NOTIFICATION_WARN": "warn", "NOTIFICATION_ERROR": "err"}
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]  # DSM week_day: 0 = Sunday

def _raw(a: Apis, api: str, method: str, **params: Any) -> Any:
    """Call a DSM API the library has no method for, at its highest advertised version."""
    info = a.sysinfo.gen_list.get(api)
    if not info:
        raise ToolError(f"{api} is not available on this NAS")
    return data(a.sysinfo.request_data(api, info["path"], {"version": info["maxVersion"], "method": method, **params}))


def _syslog(a: Apis, offset: int, limit: int, **params: Any) -> tuple[int, list[dict[str, Any]]]:
    """One page of SYNO.Core.SyslogClient.Log (string params must be JSON-quoted)."""
    d = data(a.logcenter.request_data("SYNO.Core.SyslogClient.Log", "entry.cgi",
                                      {"version": 1, "method": "list", "offset": offset, "limit": limit, **params}))
    return d.get("total") or 0, d.get("items") or []


def _version_key(v: Any) -> tuple[int, ...]:
    """Package versions like "3.0.5-24122" compared numerically, part by part."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def _num(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _rate_str(bps: int) -> str:
    return f"{_size(bps)}/s ({bps * 8 / 1e6:.1f} Mbit/s)"


def _limit_str(v: Any) -> str | None:
    """Cloud Sync bandwidth limit: stored in bytes/s (DSM's UI shows it /1024 as KB/s); 0 = unlimited.
    Rendered in the same decimal units as the transfer rates so the two compare directly."""
    if v is None:
        return None
    return "unlimited" if not v else _rate_str(_num(v))


def _sync_schedule(cfg: dict[str, Any]) -> Any:
    """Cloud Sync schedule: a 168-char bitmap, index 24*day + hour, Sunday first ('1' = sync allowed),
    as DSM's SYNO.ux.ScheduleTable lays it out. Rendered as allowed hours per day."""
    bits = cfg.get("schedule_info") or ""
    if not cfg.get("is_enabled_schedule"):
        return "always"
    if len(bits) != 168:
        return f"unknown format: {bits!r}"
    if "0" not in bits:
        return "always"
    out = {}
    for d, day in enumerate(WEEKDAYS):
        hours = [h for h in range(24) if bits[24 * d + h] != "0"]
        out[day] = _hour_ranges(hours) or "none"
    return out


def _hour_ranges(hours: list[int]) -> str:
    """[0,1,2,9,10] -> "00-03, 09-11" (end exclusive)."""
    spans, start = [], None
    for h in range(25):
        if h < 24 and h in hours:
            start = h if start is None else start
        elif start is not None:
            spans.append(f"{start:02d}-{h:02d}")
            start = None
    return ", ".join(spans)


def _cert_epoch(s: Any) -> int | None:
    """OpenSSL-style date ("Oct  4 14:57:53 2027 GMT") to epoch seconds."""
    if not isinstance(s, str):
        return None
    try:
        return int(datetime.strptime(" ".join(s.split()), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _log_summary(a: Apis, clock: NasClock, fetch: Callable[[Apis, int, int], tuple[int, list[dict[str, Any]]]],
                 offset: int, level: str, log: str, bucket: str) -> dict[str, Any]:
    """Aggregate matching log entries (newest first, bounded scan) per event and per day/hour."""
    entries, pos, total = [], offset, 0
    while pos - offset < LEVEL_SCAN_MAX:
        total, page = fetch(a, pos, LEVEL_SCAN_PAGE)
        if not page:
            total = pos  # nothing more to read; don't hand back an offset that loops
            break
        pos += len(page)
        entries += [e for e in page if level == "any" or e.get("level") == level]
        if pos >= total:
            break
    events: dict[str, list[int]] = {}
    buckets: dict[str, int] = {}
    width = 10 if bucket == "day" else 13  # "YYYY-MM-DD" / "YYYY-MM-DD HH"
    for e in entries:
        key = (f"{e.get('cmd')} by {e.get('username')}" if log == "smb" else e.get("descr")) or "?"
        t = clock.local(e.get("time"))
        if isinstance(t, str) and len(t) >= width:
            buckets[t[:width]] = buckets.get(t[:width], 0) + 1
        ts = clock.epoch(e.get("time"))
        events.setdefault(key, []).append(ts if ts is not None else 0)
    rows = []
    for key, ts in sorted(events.items(), key=lambda kv: -len(kv[1]))[:SUMMARY_MAX_EVENTS]:
        count, ts = len(ts), sorted(t for t in ts if t)  # first/last/median from parseable times only
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
        rows.append({"event": key, "count": count, "first": clock.fmt(ts[0]) if ts else None,
                     "last": clock.fmt(ts[-1]) if ts else None,
                     "median_interval": _duration(gaps[len(gaps) // 2]) if gaps else None})
    out: dict[str, Any] = {
        "tz": clock.name,
        "matched": len(entries),
        "scanned": pos - offset,
        "events": _table(rows, ["event", "count", "first", "last", "median_interval"]),
        bucket: {"columns": [bucket, "count"], "rows": [[k, v] for k, v in sorted(buckets.items())[-SUMMARY_MAX_BUCKETS:]]},
    }
    if len(buckets) > SUMMARY_MAX_BUCKETS:
        out["buckets_note"] = f"showing the newest {SUMMARY_MAX_BUCKETS} of {len(buckets)} {bucket}s; narrow with since/until"
    if len(events) > SUMMARY_MAX_EVENTS:
        out["other_events"] = len(events) - SUMMARY_MAX_EVENTS
    if pos < total:
        out["note"] = (f"scan stopped after {pos - offset} of {total} entries (oldest not reached); narrow with "
                       f"since/until, or continue from next_offset as a separate summary")
        out["next_offset"] = pos
    return out


def _onoff(v: Any) -> str | None:
    """None when DSM didn't report the setting, so it isn't shown as "off"."""
    return None if v is None else "on" if v else "off"


def _optional(fn: Callable[[], Any]) -> Any:
    """Run a lookup that some NAS models or accounts may not support: None instead of failing the tool."""
    try:
        return fn()
    except ToolError:
        return None
    except SynoBaseException as e:
        if _must_propagate(e):
            raise
        return None


def _minutes(v: Any) -> str | None:
    """Hibernation idle time (minutes; 0 = never)."""
    if v is None:
        return None
    return "never" if not v else f"{v} min"


def _must_propagate(e: SynoBaseException) -> bool:
    """Connection/HTTP errors (no error_code) and expired sessions must reach Nas.call."""
    code = getattr(e, "error_code", None)
    return code is None or code in SESSION_ERRORS


def _duration(seconds: float) -> str | None:
    if seconds < 0:  # clock change mid-run
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


def _run(r: dict[str, Any], clock: NasClock, start_key: str = "last_run") -> dict[str, Any]:
    """One Task Scheduler result entry as {start_key, result, duration}."""
    start = (int(r["timestamp"]) if str(r.get("timestamp", "")).isdigit() else None) or clock.epoch(r.get("start_time"))
    stop = clock.epoch(r.get("stop_time"))
    code, kind = r.get("exit_code"), r.get("exit_type")
    if stop is None and not r.get("stop_time"):
        result = "running"
    elif kind == "normal" and code == 0:
        result = "ok"
    else:
        result = f"{kind or 'exit'} {code}"
    return {start_key: clock.fmt(start), "result": result,
            "duration": _duration(stop - start) if start is not None and stop is not None else None}


def _schedule(s: dict[str, Any]) -> str:
    """Render a DSM Task Scheduler schedule (codes as in synology_api.task_scheduler._Schedule)."""
    at = f"{s.get('hour', 0):02d}:{s.get('minute', 0):02d}"
    if s.get("repeat_hour"):
        at += f" then every {s['repeat_hour']}h (last_work_hour {s.get('last_work_hour', 0)})"
    elif s.get("repeat_min"):
        at += f" then every {s['repeat_min']}min (last_work_hour {s.get('last_work_hour', 0)})"
    if s.get("date_type", 0) == 0:
        days = [d for d in str(s.get("week_day", "")).split(",") if d.strip().isdigit()]
        names = ",".join(WEEKDAYS[int(d)] for d in days if 0 <= int(d) < 7)
        every = {1001: "daily", 1002: "weekly", 1003: "monthly"}.get(s.get("repeat_date"),
                                                                       f"repeat_date={s.get('repeat_date')}")
        if every == "daily" and len(days) == 7:
            return f"daily {at}"
        if every in ("daily", "weekly"):
            return f"{names} {at}"
        weeks = s.get("monthly_week") or []
        return " ".join(filter(None, (every, f"weeks {weeks}" if weeks else "", names, at)))
    every = {0: "once", 1: "monthly", 2: "yearly", 3: "every 6 months", 5: "every 3 months"}.get(
        s.get("repeat_date"), f"repeat_date={s.get('repeat_date')}")
    return f"{every} from {s.get('date')} {at}"


def _file(f: dict[str, Any], clock: NasClock) -> dict[str, Any]:
    add = f.get("additional") or {}
    t = add.get("time") or {}
    out: dict[str, Any] = {"name": (f.get("name") or "") + ("/" if f.get("isdir") else "")}
    if f.get("code"):  # File Station reports per-file errors in place, e.g. 408 for a missing path
        out["error_code"] = f["code"]
    if not f.get("isdir") and "size" in add:
        out["size"] = _size(add["size"])
    if t.get("mtime"):
        out["modified"] = clock.fmt(t["mtime"])
    if add.get("real_path"):
        out["real_path"] = add["real_path"]
    return out

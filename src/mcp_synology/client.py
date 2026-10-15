"""Thread-safe wrapper around synology-api with automatic re-login.

synology-api keeps one login on the class attribute ``BaseApi.shared_session``,
shared by every API object. DSM expires idle sessions, so a long-running server
has to notice an expired session, drop it, log in again and retry the call.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

import requests
from mcp.server.mcpserver.exceptions import ToolError
from synology_api.auth import Authentication
from synology_api.base_api import BaseApi
from synology_api.cloud_sync import CloudSync
from synology_api.core_sys_info import SysInfo
from synology_api.exceptions import SynoBaseException, SynoConnectionError
from synology_api.filestation import FileStation
from synology_api.core_package import Package
from synology_api.log_center import LogCenter
from synology_api.task_scheduler import TaskScheduler

from .config import Settings

log = logging.getLogger(__name__)

# DSM error codes meaning the session is gone: timeout, kicked by a duplicate login, SID not found.
SESSION_ERRORS = {106, 107, 119}

T = TypeVar("T")

# (connect, read) seconds. synology-api sets no timeout, so a NAS that accepts the
# connection but stalls would hang the call, and with it every call queued on the lock.
HTTP_TIMEOUT = (5, 60)


def _with_timeout(send: Callable[..., requests.Response]) -> Callable[..., requests.Response]:
    def wrapper(self: Authentication, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        return send(self, url, *args, **kwargs)

    return wrapper


# Every request the tools make goes through these two (file upload/download, unused here, do not).
Authentication._get = _with_timeout(Authentication._get)
Authentication._post = _with_timeout(Authentication._post)


class DsmError(ToolError):
    """A DSM call failed. ToolError messages are passed through to the MCP client."""


class Apis:
    def __init__(self, s: Settings) -> None:
        args = (s.host, s.port, s.username, s.password)
        kw = dict(secure=s.https, cert_verify=s.verify_ssl, dsm_version=7, debug=False)
        # The first constructor logs in; the rest reuse BaseApi.shared_session.
        self.cloudsync = CloudSync(*args, **kw)
        self.sysinfo = SysInfo(*args, **kw)
        self.filestation = FileStation(*args, interactive_output=False, **kw)
        self.tasks = TaskScheduler(*args, **kw)
        self.logcenter = LogCenter(*args, **kw)
        self.packages = Package(*args, **kw)


class Nas:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._apis: Apis | None = None
        if not settings.verify_ssl:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _connect(self) -> Apis:
        if self._apis is None:
            BaseApi.shared_session = None
            try:
                self._apis = Apis(self.settings)
            except Exception as e:
                # Not only SynoBaseException: the DSM 7 login handshake (get_ik_message) lets raw
                # requests exceptions through, and the MCP SDK would mask those as "Error executing tool".
                BaseApi.shared_session = None
                raise DsmError(f"login to DSM at {self.settings.host}:{self.settings.port} failed: {_describe(e)}") from e
        return self._apis

    def _reset(self) -> None:
        apis, self._apis = self._apis, None
        if apis is not None:
            try:
                apis.cloudsync.logout()
            except Exception:  # the session is probably already dead
                pass
        BaseApi.shared_session = None

    def call(self, fn: Callable[[Apis], T]) -> T:
        """Run ``fn`` against the APIs, logging in again once if the session has expired."""
        with self._lock:
            for attempt in (1, 2):
                apis = self._connect()
                try:
                    return fn(apis)
                except SynoConnectionError as e:
                    self._reset()
                    if attempt == 2:
                        raise DsmError(f"cannot reach DSM: {_describe(e)}") from e
                    log.info("connection error, reconnecting: %s", e)
                except SynoBaseException as e:
                    if getattr(e, "error_code", None) in SESSION_ERRORS and attempt == 1:
                        log.info("DSM session expired (%s), logging in again", e.error_code)
                        self._reset()
                        continue
                    raise DsmError(_describe(e)) from e
                except requests.RequestException as e:
                    # Mostly ReadTimeout, which synology-api lets through unwrapped. Not retried:
                    # the request may have reached DSM, and a retry would double the wait.
                    self._reset()
                    raise DsmError(f"DSM request failed: {e}") from e
        raise AssertionError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._reset()


def _describe(e: Exception) -> str:
    code = getattr(e, "error_code", None)
    msg = getattr(e, "error_message", None) or str(e)
    return f"DSM error {code}: {msg}" if code is not None else msg


def data(resp: Any) -> dict[str, Any]:
    """Unwrap a ``{"success": ..., "data": ...}`` DSM response."""
    if not isinstance(resp, dict):
        raise DsmError(f"unexpected DSM response: {str(resp)[:300]}")
    if not resp.get("success", False):
        raise DsmError(f"DSM call failed: {str(resp)[:300]}")
    return resp.get("data") or {}

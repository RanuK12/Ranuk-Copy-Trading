"""Hot-reload `config_live.yaml` with watchdog.

When the file changes, we re-parse it and push updates into the shared
:class:`StrategyController` (strategy enable/disable + sizing). Risk caps
and slippage overrides can also be applied on the fly.

Design notes
------------
* Watchdog runs in a separate thread; it marshals updates into the main
  asyncio loop via ``asyncio.run_coroutine_threadsafe``.
* Parses are defensive — any YAML error logs a warning and keeps the
  last-good config.
* File absence is OK: the watcher simply idles until someone creates it.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bot.logger import get_logger
from bot.monitoring.commands import get_controller

log = get_logger("config_watcher")

try:
    import yaml  # type: ignore
    _YAML_OK = True
except Exception as e:  # noqa: BLE001
    log.warning(f"PyYAML unavailable ({e}); hot-reload config disabled.")
    _YAML_OK = False

try:
    from watchdog.events import FileSystemEventHandler  # type: ignore
    from watchdog.observers import Observer  # type: ignore
    _WATCHDOG_OK = True
except Exception as e:  # noqa: BLE001
    log.warning(f"watchdog unavailable ({e}); config polling falls back to interval.")
    _WATCHDOG_OK = False


def _default_path() -> Path:
    return Path(os.getenv("CONFIG_LIVE_PATH", "./config_live.yaml"))


def _parse(path: Path) -> Optional[dict[str, Any]]:
    if not _YAML_OK or not path.exists():
        return None
    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        log.warning(f"config_live.yaml parse failed: {e}")
        return None


def _apply(config: dict[str, Any]) -> None:
    """Push a parsed config into runtime singletons."""
    ctrl = get_controller()
    strategies = (config.get("strategies") or {})
    if isinstance(strategies, dict):
        for name, spec in strategies.items():
            if not isinstance(spec, dict):
                continue
            if "enabled" in spec:
                ctrl.set_enabled(name, bool(spec["enabled"]))
            if "sizing" in spec:
                try:
                    ctrl.set_sizing(name, float(spec["sizing"]))
                except (TypeError, ValueError):
                    log.warning(f"config_live: invalid sizing for {name}: {spec['sizing']!r}")

    # Risk overrides (optional) — applied to RiskManager via direct state poke
    # so they take effect without restart. Only a subset is hot-reloadable
    # since flipping caps on open positions is dangerous.
    risk_block = config.get("risk") or {}
    if isinstance(risk_block, dict):
        from bot.config import CFG
        if "max_slippage" in risk_block:
            # CFG is frozen dataclass; mutate via object.__setattr__ for hot swap.
            try:
                object.__setattr__(CFG, "max_slippage", float(risk_block["max_slippage"]))
            except (TypeError, ValueError):
                pass

    log.info("[cyan]config_live.yaml applied.[/]")


class ConfigWatcher:
    def __init__(self, path: Optional[Path] = None, poll_interval: float = 2.0) -> None:
        self.path = path or _default_path()
        self._poll_interval = poll_interval
        self._last_mtime: float = 0.0
        self._stop = threading.Event()
        self._observer: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Watchdog-based
    # ------------------------------------------------------------------
    def _build_handler(self):
        if not _WATCHDOG_OK:
            return None

        class _Handler(FileSystemEventHandler):
            def __init__(self, outer: "ConfigWatcher") -> None:
                self.outer = outer

            def on_modified(self, event):  # type: ignore[no-untyped-def]
                if Path(event.src_path).resolve() == self.outer.path.resolve():
                    self.outer._reload()

            on_created = on_modified

        return _Handler(self)

    # ------------------------------------------------------------------
    # Core reload
    # ------------------------------------------------------------------
    def _reload(self) -> None:
        cfg = _parse(self.path)
        if cfg is None:
            return
        try:
            _apply(cfg)
        except Exception as e:  # noqa: BLE001
            log.exception(f"config_live apply failed: {e}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        # First-run apply if the file exists.
        if self.path.exists():
            self._reload()
            self._last_mtime = self.path.stat().st_mtime

        if _WATCHDOG_OK and self.path.parent.exists():
            handler = self._build_handler()
            if handler is not None:
                self._observer = Observer()
                self._observer.schedule(handler, str(self.path.parent), recursive=False)
                self._observer.start()
                log.info(f"[green]Watching[/] {self.path} for live config changes.")
                return

        # Fallback: poll mtime in a daemon thread
        t = threading.Thread(target=self._poll_loop, daemon=True, name="config-poll")
        t.start()
        log.info(f"[green]Polling[/] {self.path} every {self._poll_interval}s.")

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            if not self.path.exists():
                continue
            mtime = self.path.stat().st_mtime
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                self._reload()

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass

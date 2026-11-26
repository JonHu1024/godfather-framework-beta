"""Cordis 风格微内核（IoC 服务注入 + 副作用作用域 + 热插拔生命周期）。

设计灵感源自 Cordis / DeepSeekHarness：
- Everything is a Plugin：Provider、TaskPack、TrainingBackend、扩展能力都作为 PluginFiber 挂载；
- 声明式依赖（inject）：所需 Service 就绪时自动激活，依赖卸载时级联停用；
- 副作用作用域（EffectScope）：插件注册的路由句柄、任务处理器、事件监听器、子插件全部绑定到当前 Scope，
  卸载或热重载时按逆序自动回收，不留孤儿状态。
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..util import now_iso

logger = logging.getLogger("godfather.cordis")

Disposer = Callable[[], Any]


class Service:
    """IoC 服务基类：提供可选的启停钩子。"""

    service_name: str = ""

    def start(self, ctx: CordisContext) -> None:
        return None

    def stop(self) -> None:
        return None


class EffectScope:
    """副作用作用域：追踪并逆序回收本插件登记的所有清理句柄。"""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self._disposers: list[Disposer] = []
        self._children: list[EffectScope] = []
        self.active = True

    def track(self, disposer: Disposer) -> Disposer:
        if not self.active:
            try:
                disposer()
            except Exception:  # noqa: BLE001
                pass
            return disposer
        self._disposers.append(disposer)
        return disposer

    def add_child(self, child: EffectScope) -> None:
        if self.active:
            self._children.append(child)

    @property
    def count(self) -> int:
        return len(self._disposers)

    def dispose(self) -> list[str]:
        if not self.active:
            return []
        self.active = False
        errors: list[str] = []
        for child in reversed(self._children):
            errors.extend(child.dispose())
        self._children.clear()

        for fn in reversed(self._disposers):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cordis disposer failed for %s: %s", self.owner, exc)
                errors.append(f"{type(exc).__name__}: {exc}")
        self._disposers.clear()
        return errors


@dataclass
class PluginMeta:
    name: str
    display_name: str = ""
    version: str = "0.1.0"
    description: str = ""
    kind: str = "extension"          # taskpack | provider | training | extension
    inject: tuple[str, ...] = ()
    source: str = "builtin"          # builtin | directory | entrypoint
    hot_reloadable: bool = True
    file_path: str = ""
    module_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "version": self.version,
            "description": self.description,
            "kind": self.kind,
            "inject": list(self.inject),
            "source": self.source,
            "hot_reloadable": self.hot_reloadable,
            "file_path": self.file_path,
        }


@dataclass
class PluginFiber:
    meta: PluginMeta
    apply_fn: Callable[[CordisContext], Any]
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    state: str = "pending"           # pending | loading | active | disposing | disposed | failed
    error: str = ""
    mounted_at: str = ""
    parent_name: str = ""
    scope: EffectScope | None = None
    provided_services: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.meta.name

    def to_dict(self, kernel: CordisKernel | None = None) -> dict[str, Any]:
        missing = []
        if kernel is not None:
            missing = [dep for dep in self.meta.inject if not kernel.has_service(dep)]
        return {
            **self.meta.to_dict(),
            "enabled": self.enabled,
            "state": self.state,
            "error": self.error,
            "mounted_at": self.mounted_at,
            "parent_name": self.parent_name,
            "provided_services": sorted(self.provided_services),
            "missing_dependencies": missing,
            "effect_count": self.scope.count if self.scope else 0,
            "config": self.config,
        }


class CordisContext:
    """传给每个插件 `apply(ctx)` 的作用域上下文。"""

    def __init__(
        self,
        kernel: CordisKernel,
        *,
        fiber: PluginFiber | None = None,
        scope: EffectScope | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.kernel = kernel
        self.fiber = fiber
        self.scope = scope or kernel.root_scope
        self.config = dict(config or {})

    # ---- 服务访问与提供 ----
    def has(self, service_name: str) -> bool:
        return self.kernel.has_service(service_name)

    def inject(self, service_name: str) -> Any:
        return self.kernel.get_service(service_name)

    def get(self, service_name: str, default: Any = None) -> Any:
        return self.kernel._services.get(service_name, default)

    def provide(
        self,
        service_name: str,
        instance: Any,
        *,
        stop_fn: Callable[[], Any] | None = None,
    ) -> Disposer:
        owner = self.fiber.name if self.fiber else "root"
        if isinstance(instance, Service):
            instance.start(self)
        disposer = self.kernel._register_service(service_name, instance, owner=owner, stop_fn=stop_fn)
        if self.fiber is not None:
            self.fiber.provided_services.add(service_name)
        return self.effect(disposer)

    # ---- 副作用与事件 ----
    def effect(self, disposer: Disposer) -> Disposer:
        return self.scope.track(disposer)

    def on(self, event: str, listener: Callable[..., Any]) -> Disposer:
        owner = self.fiber.name if self.fiber else "root"
        entry = (owner, listener)
        listeners = self.kernel._listeners.setdefault(event, [])
        listeners.append(entry)

        def _off() -> None:
            current = self.kernel._listeners.get(event)
            if current and entry in current:
                current.remove(entry)

        return self.effect(_off)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> int:
        listeners = list(self.kernel._listeners.get(event, ()))
        for _owner, listener in listeners:
            try:
                listener(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cordis event %s listener failed: %s", event, exc)
        return len(listeners)

    # ---- 挂载子插件 ----
    def plugin(
        self,
        target: Any,
        *,
        name: str = "",
        config: dict[str, Any] | None = None,
        meta: PluginMeta | None = None,
        enable: bool = True,
    ) -> PluginFiber:
        parent_name = self.fiber.name if self.fiber else ""
        return self.kernel.mount(
            target,
            name=name,
            config=config,
            meta=meta,
            enable=enable,
            parent_name=parent_name,
            parent_scope=self.scope,
        )

    def __getattr__(self, item: str) -> Any:
        # 让插件可以像 ctx.taskpacks / ctx.providers / ctx.db 一样自然注入服务
        services = object.__getattribute__(self, "kernel")._services
        if item in services:
            return services[item]
        raise AttributeError(f"CordisContext 无服务或属性 '{item}'")


class CordisKernel(CordisContext):
    """Cordis 风格的微内核与插件协调器。"""

    def __init__(self) -> None:
        self.root_scope = EffectScope("root")
        self._services: dict[str, Any] = {}
        self._service_owners: dict[str, str] = {}
        self._service_stops: dict[str, Callable[[], Any]] = {}
        self._fibers: dict[str, PluginFiber] = {}
        self._listeners: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._load_seq = 0
        super().__init__(self, fiber=None, scope=self.root_scope)

    def has_service(self, service_name: str) -> bool:
        return service_name in self._services

    def get_service(self, service_name: str) -> Any:
        if service_name not in self._services:
            raise KeyError(f"Cordis 服务未就绪：{service_name}")
        return self._services[service_name]

    def list_services(self) -> list[dict[str, Any]]:
        out = []
        for name, instance in self._services.items():
            out.append(
                {
                    "name": name,
                    "owner": self._service_owners.get(name, "root"),
                    "type": type(instance).__name__,
                }
            )
        return out

    def _register_service(
        self,
        name: str,
        instance: Any,
        *,
        owner: str,
        stop_fn: Callable[[], Any] | None = None,
    ) -> Disposer:
        if name in self._services:
            self._revoke_service(name)
        self._services[name] = instance
        self._service_owners[name] = owner
        if stop_fn is not None:
            self._service_stops[name] = stop_fn
        elif isinstance(instance, Service):
            self._service_stops[name] = instance.stop
        self.emit("service:provided", name=name, owner=owner, service=instance)
        self._reconcile_pending()

        def _unregister() -> None:
            if self._services.get(name) is instance:
                self._revoke_service(name)

        return _unregister

    def _revoke_service(self, name: str) -> None:
        if name not in self._services:
            return
        # 先把依赖此服务的活跃插件置回 pending，确保不出现悬空依赖
        dependents = [
            fiber.name
            for fiber in list(self._fibers.values())
            if fiber.state == "active" and name in fiber.meta.inject
        ]
        for dep_name in reversed(dependents):
            self._deactivate_fiber(dep_name, target_state="pending")

        stop_fn = self._service_stops.pop(name, None)
        self._service_owners.pop(name, None)
        instance = self._services.pop(name, None)
        if callable(stop_fn):
            try:
                stop_fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Service %s stop failed: %s", name, exc)
        self.emit("service:revoked", name=name, service=instance)

    def _extract_plugin(self, target: Any, *, name: str = "", meta: PluginMeta | None = None) -> tuple[PluginMeta, Callable[[CordisContext], Any]]:
        apply_fn = getattr(target, "apply", target)
        if not callable(apply_fn):
            raise TypeError(f"插件 {target!r} 不可调用，也未定义 apply(ctx) 方法")

        raw_meta = getattr(target, "PLUGIN_META", None) or getattr(target, "meta", None)
        resolved_name = (
            name
            or (meta.name if meta else "")
            or (raw_meta.get("name") if isinstance(raw_meta, dict) else getattr(raw_meta, "name", ""))
            or getattr(target, "name", "")
            or getattr(apply_fn, "__name__", "plugin")
        )
        raw_inject = (
            (meta.inject if meta else ())
            or (raw_meta.get("inject") if isinstance(raw_meta, dict) else getattr(raw_meta, "inject", ()))
            or getattr(target, "inject", ())
        )
        if isinstance(raw_meta, PluginMeta):
            built = PluginMeta(
                name=resolved_name,
                display_name=raw_meta.display_name,
                version=raw_meta.version,
                description=raw_meta.description,
                kind=raw_meta.kind,
                inject=tuple(raw_inject),
                source=raw_meta.source,
                hot_reloadable=raw_meta.hot_reloadable,
                file_path=raw_meta.file_path,
                module_name=raw_meta.module_name,
            )
        elif meta is not None:
            built = meta
            built.name = resolved_name
            built.inject = tuple(raw_inject)
        else:
            d = raw_meta if isinstance(raw_meta, dict) else {}
            doc = (inspect.getdoc(target) or "").splitlines()[0] if inspect.getdoc(target) else ""
            built = PluginMeta(
                name=resolved_name,
                display_name=str(d.get("display_name") or getattr(target, "display_name", "") or resolved_name),
                version=str(d.get("version") or getattr(target, "version", "0.1.0")),
                description=str(d.get("description") or getattr(target, "description", "") or doc),
                kind=str(d.get("kind") or getattr(target, "kind", "extension")),
                inject=tuple(raw_inject),
                source=str(d.get("source") or "builtin"),
                hot_reloadable=bool(d.get("hot_reloadable", True)),
                file_path=str(d.get("file_path") or ""),
                module_name=str(d.get("module_name") or getattr(target, "__module__", "")),
            )
        return built, apply_fn

    def mount(
        self,
        target: Any,
        *,
        name: str = "",
        config: dict[str, Any] | None = None,
        meta: PluginMeta | None = None,
        enable: bool = True,
        parent_name: str = "",
        parent_scope: EffectScope | None = None,
    ) -> PluginFiber:
        plugin_meta, apply_fn = self._extract_plugin(target, name=name, meta=meta)
        if plugin_meta.name in self._fibers:
            self.unload(plugin_meta.name)

        fiber = PluginFiber(
            meta=plugin_meta,
            apply_fn=apply_fn,
            config=dict(config or {}),
            enabled=enable,
            state="pending",
            parent_name=parent_name,
        )
        self._fibers[plugin_meta.name] = fiber
        if enable:
            self._try_activate_fiber(fiber, parent_scope=parent_scope)
        else:
            fiber.state = "disposed"
        return fiber

    def _try_activate_fiber(self, fiber: PluginFiber, *, parent_scope: EffectScope | None = None) -> bool:
        if not fiber.enabled or fiber.state == "active":
            return False
        missing = [dep for dep in fiber.meta.inject if not self.has_service(dep)]
        if missing:
            fiber.state = "pending"
            fiber.error = ""
            return False

        scope = EffectScope(fiber.name)
        if parent_scope is not None:
            parent_scope.add_child(scope)
        else:
            self.root_scope.add_child(scope)
        fiber.scope = scope
        fiber.state = "loading"
        fiber.error = ""
        fiber.provided_services.clear()

        child_ctx = CordisContext(self, fiber=fiber, scope=scope, config=fiber.config)
        try:
            result = fiber.apply_fn(child_ctx)
            if callable(result):
                scope.track(result)
            fiber.state = "active"
            fiber.mounted_at = now_iso()
            self.emit("plugin:mounted", name=fiber.name, fiber=fiber)
            self._reconcile_pending()
            return True
        except Exception as exc:  # noqa: BLE001
            fiber.state = "failed"
            fiber.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Cordis plugin %s failed to mount", fiber.name)
            scope.dispose()
            fiber.scope = None
            return False

    def _reconcile_pending(self) -> None:
        # 多轮扫描：一个等待中的插件激活并 provide 新服务后，可能继续解锁其他插件
        progressed = True
        while progressed:
            progressed = False
            for fiber in list(self._fibers.values()):
                if fiber.enabled and fiber.state == "pending":
                    if self._try_activate_fiber(fiber):
                        progressed = True

    def _deactivate_fiber(self, name: str, *, target_state: str = "disposed") -> None:
        fiber = self._fibers.get(name)
        if not fiber:
            return
        if fiber.state not in {"active", "loading"}:
            fiber.state = target_state
            return
        fiber.state = "disposing"
        # 先撤销自己提供的服务，以触发依赖方的级联清理
        for svc_name in sorted(list(fiber.provided_services)):
            self._revoke_service(svc_name)
        fiber.provided_services.clear()
        if fiber.scope is not None:
            fiber.scope.dispose()
            fiber.scope = None
        fiber.state = target_state
        self.emit("plugin:disposed", name=name, state=target_state)

    def disable(self, name: str) -> PluginFiber:
        fiber = self._require_fiber(name)
        fiber.enabled = False
        self._deactivate_fiber(name, target_state="disposed")
        return fiber

    def enable(self, name: str) -> PluginFiber:
        fiber = self._require_fiber(name)
        fiber.enabled = True
        if fiber.state in {"disposed", "failed", "pending"}:
            fiber.state = "pending"
            self._try_activate_fiber(fiber)
        return fiber

    def unload(self, name: str) -> None:
        fiber = self._fibers.get(name)
        if not fiber:
            return
        self._deactivate_fiber(name, target_state="disposed")
        self._fibers.pop(name, None)

    def reload(self, name: str, *, config: dict[str, Any] | None = None) -> PluginFiber:
        fiber = self._require_fiber(name)
        if not fiber.meta.hot_reloadable:
            raise ValueError(f"插件 {name} 标记为不可热重载")
        next_config = dict(fiber.config if config is None else config)
        was_enabled = fiber.enabled
        file_path = fiber.meta.file_path
        module_name = fiber.meta.module_name

        self._deactivate_fiber(name, target_state="disposed")
        fiber.config = next_config

        if file_path and Path(file_path).is_file():
            self._fibers.pop(name, None)
            return self.load_plugin_file(Path(file_path), config=next_config, enable=was_enabled)

        if module_name and module_name in sys.modules:
            try:
                module = importlib.reload(sys.modules[module_name])
                if hasattr(module, "apply"):
                    meta, apply_fn = self._extract_plugin(module, name=name, meta=fiber.meta)
                    fiber.meta = meta
                    fiber.apply_fn = apply_fn
            except Exception as exc:  # noqa: BLE001
                fiber.state = "failed"
                fiber.error = f"热重载失败：{exc}"
                return fiber

        fiber.enabled = was_enabled
        if was_enabled:
            fiber.state = "pending"
            self._try_activate_fiber(fiber)
        return fiber

    def load_plugin_file(
        self,
        path: Path,
        *,
        config: dict[str, Any] | None = None,
        enable: bool = True,
    ) -> PluginFiber:
        resolved = Path(path).resolve()
        importlib.invalidate_caches()
        self._load_seq += 1
        mod_name = f"godfather_ext_{resolved.stem}_{self._load_seq}_{abs(hash(str(resolved))) % 100000}"
        spec = importlib.util.spec_from_file_location(mod_name, str(resolved))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载插件模块：{resolved}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        # 从磁盘直接编译最新源码执行，绕过 NTFS/FAT 同秒内 .pyc 时间戳缓存导致的热重载滞后
        source = resolved.read_text(encoding="utf-8")
        exec(compile(source, str(resolved), "exec"), module.__dict__)

        meta, apply_fn = self._extract_plugin(module)
        meta.source = "directory"
        meta.file_path = str(resolved)
        meta.module_name = mod_name
        return self.mount(
            apply_fn,
            name=meta.name,
            config=config,
            meta=meta,
            enable=enable,
        )

    def discover_directory(self, directory: Path) -> list[PluginFiber]:
        """从插件目录自动发现外部热插拔插件（支持 *.py 或 */plugin.py）。"""
        if not directory or not directory.exists():
            return []
        loaded: list[PluginFiber] = []
        candidates: list[Path] = []
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith("_"):
                continue
            if entry.is_file() and entry.suffix == ".py":
                candidates.append(entry)
            elif entry.is_dir() and (entry / "plugin.py").is_file():
                candidates.append(entry / "plugin.py")
        for file_path in candidates:
            try:
                loaded.append(self.load_plugin_file(file_path))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load external plugin %s: %s", file_path, exc)
        return loaded

    def _require_fiber(self, name: str) -> PluginFiber:
        if name not in self._fibers:
            raise KeyError(f"未找到插件：{name}")
        return self._fibers[name]

    def get_fiber(self, name: str) -> PluginFiber | None:
        return self._fibers.get(name)

    def list_fibers(self) -> list[dict[str, Any]]:
        return [fiber.to_dict(self) for fiber in self._fibers.values()]

    def shutdown(self) -> None:
        for name in reversed(list(self._fibers.keys())):
            self._deactivate_fiber(name, target_state="disposed")
        for svc_name in reversed(list(self._services.keys())):
            self._revoke_service(svc_name)
        self.root_scope.dispose()

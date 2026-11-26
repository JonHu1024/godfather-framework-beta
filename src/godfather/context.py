"""应用上下文：以 Cordis 微内核装配数据库、产物仓库、设置、队列、服务与热插拔插件。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .cordis import CordisKernel, TaskPackRegistry, TrainingBackendRegistry
from .db import Database
from .jobs import JobQueue
from .providers.registry import ProviderRegistry, TeacherService
from .settings import Settings
from .storage import ArtifactStore


@dataclass
class AppContext:
    db: Database
    store: ArtifactStore
    settings: Settings
    queue: JobQueue
    registry: ProviderRegistry
    teacher: TeacherService
    kernel: CordisKernel = field(default_factory=CordisKernel)
    taskpacks: TaskPackRegistry = field(default_factory=TaskPackRegistry)
    training_backends: TrainingBackendRegistry = field(default_factory=TrainingBackendRegistry)

    @classmethod
    def create(cls, settings: Settings) -> AppContext:
        from .training.backends import BACKENDS

        settings.ensure_dirs()
        db = Database(settings.db_path)
        db.initialize()
        store = ArtifactStore(db, settings.artifacts_dir)
        settings.artifact_store = store  # 供 JobContext 使用
        queue = JobQueue(db, settings)
        registry = ProviderRegistry(settings)
        teacher = TeacherService(db, settings, registry)

        kernel = CordisKernel()
        taskpacks = TaskPackRegistry()
        training_backends = TrainingBackendRegistry(BACKENDS)

        ctx = cls(
            db=db,
            store=store,
            settings=settings,
            queue=queue,
            registry=registry,
            teacher=teacher,
            kernel=kernel,
            taskpacks=taskpacks,
            training_backends=training_backends,
        )
        # 注册核心 Cordis 服务，供所有内置及外部热插拔插件声明式 inject
        kernel.provide("app", ctx)
        kernel.provide("db", db)
        kernel.provide("store", store)
        kernel.provide("settings", settings)
        kernel.provide("queue", queue)
        kernel.provide("providers", registry)
        kernel.provide("teacher", teacher)
        kernel.provide("taskpacks", taskpacks)
        kernel.provide("training_backends", training_backends)

        from .cordis.builtins import mount_builtin_plugins

        mount_builtin_plugins(ctx)
        return ctx

    # ---- 便捷查询 ----
    def project(self, project_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def policy_for(self, project_id: str):
        from .policy import engine as policy_engine

        return policy_engine.load_active_policy(self.db, project_id)

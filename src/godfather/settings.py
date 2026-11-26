"""配置加载：config/config.yaml（可选）→ 环境变量覆盖 → 默认值。"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8760
    lan_bind: bool = False
    lan_host: str = "0.0.0.0"
    pairing_code_ttl_seconds: int = 600


@dataclass
class StorageSettings:
    data_dir: str = "./data"
    # 单个上传文件的大小上限（防止局域网开放时被大文件打爆内存）
    max_upload_bytes: int = 32 * 1024 * 1024
    # 模板覆盖目录：放同名文件即可覆盖内置模板（文档承诺的可扩展点，
    # 以前 load_template 的 overrides_dir 从来没人传 → 这条承诺是空的）
    templates_dir: str = "./config/templates"
    # 外部热插拔 Cordis 插件目录
    plugins_dir: str = "./plugins"


@dataclass
class WorkerSettings:
    mode: str = "embedded"
    poll_interval_seconds: float = 0.5
    lease_seconds: int = 120
    # 等待 GPU 资源锁的最长秒数（训练 / 导出 / 评测共用同一把锁）
    gpu_wait_seconds: int = 900
    max_attempts: int = 3
    max_jobs: int = 2


@dataclass
class OllamaSettings:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: int = 180
    teacher_model: str = "qwen2.5:7b"
    # 采样种子：固定它，同一输入 + 同一权重 → 同一输出（可复现、可追溯）。
    # DeepInfer 把"确定性采样"当成推理框架的基本承诺。
    seed: int = 42


@dataclass
class OpenAICompatibleSettings:
    enabled: bool = False
    display_name: str = "远程 OpenAI 兼容 API"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    teacher_model: str = "deepseek-chat"
    timeout_seconds: int = 180
    temperature: float = 0.2
    max_tokens: int = 4096
    strip_think_tags: bool = True
    pricing_known: bool = False
    input_price_per_1m_cents: float = 0.0
    output_price_per_1m_cents: float = 0.0


@dataclass
class ManualSettings:
    enabled: bool = True


@dataclass
class ProviderSettings:
    default_teacher_provider: str = "ollama"
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    openai_compatible: OpenAICompatibleSettings = field(default_factory=OpenAICompatibleSettings)
    manual: ManualSettings = field(default_factory=ManualSettings)


@dataclass
class BudgetSettings:
    paid_calls_enabled: bool = False
    total_cents: int = 0
    per_call_cents: int = 0
    max_calls: int = 200
    max_seconds: int = 3600


@dataclass
class LatexSettings:
    engine: str = "xelatex"
    binary: str = ""
    candidates: list[str] = field(default_factory=lambda: ["xelatex"])
    timeout_seconds: int = 180
    passes: int = 2
    # 字体方案：auto（Windows 用 ctex windows 字体集，其它平台用 fandol）/ windows / fandol / manual
    fontset: str = "auto"
    cjk_main_font: str = "Noto Serif SC"
    cjk_sans_font: str = "Noto Sans SC"
    cjk_mono_font: str = "Noto Sans Mono CJK SC"


@dataclass
class ExportSettings:
    """模型导出（§9.2）所需路径。"""

    llama_cpp_dir: str = ""          # llama.cpp 仓库目录（含 convert_hf_to_gguf.py）
    ollama_bin: str = "ollama"
    quantize_method: str = "q4_k_m"
    model_name_prefix: str = "godfather-latex"


@dataclass
class PandocSettings:
    binary: str = "pandoc"
    markdown_extensions: list[str] = field(default_factory=lambda: ["tex_math_dollars", "pipe_tables"])


@dataclass
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    providers: ProviderSettings = field(default_factory=ProviderSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    latex: LatexSettings = field(default_factory=LatexSettings)
    pandoc: PandocSettings = field(default_factory=PandocSettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    config_path: str = ""
    root_dir: str = str(PROJECT_ROOT)

    # ---- 派生路径 ----
    @property
    def data_dir(self) -> Path:
        raw = Path(self.storage.data_dir)
        return raw if raw.is_absolute() else (Path(self.root_dir) / raw)

    @property
    def templates_dir(self) -> Path:
        """模板覆盖目录（相对 root_dir 解析，不依赖进程 CWD）。

        以前是相对**进程工作目录**解析的：换个目录启动服务，
        "在 config/templates/ 放同名文件覆盖模板"这条承诺就静默失效。
        """
        raw = Path(self.storage.templates_dir)
        return raw if raw.is_absolute() else (Path(self.root_dir) / raw)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "godfather.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def plugins_dir(self) -> Path:
        raw = Path(self.storage.plugins_dir)
        return raw if raw.is_absolute() else (Path(self.root_dir) / raw)

    @property
    def runtime_config_path(self) -> Path:
        return self.data_dir / "runtime_config.json"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.artifacts_dir, self.work_dir, self.exports_dir):
            path.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["config_path"] = self.config_path
        # 脱敏：永不把明文 api_key 序列化到接口响应中
        oa = (data.get("providers") or {}).get("openai_compatible")
        if isinstance(oa, dict):
            raw_key = oa.pop("api_key", "") or ""
            oa["has_api_key"] = bool(raw_key)
            oa["api_key_masked"] = (
                f"{raw_key[:4]}…{raw_key[-4:]}" if len(raw_key) > 8 else ("****" if raw_key else "")
            )
        return data


def _apply(section: Any, raw: dict[str, Any]) -> Any:
    if not isinstance(raw, dict):
        return section
    known = {f for f in section.__dataclass_fields__}
    for key, value in raw.items():
        if key in known:
            current = getattr(section, key)
            if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
                setattr(section, key, _apply(current, value))
            else:
                setattr(section, key, value)
    return section


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = Settings()
    _apply(settings.server, raw.get("server", {}))
    _apply(settings.storage, raw.get("storage", {}))
    _apply(settings.worker, raw.get("worker", {}))
    _apply(settings.providers, raw.get("providers", {}))
    _apply(settings.budget, raw.get("budget", {}))
    _apply(settings.latex, raw.get("latex", {}))
    _apply(settings.pandoc, raw.get("pandoc", {}))
    _apply(settings.export, raw.get("export", {}))

    # 环境变量覆盖（便于 Windows 上一键脚本）
    env_map = {
        "GODFATHER_HOST": ("server", "host", str),
        "GODFATHER_PORT": ("server", "port", int),
        "GODFATHER_LAN_BIND": ("server", "lan_bind", lambda v: v.lower() in {"1", "true", "yes"}),
        "GODFATHER_DATA_DIR": ("storage", "data_dir", str),
        "GODFATHER_WORKER_MODE": ("worker", "mode", str),
        "GODFATHER_LATEX_BINARY": ("latex", "binary", str),
        "GODFATHER_LATEX_ENGINE": ("latex", "engine", str),
        "GODFATHER_OLLAMA_BASE_URL": ("providers", "ollama", "base_url", str),
        "GODFATHER_OPENAI_BASE_URL": ("providers", "openai_compatible", "base_url", str),
        "GODFATHER_OPENAI_API_KEY": ("providers", "openai_compatible", "api_key", str),
        "GODFATHER_OPENAI_MODEL": ("providers", "openai_compatible", "teacher_model", str),
    }
    for env_key, spec in env_map.items():
        if env_key not in os.environ:
            continue
        raw_value = os.environ[env_key]
        if len(spec) == 4:
            section, sub, field_name, caster = spec
            target = getattr(getattr(settings, section), sub)
            setattr(target, field_name, caster(raw_value))
        else:
            section, field_name, caster = spec
            setattr(getattr(settings, section), field_name, caster(raw_value))

    # 兼容 OPENAI_API_KEY 环境变量（若未专门配置 GODFATHER_OPENAI_API_KEY）
    if not settings.providers.openai_compatible.api_key and os.environ.get("OPENAI_API_KEY"):
        settings.providers.openai_compatible.api_key = os.environ["OPENAI_API_KEY"]

    settings.config_path = str(path)
    load_runtime_overrides(settings)
    return settings


def load_runtime_overrides(settings: Settings) -> dict[str, Any]:
    """从 `data_dir/runtime_config.json` 读取 UI 运行时保存的配置并合入 Settings。"""
    import json

    override_path = settings.runtime_config_path
    if not override_path.exists():
        return {}
    try:
        raw = json.loads(override_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    for section_name in ("providers", "budget", "latex", "export"):
        if isinstance(raw.get(section_name), dict):
            _apply(getattr(settings, section_name), raw[section_name])
    return raw


def save_runtime_overrides(settings: Settings, patch: dict[str, Any]) -> dict[str, Any]:
    """持久化用户在界面中修改的运行期配置（如远程 API 接入、预算、LaTeX 设置）。"""
    import json

    settings.ensure_dirs()
    current = {}
    if settings.runtime_config_path.exists():
        try:
            loaded = json.loads(settings.runtime_config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except Exception:  # noqa: BLE001
            current = {}
    merged = _deep_merge(current, patch)
    for section_name in ("providers", "budget", "latex", "export"):
        if isinstance(merged.get(section_name), dict):
            _apply(getattr(settings, section_name), merged[section_name])

    tmp = settings.runtime_config_path.with_name(settings.runtime_config_path.name + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    tmp.replace(settings.runtime_config_path)
    if os.name != "nt":
        try:
            settings.runtime_config_path.chmod(0o600)
        except OSError:
            pass
    return merged

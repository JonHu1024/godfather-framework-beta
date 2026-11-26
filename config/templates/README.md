# 模板覆盖目录

把与内置模板**同名**的文件放在这里，即可覆盖内置模板。
例如 `preamble_zh_ctex.tex` 会替换 `src/godfather/taskpacks/latex/templates.py`
里的同名内置模板。

- 生效范围：`storage.templates_dir` 配置的目录（默认 `config/templates/`）
- 相对路径按**项目根目录**解析，不依赖服务启动时的工作目录
- 覆盖只影响导言区与标题块；正文转换逻辑不受影响

内置模板名见 `src/godfather/taskpacks/latex/templates.py` 的 `DEFAULT_TEMPLATES`。

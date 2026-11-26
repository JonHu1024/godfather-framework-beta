# Cordis 外部热插拔插件目录 (`plugins/`)

将自定义 `.py` 插件（或 `<plugin_name>/plugin.py` 目录）放入此目录后，可在浏览器 **「Cordis 插件」** 页点击 **「扫描并挂载外部 ./plugins/ 插件」** 热装载，无需重启服务器。

- 以 `_` 开头的文件（例如 `_example_custom_taskpack.py`）会被扫描器跳过，作为参考模板保留。
- 所有注册动作（`taskpacks.register`、`queue.register_disposable`、`providers.register`、`ctx.on`、`ctx.provide`）建议一律套上 `ctx.effect(...)`，这样卸载或热重载时 Cordis 会自动逆序回收资源。

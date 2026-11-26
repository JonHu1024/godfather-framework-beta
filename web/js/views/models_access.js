// 模型接入中心 (BUG-03)：配置默认教师提供方、OpenAI 兼容远程 API、Ollama 显存释放、预算与 LaTeX 引擎
import { api } from "../api.js";
import { clear, h, pill, showError, toast } from "../ui.js";

export async function render() {
  const root = h("div", { class: "stack-lg", "data-testid": "view-models-access" });
  let data;
  try {
    data = await api.modelsAccess();
  } catch (error) {
    root.append(
      h("div", { class: "page-head" }, h("h1", {}, "模型接入与运行参数中心")),
      h("div", { class: "alert danger" }, `无法加载配置（注意：仅限运行宿主本机访问密钥配置）：${error.message}`)
    );
    return root;
  }

  const providers = data.providers || {};
  const oa = providers.openai_compatible || {};
  const ollama = providers.ollama || {};
  const manual = providers.manual || {};
  const budget = data.budget || {};
  const latex = data.latex || {};
  const exportCfg = data.export || {};
  const presets = data.presets || [];

  // ---- 1. 默认提供方 ----
  const defaultProviderSelect = h(
    "select",
    { "data-testid": "ma-default-provider" },
    [
      ["ollama", "Ollama 本地推理 (ollama)"],
      ["openai_compatible", "远程 OpenAI 兼容 API (openai_compatible)"],
      ["manual_import", "离线手工导入 (manual_import)"],
    ].map(([val, text]) => h("option", { value: val, selected: data.default_teacher_provider === val }, text))
  );

  // ---- 2. OpenAI 兼容远程 API 表单 ----
  const oaEnabled = h("input", { type: "checkbox", checked: Boolean(oa.enabled), "data-testid": "ma-oa-enabled" });
  const oaDisplayName = h("input", { value: oa.display_name || "远程 OpenAI 兼容 API", "data-testid": "ma-oa-display-name" });
  const oaBaseUrl = h("input", { value: oa.base_url || "https://api.deepseek.com/v1", placeholder: "https://api.deepseek.com/v1", "data-testid": "ma-oa-base-url" });
  const oaApiKey = h("input", {
    type: "password",
    value: "",
    placeholder: oa.has_api_key ? `已保存（${oa.api_key_masked}），留空保留原密钥` : "输入 API Key (sk-...)",
    autocomplete: "off",
    "data-testid": "ma-oa-api-key",
  });
  const oaModel = h("input", {
    value: oa.teacher_model || "deepseek-chat",
    list: "oa-models-datalist",
    placeholder: "例如 deepseek-chat / MiniMax-M2.5 / qwen-plus",
    "data-testid": "ma-oa-model",
  });
  const oaDatalist = h("datalist", { id: "oa-models-datalist" });
  const oaMaxTokens = h("input", { type: "number", value: String(oa.max_tokens || 4096), min: "128", max: "65536", "data-testid": "ma-oa-max-tokens" });
  const oaStripThink = h("input", { type: "checkbox", checked: oa.strip_think_tags !== false, "data-testid": "ma-oa-strip-think" });
  const oaPricingKnown = h("input", { type: "checkbox", checked: Boolean(oa.pricing_known), "data-testid": "ma-oa-pricing-known" });
  const oaInPrice = h("input", { type: "number", step: "0.01", value: String(oa.input_price_per_1m_cents || 0), "data-testid": "ma-oa-in-price" });
  const oaOutPrice = h("input", { type: "number", step: "0.01", value: String(oa.output_price_per_1m_cents || 0), "data-testid": "ma-oa-out-price" });

  const presetWrap = h(
    "div",
    { class: "row", style: { gap: "6px" } },
    h("span", { class: "muted", style: { fontSize: "12px" } }, "一键预设："),
    presets.map((p) =>
      h(
        "button",
        {
          type: "button",
          class: "btn sm",
          title: p.notes,
          "data-testid": `ma-preset-${p.id}`,
          onClick: () => {
            oaEnabled.checked = true;
            oaDisplayName.value = p.label;
            oaBaseUrl.value = p.base_url;
            if (p.models?.length) {
              oaModel.value = p.models[0];
              clear(oaDatalist);
              for (const m of p.models) oaDatalist.append(h("option", { value: m }));
            }
            toast(`已填入「${p.label}」预设：请填写 API Key 后点击保存与测试`, "ok");
          },
        },
        p.label
      )
    )
  );

  const testResultBox = h("div", { class: "alert hidden", "data-testid": "ma-test-result" });

  async function runConnectionTest(pingChat) {
    testResultBox.className = "alert info";
    testResultBox.textContent = pingChat ? "正在发送真实测试对话…" : "正在探测接口与模型列表…";
    try {
      const res = await api.testProvider({
        provider: "openai_compatible",
        base_url: oaBaseUrl.value.trim(),
        api_key: oaApiKey.value.trim() || undefined,
        model: oaModel.value.trim(),
        ping_chat: pingChat,
      });
      if (res.models?.length) {
        clear(oaDatalist);
        for (const m of res.models) oaDatalist.append(h("option", { value: m }));
      }
      testResultBox.className = `alert ${res.ok ? "ok" : "danger"}`;
      testResultBox.textContent =
        `${res.message}` +
        (res.chat_test?.content ? `｜返回：${res.chat_test.content}` : "") +
        (res.models?.length ? `｜已填充 ${res.models.length} 个可用模型到下拉建议` : "");
    } catch (error) {
      testResultBox.className = "alert danger";
      testResultBox.textContent = `测试失败：${error.message}`;
    }
  }

  // ---- 3. Ollama 与显存管理 ----
  const olEnabled = h("input", { type: "checkbox", checked: ollama.enabled !== false, "data-testid": "ma-ol-enabled" });
  const olBaseUrl = h("input", { value: ollama.base_url || "http://127.0.0.1:11434", "data-testid": "ma-ol-base-url" });
  const olModel = h("input", {
    value: ollama.teacher_model || "qwen2.5:7b",
    list: "ol-models-datalist",
    "data-testid": "ma-ol-model",
  });
  const olDatalist = h("datalist", { id: "ol-models-datalist" }, (data.ollama_models || []).map((m) => h("option", { value: m })));
  const olSeed = h("input", { type: "number", value: String(ollama.seed ?? 42), "data-testid": "ma-ol-seed" });
  const runningWrap = h("div", { class: "row", "data-testid": "ma-ol-running" });

  function renderRunningOllama(names) {
    clear(runningWrap);
    if (!names?.length) {
      runningWrap.append(h("span", { class: "muted", style: { fontSize: "12.5px" } }, "当前无常驻 Ollama 显存模型"));
      return;
    }
    runningWrap.append(h("span", { class: "muted", style: { fontSize: "12.5px" } }, "显存常驻模型："));
    for (const n of names) runningWrap.append(pill(n, "warn"));
    runningWrap.append(
      h(
        "button",
        {
          type: "button",
          class: "btn danger sm",
          "data-testid": "ma-ol-unload",
          onClick: async () => {
            const out = await api.unloadOllama("");
            toast(out.ok ? `已卸载释放显存：${(out.unloaded || []).join(", ") || "完成"}` : `卸载部分失败：${(out.errors || []).join("; ")}`, out.ok ? "ok" : "warn");
            const fresh = await api.modelsAccess();
            renderRunningOllama(fresh.running_ollama_models || []);
          },
        },
        "一键释放 Ollama 显存"
      )
    );
  }
  renderRunningOllama(data.running_ollama_models || []);

  // ---- 4. 预算闸门 ----
  const paidEnabled = h("input", { type: "checkbox", checked: Boolean(budget.paid_calls_enabled), "data-testid": "ma-budget-paid" });
  const totalCents = h("input", { type: "number", min: "0", value: String(budget.total_cents || 0), "data-testid": "ma-budget-total" });
  const perCallCents = h("input", { type: "number", min: "0", value: String(budget.per_call_cents || 0), "data-testid": "ma-budget-percall" });
  const maxCalls = h("input", { type: "number", min: "1", value: String(budget.max_calls || 200), "data-testid": "ma-budget-maxcalls" });
  const maxSeconds = h("input", { type: "number", min: "10", value: String(budget.max_seconds || 3600), "data-testid": "ma-budget-maxsec" });

  // ---- 5. LaTeX 与导出设置 ----
  const latexBinary = h("input", { value: latex.binary || "", placeholder: "自动查找 PATH 中的 xelatex，或填完整路径", "data-testid": "ma-latex-binary" });
  const latexFontset = h(
    "select",
    { "data-testid": "ma-latex-fontset" },
    ["auto", "windows", "fandol", "manual"].map((f) => h("option", { value: f, selected: (latex.fontset || "auto") === f }, f))
  );
  const latexPasses = h("input", { type: "number", min: "1", max: "5", value: String(latex.passes || 2), "data-testid": "ma-latex-passes" });
  const llamaCppDir = h("input", { value: exportCfg.llama_cpp_dir || "", placeholder: "例如 D:/Tools/llama.cpp", "data-testid": "ma-export-llamacpp" });

  async function saveAll(clearKey = false) {
    try {
      await api.saveModelsAccess({
        default_teacher_provider: defaultProviderSelect.value,
        clear_api_key: clearKey,
        openai_compatible: {
          enabled: oaEnabled.checked,
          display_name: oaDisplayName.value.trim(),
          base_url: oaBaseUrl.value.trim(),
          api_key: oaApiKey.value.trim(),
          teacher_model: oaModel.value.trim(),
          max_tokens: Number(oaMaxTokens.value || 4096),
          strip_think_tags: oaStripThink.checked,
          pricing_known: oaPricingKnown.checked,
          input_price_per_1m_cents: Number(oaInPrice.value || 0),
          output_price_per_1m_cents: Number(oaOutPrice.value || 0),
        },
        ollama: {
          enabled: olEnabled.checked,
          base_url: olBaseUrl.value.trim(),
          teacher_model: olModel.value.trim(),
          seed: Number(olSeed.value ?? 42),
        },
        manual: { enabled: Boolean(manual.enabled !== false) },
        budget: {
          paid_calls_enabled: paidEnabled.checked,
          total_cents: Number(totalCents.value || 0),
          per_call_cents: Number(perCallCents.value || 0),
          max_calls: Number(maxCalls.value || 200),
          max_seconds: Number(maxSeconds.value || 3600),
        },
        latex: {
          binary: latexBinary.value.trim(),
          fontset: latexFontset.value,
          passes: Number(latexPasses.value || 2),
        },
        export: {
          llama_cpp_dir: llamaCppDir.value.trim(),
        },
      });
      toast("配置已保存并热生效到当前服务（密钥已脱敏落盘）", "ok");
      window.dispatchEvent(new CustomEvent("gf:refresh"));
    } catch (error) {
      showError(error);
    }
  }

  root.append(
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        {},
        h("h1", {}, "模型接入"),
        h("p", {}, "配置本地 Ollama、远程 OpenAI 兼容 API 与预算限额，保存后即时生效。")
      ),
      h(
        "div",
        { class: "row" },
        h("label", { class: "row" }, h("span", { class: "muted" }, "默认提供方"), defaultProviderSelect),
        h("button", { type: "button", class: "btn primary", "data-testid": "ma-save-all", onClick: () => saveAll(false) }, "保存配置")
      )
    )
  );

  // 提醒预算开关与远程 API 联动
  if (oaEnabled.checked && oaPricingKnown.checked && !paidEnabled.checked) {
    root.append(
      h(
        "div",
        { class: "alert warn" },
        "你启用了远程付费单价核算，但「允许外部付费 API 调用」开关目前处于关闭状态，正式运行教师生成前请在下方开启。"
      )
    );
  }

  root.append(
    h(
      "div",
      { class: "grid cols-2" },
      // 远程 API 卡片
      h(
        "section",
        { class: "card stack", "data-testid": "ma-card-openai" },
        h(
          "div",
          { class: "row-between" },
          h("h2", {}, "远程 OpenAI 兼容 API（DeepSeek / MiniMax / Qwen 等）"),
          h("label", { class: "row" }, oaEnabled, h("span", {}, "启用此提供方"))
        ),
        presetWrap,
        oaDatalist,
        h("div", { class: "grid cols-2" }, h("label", { class: "stack" }, h("span", { class: "muted" }, "显示名称"), oaDisplayName), h("label", { class: "stack" }, h("span", { class: "muted" }, "默认模型 ID"), oaModel)),
        h("label", { class: "stack" }, h("span", { class: "muted" }, "API Base URL (通常以 /v1 结尾)"), oaBaseUrl),
        h(
          "div",
          { class: "row-between", style: { alignItems: "flex-end" } },
          h("label", { class: "stack", style: { flex: "1" } }, h("span", { class: "muted" }, "API Key（加密不回显）"), oaApiKey),
          oa.has_api_key
            ? h("button", { type: "button", class: "btn danger sm", onClick: () => saveAll(true) }, "清空密钥")
            : null
        ),
        h(
          "div",
          { class: "grid cols-2" },
          h("label", { class: "stack" }, h("span", { class: "muted" }, "Max Output Tokens"), oaMaxTokens),
          h("label", { class: "row", style: { alignSelf: "end", paddingBottom: "8px" } }, oaStripThink, h("span", {}, "自动剥离 <think> 思考段"))
        ),
        h(
          "div",
          { class: "subcard stack" },
          h("label", { class: "row" }, oaPricingKnown, h("span", {}, "开启按百万 Token 已知单价计费（美分 ¢；未勾选则记为「未知用量」仅受调用次数硬限）")),
          h("div", { class: "grid cols-2" }, h("label", { class: "stack" }, h("span", { class: "muted" }, "输入单价 (¢ / 1M tok)"), oaInPrice), h("label", { class: "stack" }, h("span", { class: "muted" }, "输出单价 (¢ / 1M tok)"), oaOutPrice))
        ),
        h(
          "div",
          { class: "row" },
          h("button", { type: "button", class: "btn sm", "data-testid": "ma-btn-test-models", onClick: () => runConnectionTest(false) }, "探测模型列表"),
          h("button", { type: "button", class: "btn primary sm", "data-testid": "ma-btn-test-chat", onClick: () => runConnectionTest(true) }, "发送测试对话 Ping")
        ),
        testResultBox
      ),

      // 右侧：Ollama + 预算 + 工具路径
      h(
        "div",
        { class: "stack-lg" },
        h(
          "section",
          { class: "card stack", "data-testid": "ma-card-ollama" },
          h(
            "div",
            { class: "row-between" },
            h("h2", {}, "Ollama 本地推理"),
            h("label", { class: "row" }, olEnabled, h("span", {}, "启用 Ollama"))
          ),
          olDatalist,
          h("div", { class: "grid cols-2" }, h("label", { class: "stack" }, h("span", { class: "muted" }, "Ollama 服务地址"), olBaseUrl), h("label", { class: "stack" }, h("span", { class: "muted" }, "默认教师模型"), olModel)),
          h("label", { class: "stack" }, h("span", { class: "muted" }, "确定性采样种子 (Seed)"), olSeed),
          runningWrap
        ),
        h(
          "section",
          { class: "card stack", "data-testid": "ma-card-budget" },
          h(
            "div",
            { class: "row-between" },
            h("h2", {}, "安全预算与硬上限闸门"),
            h("label", { class: "row" }, paidEnabled, h("span", {}, "允许已定价付费调用"))
          ),
          h(
            "div",
            { class: "grid cols-2" },
            h("label", { class: "stack" }, h("span", { class: "muted" }, "总预算上限（分/¢，0 表示不按金额限制）"), totalCents),
            h("label", { class: "stack" }, h("span", { class: "muted" }, "单次调用预留上限"), perCallCents),
            h("label", { class: "stack" }, h("span", { class: "muted" }, "最大累计调用次数"), maxCalls),
            h("label", { class: "stack" }, h("span", { class: "muted" }, "最大累计耗时 (秒)"), maxSeconds)
          )
        ),
        h(
          "section",
          { class: "card stack", "data-testid": "ma-card-toolchain" },
          h("h2", {}, "LaTeX 编译器与 GGUF 导出路径"),
          h("div", { class: "grid cols-2" }, h("label", { class: "stack" }, h("span", { class: "muted" }, "字体方案 (fontset)"), latexFontset), h("label", { class: "stack" }, h("span", { class: "muted" }, "编译遍数 (passes)"), latexPasses)),
          h("label", { class: "stack" }, h("span", { class: "muted" }, "XeLaTeX 路径（留空自动查找）"), latexBinary),
          h("label", { class: "stack" }, h("span", { class: "muted" }, "llama.cpp 仓库目录（导出 GGUF 用）"), llamaCppDir)
        )
      )
    )
  );

  return root;
}

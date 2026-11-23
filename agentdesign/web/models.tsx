import { useState } from "react";
import { type Profile, type Project, type Workspace, json, post, roleLabel, roles, useQuery } from "./api";
import { useI18n } from "./i18n";
import { Button, Empty, ErrorNote, Field, Head, Modal, Section, Status, useAction } from "./ui";

type Role = keyof typeof roles;

const roleMeta: Record<Role, { en: string; zh: string }> = {
  extractor: { en: "Extract entities & relation graph", zh: "抽取实体与关系图谱（建议关闭思考）" },
  selector: { en: "Select best context evidence", zh: "从候选池筛选最优上下文证据" },
  translator: { en: "Draft localized translations", zh: "结合上下文生成游戏风格初译" },
  reviewer: { en: "Verify evidence via read-only tools", zh: "只读工具核验证据并做出审校判定" },
  repairer: { en: "Fix drafts via checks & review", zh: "按硬检查与审校意见定向修正" },
  intent: { en: "Parse natural-language task plans", zh: "解析自然语言指令为结构化计划" },
};

type Preset = {
  id: string;
  en: string;
  zh: string;
  api_format: "chat_completions" | "responses";
  thinking: "auto" | "disabled";
  name: string;
  model: string;
  base_url: string;
  timeout_seconds: number;
};

const PRESETS: Preset[] = [
  {
    id: "deepseek",
    en: "DeepSeek",
    zh: "DeepSeek",
    api_format: "chat_completions",
    thinking: "disabled",
    name: "DeepSeek Chat",
    model: "deepseek-chat",
    base_url: "https://api.deepseek.com/v1",
    timeout_seconds: 90,
  },
  {
    id: "gateway",
    en: "Tokendance Gateway",
    zh: "Tokendance 网关",
    api_format: "responses",
    thinking: "disabled",
    name: "DeepSeek Flash (Gateway)",
    model: "deepseek-v4.1-flash-1017",
    base_url: "https://tokendance.space/gateway/v1",
    timeout_seconds: 75,
  },
  {
    id: "qwen",
    en: "Qwen / Compatible",
    zh: "通义千问 / 兼容",
    api_format: "chat_completions",
    thinking: "disabled",
    name: "Qwen Plus",
    model: "qwen-plus",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    timeout_seconds: 90,
  },
  {
    id: "ollama",
    en: "Local Ollama",
    zh: "本地 Ollama",
    api_format: "chat_completions",
    thinking: "disabled",
    name: "Ollama Qwen3-8B",
    model: "qwen3:8b",
    base_url: "http://127.0.0.1:11434/v1",
    timeout_seconds: 120,
  },
  {
    id: "vllm",
    en: "Local vLLM",
    zh: "本地 vLLM",
    api_format: "chat_completions",
    thinking: "disabled",
    name: "Local vLLM",
    model: "Qwen/Qwen3-8B",
    base_url: "http://127.0.0.1:8000/v1",
    timeout_seconds: 120,
  },
];

function isLoopbackUrl(raw: string) {
  try {
    const host = new URL(raw).hostname;
    return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
  } catch {
    return false;
  }
}

function ProfileModal({
  initial, backendReady, close, saved,
}: {
  initial?: Profile; backendReady: boolean; close: () => void; saved: () => void;
}) {
  const { locale, t } = useI18n();
  const isEdit = !!initial;
  const [apiFormat, setApiFormat] = useState<"chat_completions" | "responses">(initial?.api_format ?? "chat_completions");
  const [thinking, setThinking] = useState<"auto" | "disabled">(initial?.thinking ?? "disabled");
  const [name, setName] = useState(initial?.name ?? PRESETS[0].name);
  const [model, setModel] = useState(initial?.model ?? PRESETS[0].model);
  const [baseUrl, setBaseUrl] = useState(initial?.base_url ?? PRESETS[0].base_url);
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [apiKeyEnv, setApiKeyEnv] = useState(initial?.api_key_env ?? "");
  const [timeout, setTimeoutVal] = useState(initial?.timeout_seconds ?? 90);
  const [showAdv, setShowAdv] = useState(!!initial?.api_key_env);
  const [activePreset, setActivePreset] = useState<string>(isEdit ? "" : PRESETS[0].id);
  const action = useAction();

  const localUrl = isLoopbackUrl(baseUrl.trim());
  const effectiveMode: "api" | "local" = localUrl && !apiKey.trim() && !apiKeyEnv.trim() && initial?.credential_source !== "direct"
    ? "local" : "api";
  const hasSavedDirectKey = isEdit && initial?.credential_source === "direct";

  const applyPreset = (preset: Preset) => {
    setActivePreset(preset.id);
    setApiFormat(preset.api_format);
    setThinking(preset.thinking);
    setName(preset.name);
    setModel(preset.model);
    setBaseUrl(preset.base_url);
    setTimeoutVal(preset.timeout_seconds);
  };

  return <Modal title={isEdit ? t(`Edit Connection · ${initial?.name}`, `编辑模型连接 · ${initial?.name}`) : t("Configure Model Connection", "配置模型连接")}
    close={close} className="context-graph-dialog">
    {!backendReady && <div className="note note--warn">
      <b>{t("Backend restart required", "后端服务尚未重启")}</b>
      {t(
        "The running run_api.py process is an older build and will reject direct UI API keys (body.api_key). Press Ctrl+C and restart .venv/bin/python run_api.py --projects-only, then refresh.",
        "当前运行中的后端仍是旧版本，保存界面填写的 API Key 会被旧进程拒收（body.api_key）。请在终端按 Ctrl+C 停止并重启 .venv/bin/python run_api.py --projects-only 后刷新本页。",
      )}
    </div>}
    <ErrorNote error={action.error} />
    <div className="stack">
      {!isEdit && <div className="row-set">
        <span className="micro">{t("Quick Fill", "快速填充")}</span>
        {PRESETS.map(preset => <Button key={preset.id} small ghost={activePreset !== preset.id}
          primary={activePreset === preset.id} onClick={() => applyPreset(preset)}>{locale === "zh" ? preset.zh : preset.en}</Button>)}
      </div>}

      <form className="stack" onSubmit={event => {
        event.preventDefault();
        void action.run(async () => {
          const cleanUrl = baseUrl.trim();
          const cleanModel = model.trim();
          const cleanKey = apiKey.trim();
          const cleanEnv = apiKeyEnv.trim();
          const payload = {
            name: name.trim() || cleanModel,
            model: cleanModel,
            base_url: cleanUrl,
            api_key: cleanEnv ? null : (cleanKey || null),
            api_key_env: cleanEnv || null,
            timeout_seconds: Number(timeout),
            mode: effectiveMode,
            api_format: apiFormat,
            thinking,
          };
          if (isEdit && initial) {
            await json(`/model-profiles/${initial.profile_id}`, post(payload, "PUT"));
          } else {
            await json("/model-profiles", post(payload));
          }
          saved();
          close();
        });
      }}>
        <div className="form-grid">
          <Field label={t("API Base URL", "API 地址 (Base URL)")}>
            <input className="input" type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)}
              placeholder="https://api.deepseek.com/v1 or http://127.0.0.1:11434/v1" required />
          </Field>
          <Field label={t("Model ID", "模型名 (Model ID)")}>
            <input className="input" value={model} onChange={e => {
              const next = e.target.value;
              setModel(next);
              if (!isEdit && (!name || name === model)) setName(next);
            }} placeholder="deepseek-chat / gpt-4.1-mini / qwen3:8b" required maxLength={200} />
          </Field>
          <Field label={localUrl ? t("API Key (optional for localhost)", "API Key（本机地址可留空）")
            : hasSavedDirectKey ? t(`API Key (saved ${initial?.api_key_masked ?? ""}, leave blank to keep)`, `API Key（已保存 ${initial?.api_key_masked ?? ""}，留空保持不变）`)
              : "API Key"}>
            <div className="row-set" style={{ width: "100%" }}>
              <input className="input" style={{ flex: 1 }} type={showKey ? "text" : "password"}
                value={apiKey} onChange={e => setApiKey(e.target.value)}
                required={!localUrl && !hasSavedDirectKey && !apiKeyEnv.trim()}
                placeholder={localUrl ? t("Optional for local Ollama / vLLM", "本机 Ollama / vLLM 可留空")
                  : hasSavedDirectKey ? t("Leave blank to keep or paste new key", "留空保持原密钥，或输入新密钥覆盖")
                    : t("Paste sk-... key (stored locally)", "直接粘贴 sk-... 密钥（仅保存在本地工作区）")} />
              <Button small ghost onClick={() => setShowKey(v => !v)}>{showKey ? t("Hide", "隐藏") : t("Show", "显示")}</Button>
            </div>
          </Field>
          <Field label={t("OpenAI Protocol Format", "OpenAI 协议格式（通信标准）")}>
            <select className="input" value={apiFormat} onChange={e => setApiFormat(e.target.value as "chat_completions" | "responses")}>
              <option value="chat_completions">{t("Chat Completions (/chat/completions)", "Chat Completions (/chat/completions · 通用)")}</option>
              <option value="responses">{t("Responses (/responses)", "Responses (/responses · 网关/新版接口)")}</option>
            </select>
          </Field>
        </div>

        <div className="row-set">
          <Button small ghost onClick={() => setShowAdv(v => !v)}>
            {showAdv ? t("▾ Hide Advanced", "▾ 收起高级选项") : t("▸ Advanced (Alias / Thinking / Timeout / Env Var)", "▸ 高级选项（别名 / 思考开关 / 超时 / 环境变量）")}
          </Button>
          <span className="sp" />
          <span className="hint">{localUrl ? t("Localhost · No key required", "已识别为本机地址 · 免密钥") : t("Thinking off · 90s timeout", "已默认关闭思考并设超时 90 秒")}</span>
        </div>

        {showAdv && <div className="form-grid panel">
          <Field label={t("Display Name (Optional)", "显示名称（可选）")}>
            <input className="input" value={name} onChange={e => setName(e.target.value)} placeholder={t("Defaults to Model ID", "默认使用模型名")} maxLength={120} />
          </Field>
          <Field label={t("Reasoning / Thinking", "推理思考开关")}>
            <select className="input" value={thinking} onChange={e => setThinking(e.target.value as "auto" | "disabled")}>
              <option value="disabled">{t("Disabled (Recommended for JSON/Graph)", "关闭思考（推荐：防建图/JSON 截断）")}</option>
              <option value="auto">{t("Server Default", "按服务端默认")}</option>
            </select>
          </Field>
          <Field label={t("Timeout (seconds)", "请求超时（秒）")}>
            <input className="input" type="number" min="2" max="360" value={timeout}
              onChange={e => setTimeoutVal(Number(e.target.value))} required />
          </Field>
          <Field label={t("Env Var Name (Optional fallback)", "改用终端环境变量名（高级备用）")}>
            <input className="input" value={apiKeyEnv} onChange={e => setApiKeyEnv(e.target.value.toUpperCase())}
              pattern="[A-Z][A-Z0-9_]{1,80}" placeholder={t("Leave blank to use direct API Key", "留空即直接用上方填写的 API Key")} />
          </Field>
        </div>}

        <div className="dialog__foot">
          <Button ghost onClick={close}>{t("Cancel", "取消")}</Button>
          <Button primary type="submit" disabled={action.busy}>{action.busy ? t("Saving…", "正在保存…") : isEdit ? t("Save Changes", "保存修改") : t("Save Connection", "保存连接")}</Button>
        </div>
      </form>
    </div>
  </Modal>;
}

export function Models({ project, projects, workspace, offline, changed }: {
  project?: Project; projects: Project[]; workspace?: Workspace; offline: boolean; changed: () => void;
}) {
  const { locale, t } = useI18n();
  const [refresh, setRefresh] = useState(0);
  const [targetScope, setTargetScope] = useState<"project" | "workspace">(project ? "project" : "workspace");
  const [probeRole, setProbeRole] = useState<Role>("reviewer");
  const [probe, setProbe] = useState<{ profile: string; role: Role; passed: boolean; calls: number; reason: string | null }>();
  const [saved, setSaved] = useState(false);
  const [editing, setEditing] = useState<Profile | "new" | null>(null);
  const [quickProfile, setQuickProfile] = useState("");
  const profilesQuery = useQuery<{ profiles: Profile[] }>("/model-profiles", refresh);
  const profiles = profilesQuery.data?.profiles ?? [];
  const backendReady = workspace?.capabilities?.direct_api_key === true;
  const effectiveTarget = targetScope === "project" && project ? "project" : "workspace";
  const currentBindings = effectiveTarget === "project" && project
    ? project.models
    : (workspace?.default_models ?? {});
  const [draftBindings, setDraftBindings] = useState<Record<string, string>>({});
  const activeBindings = { ...currentBindings, ...draftBindings };
  const action = useAction(() => { setRefresh(n => n + 1); changed(); });

  const formatTag = (p: Profile) => {
    const modeLabel = p.mode === "local" ? t("Local", "本地") : "API";
    const protoLabel = p.api_format === "chat_completions" ? "Chat" : "Responses";
    return `${modeLabel} · ${protoLabel}`;
  };

  const runProbe = (profileId: string, role: Role) => void action.run(async () => {
    setProbe(undefined);
    const result = await json<{ passed: boolean; calls: number; reason: string | null }>(
      `/model-profiles/${profileId}/probe`, post({ role }),
    );
    setProbe({ ...result, profile: profileId, role });
  });

  const saveBindings = (bindingsToSave: Record<string, string>) => {
    const clean = Object.fromEntries(Object.entries(bindingsToSave).filter(([, v]) => !!v));
    const url = effectiveTarget === "project" && project
      ? `/projects/${project.project_id}/model-roles`
      : "/workspace/model-roles";
    void action.run(async () => {
      await json(url, post({ roles: clean }, "PUT"));
      setDraftBindings({});
      setSaved(true);
    });
  };

  const applyAllToProfile = (profileId: string) => {
    if (!profileId) return;
    const all = Object.fromEntries((Object.keys(roles) as Role[]).map(r => [r, profileId]));
    setDraftBindings(all);
    saveBindings(all);
  };

  return <>
    <Head title={t("Model Settings", "模型设置")} sub={effectiveTarget === "project" && project ? t(`Project: ${project.name}`, `当前配置项目：${project.name}`) : t("Scope: Workspace Defaults (Naev / Chat / Fallback)", "当前配置：工作台全局默认（Naev / 对话 / 未覆盖项目）")}>
      <Button primary onClick={() => setEditing("new")}>{t("+ Add Connection", "+ 新增模型连接")}</Button>
    </Head>
    {workspace && !backendReady && <div className="note note--warn">
      <b>{t("Backend restart needed", "提示：检测到后端进程需要重启")}</b>
      {t(
        "run_api.py is running an older build. Press Ctrl+C and restart .venv/bin/python run_api.py --projects-only, then refresh to save API keys directly in UI.",
        "你已更新代码，但终端里运行中的 run_api.py 仍是旧进程。请重启 run_api.py 后刷新本页，即可直接在界面填写保存 API Key。",
      )}
    </div>}
    <ErrorNote error={action.error || profilesQuery.error} />

    <div className="split">
      <div className="split__main">
        <Section>{t("6 Agent Module Bindings", "六个 Agent 模块模型选择")}</Section>
        <div className="row-set">
          {project && <Button small ghost aria-pressed={effectiveTarget === "project"}
            onClick={() => { setTargetScope("project"); setDraftBindings({}); setSaved(false); }}>
            {t(`Project (${project.name})`, `当前项目（${project.name}）`)}
          </Button>}
          <Button small ghost aria-pressed={effectiveTarget === "workspace"}
            onClick={() => { setTargetScope("workspace"); setDraftBindings({}); setSaved(false); }}>
            {t("Workspace Default", "工作台全局默认")}{projects.length > 0 ? t(" (Naev / Chat / Fallback)", "（Naev / 对话 / 兜底）") : ""}
          </Button>
          {!!profiles.length && <>
            <span className="sp" />
            <select className="input" style={{ maxWidth: 220 }} aria-label={t("Apply one connection to all 6 modules", "一键应用到全部 6 个模块")}
              value={quickProfile} onChange={e => setQuickProfile(e.target.value)}>
              <option value="">{t("Apply to all modules…", "一键绑定全部模块…")}</option>
              {profiles.map(p => <option key={p.profile_id} value={p.profile_id}>{p.name} ({p.model})</option>)}
            </select>
            <Button small disabled={!quickProfile || action.busy} onClick={() => applyAllToProfile(quickProfile)}>{t("Apply All", "全部应用")}</Button>
          </>}
        </div>

        <div className="module-grid">
          {(Object.entries(roles) as [Role, string][]).map(([key]) => {
            const title = roleLabel(key, locale);
            const selectedId = activeBindings[key] ?? "";
            const selectedProfile = profiles.find(p => p.profile_id === selectedId);
            const fallbackId = effectiveTarget === "project" ? workspace?.default_models?.[key] : undefined;
            const fallbackProfile = fallbackId ? profiles.find(p => p.profile_id === fallbackId) : undefined;
            const moduleProbe = probe && probe.role === key && probe.profile === selectedId ? probe : undefined;
            return <div className="panel module-card" key={key}>
              <div className="module-card__head">
                <div className="module-card__title">
                  <b>{title}</b>
                  <span className="mono hint">{key}</span>
                </div>
                {selectedProfile && <span className={`pill ${selectedProfile.mode === "local" ? "pill--info" : "pill--ok"}`}>
                  {formatTag(selectedProfile)}
                </span>}
              </div>
              <p className="muted module-desc" title={roleMeta[key][locale]}>{roleMeta[key][locale]}</p>
              <div className="module-card__ctrl">
                <select className="input module-select" aria-label={t(`${title} model`, `${title} 模型`)} value={selectedId} onChange={e => {
                  setSaved(false);
                  setDraftBindings(old => ({ ...old, [key]: e.target.value }));
                }}>
                  <option value="">
                    {fallbackProfile ? t(`Inherit default (${fallbackProfile.name})`, `继承工作台默认（${fallbackProfile.name}）`)
                      : t(`System default (${workspace?.default_model_config?.model ?? "deepseek-v4.1-flash-1017"})`, `系统默认（${workspace?.default_model_config?.model ?? "deepseek-v4.1-flash-1017"}）`)}
                  </option>
                  {profiles.map(p => <option key={p.profile_id} value={p.profile_id}>
                    [{formatTag(p)}] {p.name} ({p.model})
                  </option>)}
                </select>
                {selectedProfile && <Button small ghost disabled={action.busy || offline}
                  onClick={() => runProbe(selectedProfile.profile_id, key)}>{t("Test", "测试")}</Button>}
              </div>
              {moduleProbe && <div className="row-set">
                <Status value={moduleProbe.passed ? "passed" : "failed"} />
                <span className="mono hint">{t(`${moduleProbe.calls} calls`, `${moduleProbe.calls} 次调用`)}</span>
                {moduleProbe.reason && <ErrorNote reason={moduleProbe.reason} />}
              </div>}
            </div>;
          })}
        </div>

        <div className="row-set">
          <Button primary disabled={action.busy} onClick={() => saveBindings(activeBindings)}>{t("Save Bindings", "保存模块绑定")}</Button>
          {!!Object.keys(activeBindings).length && <Button ghost disabled={action.busy}
            onClick={() => saveBindings({})}>{t("Reset to Default", "全部重置为默认")}</Button>}
          {saved && <Status value="completed" />}
        </div>

        <Section>{t("Configured Connections (URL · API Key · Model · Protocol)", "已配置模型连接（URL · API Key · 模型名 · OpenAI 协议格式）")}</Section>
        <div className="row-set">
          <Field label={t("Probe Role", "测试环节")}><select className="input" value={probeRole} onChange={e => setProbeRole(e.target.value as Role)}>
            {(Object.keys(roles) as Role[]).map(key => <option key={key} value={key}>{roleLabel(key, locale)} ({key})</option>)}
          </select></Field>
          {offline && <p className="muted">{t("Offline demo mode: bindings save normally; live probing requires running without --offline.", "当前为离线演示模式，可配置并绑定模型；真实连通测试需不带 --offline 启动服务。")}</p>}
        </div>

        {!profiles.length && <Empty title={t("No custom connections yet", "还没有自定义模型连接")}>{t("Click '+ Add Connection' to enter your Base URL, Model ID, and API Key.", "点击右上角「+ 新增模型连接」，只需填写 Base URL、模型名和 API Key 即可保存。")}</Empty>}

        {profiles.map(p => <div className="panel" key={p.profile_id}>
          <div className="row-set">
            <b>{p.name}</b>
            <span className={`pill ${p.mode === "local" ? "pill--info" : "pill--ok"}`}>{formatTag(p)}</span>
            {p.thinking === "disabled" && <span className="pill pill--plain">{t("Thinking Off", "已关闭思考")}</span>}
            <Status value={p.credential_configured ? "valid" : "pending"} />
            <span className="sp" />
            <Button small ghost disabled={action.busy || offline} onClick={() => runProbe(p.profile_id, probeRole)}>{t("Probe", "测试可用性")}</Button>
            <Button small ghost disabled={action.busy} onClick={() => setEditing(p)}>{t("Edit", "编辑")}</Button>
            <Button small ghost disabled={action.busy} onClick={() => void action.run(() =>
              json(`/model-profiles/${p.profile_id}`, { method: "DELETE" }))}>{t("Delete", "删除")}</Button>
          </div>
          <div className="kv"><span>{t("Model ID", "模型名")}</span><b>{p.model}</b></div>
          <div className="kv"><span>{t("Base URL", "接口地址")}</span><b>{p.base_url}</b></div>
          <div className="kv"><span>{t("Credentials", "API 凭据")}</span><b>
            {p.credential_source === "direct"
              ? t(`Saved key (${p.api_key_masked ?? "set"})`, `界面已保存密钥（${p.api_key_masked ?? "已配置"}）`)
              : p.api_key_env
                ? t(`Env var ${p.api_key_env} (${p.credential_configured ? "detected" : "missing"})`, `终端环境变量 ${p.api_key_env}（${p.credential_configured ? "已检测到" : "未在服务端检测到"}）`)
                : t("Local (No key)", "本地免密钥")}
          </b></div>
          {probe?.profile === p.profile_id && <div className="row-set">
            <Status value={probe.passed ? "passed" : "failed"} />
            <span className="mono muted">{t(`Role: ${roleLabel(probe.role, locale)} · ${probe.calls} calls`, `测试环节：${roleLabel(probe.role, locale)} · ${probe.calls} 次调用`)}</span>
            {probe.reason && <ErrorNote reason={probe.reason} />}
          </div>}
        </div>)}
      </div>

      <aside className="split__side">
        <div className="panel">
          <h4>{t("System Fallback Config", "当前系统兜底配置")}</h4>
          <div className="kv"><span>{t("Default Model", "默认模型")}</span><b>{workspace?.default_model_config?.model ?? "deepseek-v4.1-flash-1017"}</b></div>
          <div className="kv"><span>{t("Default Gateway", "默认网关")}</span><b>{workspace?.default_model_config?.base_url ?? "—"}</b></div>
          <div className="kv"><span>{t("Credentials", "默认凭据")}</span><b>{workspace?.default_model_config?.credential_configured ? t("Env var ready", "环境变量已就绪") : t("Not set (add above)", "未配置（可上方新增）")}</b></div>
        </div>
        <div className="panel">
          <h4>{t("Guide", "配置说明")}</h4>
          <p className="muted">{t("1. OpenAI refers to the API wire format (Chat or Responses).", "1. OpenAI 指 API 的标准协议格式（Chat 或 Responses）。")}</p>
          <p className="muted">{t("2. Enter Base URL, Model ID, and API Key (optional for localhost).", "2. 填写 URL、模型名、API Key 即可保存，本机服务可不填密钥。")}</p>
          <p className="muted">{t("3. Use 'Apply All' above to bind one connection to all 6 roles.", "3. 可用上方「一键绑定全部模块」直接应用到全部 6 个环节。")}</p>
        </div>
      </aside>
    </div>

    {editing && <ProfileModal
      initial={editing === "new" ? undefined : editing}
      backendReady={backendReady}
      close={() => setEditing(null)}
      saved={() => { setRefresh(n => n + 1); changed(); }} />}
  </>;
}

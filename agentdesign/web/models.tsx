import { useState } from "react";
import { type Profile, type Project, type Workspace, json, post, roles, useQuery } from "./api";
import { Button, Empty, ErrorNote, Field, Head, Modal, Section, Status, useAction } from "./ui";

type Role = keyof typeof roles;

const roleMeta: Record<Role, { desc: string }> = {
  extractor: { desc: "抽取实体与关系图谱（建议关闭思考）" },
  selector: { desc: "从候选池筛选最优上下文证据" },
  translator: { desc: "结合上下文生成游戏风格初译" },
  reviewer: { desc: "只读工具核验证据并做出审校判定" },
  repairer: { desc: "按硬检查与审校意见定向修正" },
  intent: { desc: "解析自然语言指令为结构化计划" },
};

type Preset = {
  id: string;
  label: string;
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
    label: "DeepSeek",
    api_format: "chat_completions",
    thinking: "disabled",
    name: "DeepSeek Chat",
    model: "deepseek-chat",
    base_url: "https://api.deepseek.com/v1",
    timeout_seconds: 90,
  },
  {
    id: "gateway",
    label: "Tokendance 网关",
    api_format: "responses",
    thinking: "disabled",
    name: "DeepSeek Flash (网关)",
    model: "deepseek-v4.1-flash-1017",
    base_url: "https://tokendance.space/gateway/v1",
    timeout_seconds: 75,
  },
  {
    id: "qwen",
    label: "通义千问 / 兼容",
    api_format: "chat_completions",
    thinking: "disabled",
    name: "Qwen Plus",
    model: "qwen-plus",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    timeout_seconds: 90,
  },
  {
    id: "ollama",
    label: "本地 Ollama",
    api_format: "chat_completions",
    thinking: "disabled",
    name: "Ollama Qwen3-8B",
    model: "qwen3:8b",
    base_url: "http://127.0.0.1:11434/v1",
    timeout_seconds: 120,
  },
  {
    id: "vllm",
    label: "本地 vLLM",
    api_format: "chat_completions",
    thinking: "disabled",
    name: "本地 vLLM",
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

  return <Modal title={isEdit ? `编辑模型连接 · ${initial?.name}` : "配置模型连接"}
    close={close} className="context-graph-dialog">
    {!backendReady && <div className="note note--warn">
      <b>后端服务尚未重启</b>
      当前运行中的后端仍是旧版本，保存界面填写的 API Key 会被旧进程拒收（<code>body.api_key</code>）。请在启动服务的终端先按 <code>Ctrl+C</code> 停止（如端口仍占用可执行 <code>kill $(lsof -ti :8766)</code>），重新运行 <code>.venv/bin/python run_api.py --projects-only</code> 后刷新本页。
    </div>}
    <ErrorNote error={action.error} />
    <div className="stack">
      {!isEdit && <div className="row-set">
        <span className="micro">快速填充</span>
        {PRESETS.map(preset => <Button key={preset.id} small ghost={activePreset !== preset.id}
          primary={activePreset === preset.id} onClick={() => applyPreset(preset)}>{preset.label}</Button>)}
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
          <Field label="API 地址 (Base URL)">
            <input className="input" type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)}
              placeholder="https://api.deepseek.com/v1 或 http://127.0.0.1:11434/v1" required />
          </Field>
          <Field label="模型名 (Model ID)">
            <input className="input" value={model} onChange={e => {
              const next = e.target.value;
              setModel(next);
              if (!isEdit && (!name || name === model)) setName(next);
            }} placeholder="deepseek-chat / gpt-4.1-mini / qwen3:8b" required maxLength={200} />
          </Field>
          <Field label={localUrl ? "API Key（本机地址可留空）"
            : hasSavedDirectKey ? `API Key（已保存 ${initial?.api_key_masked ?? ""}，留空保持不变）`
              : "API Key"}>
            <div className="row-set" style={{ width: "100%" }}>
              <input className="input" style={{ flex: 1 }} type={showKey ? "text" : "password"}
                value={apiKey} onChange={e => setApiKey(e.target.value)}
                required={!localUrl && !hasSavedDirectKey && !apiKeyEnv.trim()}
                placeholder={localUrl ? "本机 Ollama / vLLM 可留空"
                  : hasSavedDirectKey ? "留空保持原密钥，或输入新密钥覆盖"
                    : "直接粘贴 sk-... 密钥（仅保存在本地工作区）"} />
              <Button small ghost onClick={() => setShowKey(v => !v)}>{showKey ? "隐藏" : "显示"}</Button>
            </div>
          </Field>
          <Field label="OpenAI 协议格式（通信标准，非指某家厂商）">
            <select className="input" value={apiFormat} onChange={e => setApiFormat(e.target.value as "chat_completions" | "responses")}>
              <option value="chat_completions">Chat Completions (/chat/completions · 通用)</option>
              <option value="responses">Responses (/responses · 网关/新版接口)</option>
            </select>
          </Field>
        </div>

        <div className="row-set">
          <Button small ghost onClick={() => setShowAdv(v => !v)}>
            {showAdv ? "▾ 收起高级选项" : "▸ 高级选项（别名 / 思考开关 / 超时 / 环境变量）"}
          </Button>
          <span className="sp" />
          <span className="hint">{localUrl ? "已识别为本机地址 · 免密钥" : "已默认关闭思考并设超时 90 秒"}</span>
        </div>

        {showAdv && <div className="form-grid panel">
          <Field label="显示名称（可选）">
            <input className="input" value={name} onChange={e => setName(e.target.value)} placeholder="默认使用模型名" maxLength={120} />
          </Field>
          <Field label="推理思考开关">
            <select className="input" value={thinking} onChange={e => setThinking(e.target.value as "auto" | "disabled")}>
              <option value="disabled">关闭思考（推荐：防建图/JSON 截断）</option>
              <option value="auto">按服务端默认</option>
            </select>
          </Field>
          <Field label="请求超时（秒）">
            <input className="input" type="number" min="2" max="360" value={timeout}
              onChange={e => setTimeoutVal(Number(e.target.value))} required />
          </Field>
          <Field label="改用终端环境变量名（高级备用，填了则优先用变量）">
            <input className="input" value={apiKeyEnv} onChange={e => setApiKeyEnv(e.target.value.toUpperCase())}
              pattern="[A-Z][A-Z0-9_]{1,80}" placeholder="留空即直接用上方填写的 API Key" />
          </Field>
        </div>}

        <div className="dialog__foot">
          <Button ghost onClick={close}>取消</Button>
          <Button primary type="submit" disabled={action.busy}>{action.busy ? "正在保存…" : isEdit ? "保存修改" : "保存连接"}</Button>
        </div>
      </form>
    </div>
  </Modal>;
}

export function Models({ project, projects, workspace, offline, changed }: {
  project?: Project; projects: Project[]; workspace?: Workspace; offline: boolean; changed: () => void;
}) {
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
    const modeLabel = p.mode === "local" ? "本地" : "API";
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
    <Head title="模型设置" sub={effectiveTarget === "project" && project ? `当前配置项目：${project.name}` : "当前配置：工作台全局默认（Naev / 对话 / 未覆盖项目）"}>
      <Button primary onClick={() => setEditing("new")}>+ 新增模型连接</Button>
    </Head>
    {workspace && !backendReady && <div className="note note--warn">
      <b>提示：检测到后端进程需要重启</b>
      你已更新代码，但终端里运行中的 <code>run_api.py</code> 仍是旧进程。请先按 <code>Ctrl+C</code> 停止（如端口 8766 仍被占用执行 <code>kill $(lsof -ti :8766)</code>），再重启 <code>.venv/bin/python run_api.py --projects-only</code> 并刷新本页，即可直接在界面填写保存 API Key。
    </div>}
    <ErrorNote error={action.error || profilesQuery.error} />

    <div className="split">
      <div className="split__main">
        <Section>六个 Agent 模块模型选择</Section>
        <div className="row-set">
          {project && <Button small ghost aria-pressed={effectiveTarget === "project"}
            onClick={() => { setTargetScope("project"); setDraftBindings({}); setSaved(false); }}>
            当前项目（{project.name}）
          </Button>}
          <Button small ghost aria-pressed={effectiveTarget === "workspace"}
            onClick={() => { setTargetScope("workspace"); setDraftBindings({}); setSaved(false); }}>
            工作台全局默认{projects.length > 0 ? "（Naev / 对话 / 兜底）" : ""}
          </Button>
          {!!profiles.length && <>
            <span className="sp" />
            <select className="input" style={{ maxWidth: 210 }} aria-label="一键应用到全部 6 个模块"
              value={quickProfile} onChange={e => setQuickProfile(e.target.value)}>
              <option value="">一键绑定全部模块…</option>
              {profiles.map(p => <option key={p.profile_id} value={p.profile_id}>{p.name} ({p.model})</option>)}
            </select>
            <Button small disabled={!quickProfile || action.busy} onClick={() => applyAllToProfile(quickProfile)}>全部应用</Button>
          </>}
        </div>

        <div className="module-grid">
          {(Object.entries(roles) as [Role, string][]).map(([key, title]) => {
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
              <p className="muted module-desc" title={roleMeta[key].desc}>{roleMeta[key].desc}</p>
              <div className="module-card__ctrl">
                <select className="input module-select" aria-label={`${title} 模型`} value={selectedId} onChange={e => {
                  setSaved(false);
                  setDraftBindings(old => ({ ...old, [key]: e.target.value }));
                }}>
                  <option value="">
                    {fallbackProfile ? `继承工作台默认（${fallbackProfile.name}）`
                      : `系统默认（${workspace?.default_model_config?.model ?? "deepseek-v4.1-flash-1017"}）`}
                  </option>
                  {profiles.map(p => <option key={p.profile_id} value={p.profile_id}>
                    [{formatTag(p)}] {p.name} ({p.model})
                  </option>)}
                </select>
                {selectedProfile && <Button small ghost disabled={action.busy || offline}
                  onClick={() => runProbe(selectedProfile.profile_id, key)}>测试</Button>}
              </div>
              {moduleProbe && <div className="row-set">
                <Status value={moduleProbe.passed ? "passed" : "failed"} />
                <span className="mono hint">{moduleProbe.calls} 次调用</span>
                {moduleProbe.reason && <ErrorNote reason={moduleProbe.reason} />}
              </div>}
            </div>;
          })}
        </div>

        <div className="row-set">
          <Button primary disabled={action.busy} onClick={() => saveBindings(activeBindings)}>保存模块绑定</Button>
          {!!Object.keys(activeBindings).length && <Button ghost disabled={action.busy}
            onClick={() => saveBindings({})}>全部重置为默认</Button>}
          {saved && <Status value="completed" />}
        </div>

        <Section>已配置模型连接（URL · API Key · 模型名 · OpenAI 协议格式）</Section>
        <div className="row-set">
          <Field label="测试环节"><select className="input" value={probeRole} onChange={e => setProbeRole(e.target.value as Role)}>
            {Object.entries(roles).map(([key, title]) => <option key={key} value={key}>{title} ({key})</option>)}
          </select></Field>
          {offline && <p className="muted">当前为离线演示模式，可配置并绑定模型；真实连通测试需不带 --offline 启动服务。</p>}
        </div>

        {!profiles.length && <Empty title="还没有自定义模型连接">点击右上角「+ 新增模型连接」，只需填写 Base URL、模型名和 API Key 即可保存。</Empty>}

        {profiles.map(p => <div className="panel" key={p.profile_id}>
          <div className="row-set">
            <b>{p.name}</b>
            <span className={`pill ${p.mode === "local" ? "pill--info" : "pill--ok"}`}>{formatTag(p)}</span>
            {p.thinking === "disabled" && <span className="pill pill--plain">已关闭思考</span>}
            <Status value={p.credential_configured ? "valid" : "pending"} />
            <span className="sp" />
            <Button small ghost disabled={action.busy || offline} onClick={() => runProbe(p.profile_id, probeRole)}>测试可用性</Button>
            <Button small ghost disabled={action.busy} onClick={() => setEditing(p)}>编辑</Button>
            <Button small ghost disabled={action.busy} onClick={() => void action.run(() =>
              json(`/model-profiles/${p.profile_id}`, { method: "DELETE" }))}>删除</Button>
          </div>
          <div className="kv"><span>模型名</span><b>{p.model}</b></div>
          <div className="kv"><span>接口地址</span><b>{p.base_url}</b></div>
          <div className="kv"><span>API 凭据</span><b>
            {p.credential_source === "direct"
              ? `界面已保存密钥（${p.api_key_masked ?? "已配置"}）`
              : p.api_key_env
                ? `终端环境变量 ${p.api_key_env}（${p.credential_configured ? "已检测到" : "未在服务端检测到"}）`
                : "本地免密钥"}
          </b></div>
          {probe?.profile === p.profile_id && <div className="row-set">
            <Status value={probe.passed ? "passed" : "failed"} />
            <span className="mono muted">测试环节：{roles[probe.role]} · {probe.calls} 次调用</span>
            {probe.reason && <ErrorNote reason={probe.reason} />}
          </div>}
        </div>)}
      </div>

      <aside className="split__side">
        <div className="panel">
          <h4>当前系统兜底配置</h4>
          <div className="kv"><span>默认模型</span><b>{workspace?.default_model_config?.model ?? "deepseek-v4.1-flash-1017"}</b></div>
          <div className="kv"><span>默认网关</span><b>{workspace?.default_model_config?.base_url ?? "—"}</b></div>
          <div className="kv"><span>默认凭据</span><b>{workspace?.default_model_config?.credential_configured ? "环境变量已就绪" : "未配置（可上方新增）"}</b></div>
        </div>
        <div className="panel">
          <h4>配置说明</h4>
          <p className="muted">1. <b>OpenAI</b> 指 API 的标准协议格式（<code>Chat</code> 或 <code>Responses</code>）。</p>
          <p className="muted">2. 填写 <b>URL、模型名、API Key</b> 即可保存，本机服务可不填密钥。</p>
          <p className="muted">3. 可用上方「一键绑定全部模块」直接应用到全部 6 个环节。</p>
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

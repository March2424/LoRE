import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { type FileReviewItem, type FileReviewScope, type JobPage, type Project, type Selection, type Workspace, json, params, post, stored, useQuery } from "./api";
import { Button, Empty, ErrorNote, Field, Modal, useAction } from "./ui";
import { Resources, type Upload } from "./resources";
import { AgentWorkspace } from "./agent-workspace";
import { Compose, type Composer, FileReviewQueue, JobDetail, JobList } from "./jobs";
import { Knowledge } from "./knowledge";
import { Models } from "./models";
import { Glossary } from "./glossary";
import { Evolution } from "./evolution";
import {
  ASCII_MODES,
  ASCII_STORAGE_MODE,
  ASCII_STORAGE_PAUSED,
  AsciiDock,
  type AsciiMode,
  AsciiOcean,
} from "./ascii-ocean";

const pages = {
  home: "工作台", chat: "Agent 对话", jobs: "任务", knowledge: "图谱与记忆", models: "模型设置",
  glossary: "术语变更", evolution: "进化中心",
};
const pageNumbers: Record<keyof typeof pages, string> = {
  home: "01", chat: "02", jobs: "03", knowledge: "04", models: "05", glossary: "06", evolution: "07",
};
type Page = keyof typeof pages;
type Route = { page: Page; job?: string; fileReview?: FileReviewScope; reviewSelection?: string };
function readRoute(): Route {
  const [path, search] = location.hash.slice(1).split("?", 2);
  const [rawPage, detail] = path.split("/");
  const page = !rawPage ? "chat" : rawPage === "resources" ? "home" : rawPage;
  const job = page === "jobs" && /^[a-f0-9]{32}$/.test(detail || "") ? detail : undefined;
  const route: Route = { page: Object.hasOwn(pages, page) ? page as Page : "chat", job };
  if (page === "jobs" && (job || detail === "file-review")) {
    const query = new URLSearchParams(search);
    const projectId = query.get("project");
    const filename = query.get("file");
    if (projectId && /^[a-f0-9]{32}$/.test(projectId) && filename) {
      route.fileReview = { projectId, filename };
      const step = query.get("step");
      const item = query.get("item");
      if (job && step !== null && /^\d+$/.test(step) && item) route.reviewSelection = `${step}:${item}`;
    }
  }
  return route;
}

function NewProject({ close, created }: { close: () => void; created: (project: Project) => void }) {
  const action = useAction();
  return <Modal title="新建项目" close={close}>
    <ErrorNote error={action.error} />
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const source = data.get("source");
      void action.run(async () => created(await json<Project>("/projects", post({
        name: data.get("name"), source_lang: source, target_lang: source === "en" ? "zh" : "en",
      }))));
    }}>
      <Field label="项目名称"><input className="input" name="name" required maxLength={120} autoFocus /></Field>
      <Field label="翻译方向"><select className="input" name="source">
        <option value="en">英语 → 简体中文</option><option value="zh">简体中文 → 英语</option>
      </select></Field>
      <div className="dialog__foot"><Button type="submit" primary disabled={action.busy}>创建项目</Button></div>
    </form>
  </Modal>;
}

function App() {
  const [route, setRoute] = useState(readRoute);
  const [selectedProject, setSelectedProject] = useState(() => stored("lore-project", ""));
  const [createdProject, setCreatedProject] = useState<Project>();
  const [upload, changeUpload] = useState<Upload | null>(() => stored("naev-upload", null));
  const [selection, setSelection] = useState<Selection>(() => ({ ids: [], upload: selectedProject === "naev" ? upload?.upload_id : undefined }));
  const [newProject, setNewProject] = useState(false);
  const [composer, setComposer] = useState<{ mode: Composer; operation?: string }>();
  const [refresh, setRefresh] = useState(0);
  const [connected, setConnected] = useState(false);
  const [navStack, setNavStack] = useState<string[]>([]);
  const [asciiMode, setAsciiMode] = useState<AsciiMode>(() => {
    const raw = localStorage.getItem(ASCII_STORAGE_MODE);
    return ASCII_MODES.some(m => m.id === raw) ? (raw as AsciiMode) : "ascii";
  });
  const [asciiPaused, setAsciiPaused] = useState(() => localStorage.getItem(ASCII_STORAGE_PAUSED) === "1");
  const cycleAsciiMode = () => setAsciiMode(cur => {
    const idx = ASCII_MODES.findIndex(m => m.id === cur);
    const next = ASCII_MODES[(idx + 1) % ASCII_MODES.length].id;
    localStorage.setItem(ASCII_STORAGE_MODE, next);
    return next;
  });
  const toggleAsciiPause = () => setAsciiPaused(cur => {
    const next = !cur;
    localStorage.setItem(ASCII_STORAGE_PAUSED, next ? "1" : "0");
    return next;
  });
  const changed = () => setRefresh(value => value + 1);
  const workspace = useQuery<Workspace>("/workspace", refresh);
  const catalog = useQuery<{ projects: Project[] }>("/projects", refresh);
  const projects = catalog.data?.projects ?? [];
  const fileReview = route.fileReview;
  const activeProject = fileReview?.projectId ?? selectedProject;
  const chosen = projects.find(item => item.project_id === activeProject) ??
    (createdProject?.project_id === selectedProject ? createdProject : undefined) ?? projects[0];
  const scope = workspace.data?.naev && (activeProject === "naev" || !chosen) ? "naev" : chosen?.project_id ?? "";
  const detail = useQuery<Project>(scope && scope !== "naev" ? `/projects/${scope}` : null, refresh);
  const project = detail.data ?? (scope !== "naev" ? chosen : undefined);
  const recent = useQuery<JobPage>(route.page === "home" ?
    `/jobs?${params({ project: scope, limit: 6 })}` : null, refresh);
  const name = project?.name || (scope === "naev" ? "Naev 示范区" : "选择项目");
  const navigate = (page: Page, job?: string) => {
    const targetHash = page + (job ? "/" + job : "");
    const currentHash = location.hash.slice(1) || "chat";
    if (currentHash !== targetHash) setNavStack(prev => [...prev.slice(-15), currentHash]);
    location.hash = targetHash;
    setRoute({ page, job });
  };
  const goBack = (fallback: Page) => {
    const prev = navStack[navStack.length - 1];
    if (prev) {
      setNavStack(s => s.slice(0, -1));
      location.hash = prev;
      setRoute(readRoute());
    } else {
      navigate(fallback);
    }
  };
  const choose = (id: string) => {
    setSelectedProject(id); setSelection({ ids: [], upload: id === "naev" ? upload?.upload_id : undefined });
    localStorage.setItem("lore-project", JSON.stringify(id));
  };
  const openJob = (id: string) => { setComposer(undefined); changed(); navigate("jobs", id); };
  const openFileReview = (file: FileReviewScope, entry?: FileReviewItem) => {
    location.hash = `jobs/${entry?.job_id ?? "file-review"}?${params({ project: file.projectId, file: file.filename,
      step: entry?.step, item: entry?.occurrence_id })}`;
    setRoute(readRoute());
  };
  const openWorkbench = (id: string) => {
    if (id !== selectedProject) choose(id);
    navigate("home");
  };
  const openCompose = (mode: Composer, operation?: string) => setComposer({ mode, operation });
  useEffect(() => {
    const navigateHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", navigateHash);
    const events = new EventSource("/job-events");
    events.onopen = () => setConnected(true);
    events.onmessage = () => { setConnected(true); setRefresh(value => value + 1); };
    events.onerror = () => setConnected(false);
    return () => { window.removeEventListener("hashchange", navigateHash); events.close(); };
  }, []);
  useEffect(() => {
    document.querySelector("main.page")?.scrollTo(0, 0);
  }, [route.page, route.job, fileReview?.filename, route.reviewSelection, scope]);
  const nav = (page: Page) => <a href={`#${page}`} aria-current={route.page === page ? "page" : undefined}
    className={`nav${route.page === page ? " nav--active" : ""}`}>
    <span className="nav__num">{pageNumbers[page]}</span>{pages[page]}
  </a>;
  return <>
    <AsciiOcean mode={asciiMode} paused={asciiPaused} />
    <div className="soft-light" aria-hidden="true" />
    <div className="shell">
      <aside className="rail">
        <div className="rail__brand">LoRE</div>
        <div className="ws">
          <select className="ws__name" aria-label="切换项目" value={scope} onChange={e => { choose(e.target.value); navigate(route.page === "chat" ? "chat" : "home"); }}>
            {!scope && <option value="">选择项目</option>}{workspace.data?.naev && <option value="naev">Naev 示范区</option>}
            {projects.map(item => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
          </select>
          {scope && <div className="ws__meta">{project ? `${project.source_lang.toUpperCase()} → ${project.target_lang.toUpperCase()} · 源 v${project.revision}` : "EN → ZH · 内置"}</div>}
        </div>
        <nav aria-label="工作台导航">
          <div className="nav__label">项目</div>
          {nav("home")}{nav("chat")}{nav("jobs")}{project && nav("knowledge")}{nav("models")}
          <div className="nav__label">维护</div>
          {workspace.data?.naev && nav("glossary")}{nav("evolution")}
        </nav>
        <div className="rail__foot">
          <AsciiDock mode={asciiMode} paused={asciiPaused} onCycleMode={cycleAsciiMode} onTogglePause={toggleAsciiPause} />
          <div className={connected ? "live" : "rail__note"}>{connected ? "实时连接" : "正在连接"}</div>
          <div className="rail__note">{workspace.data?.offline ? "离线模式 · 单 worker" : workspace.data ? "在线模式 · 单 worker" : ""}</div>
        </div>
      </aside>
    <div className="canvas">
      <header className="topbar">
        <div className="crumb"><b>{name}</b><i>/</i><span>{pages[route.page]}</span>
          {route.job && <><i>/</i><span className="mono">J-{route.job.slice(0, 6)}</span></>}</div>
      </header>
      <main className="page">
        <ErrorNote error={workspace.error || catalog.error || detail.error} />
        {!workspace.data && !workspace.error && <Empty title="正在连接工作台…" />}
        {workspace.data && <>
          {route.page === "home" && <Resources key={scope || "empty"} project={project} scope={scope} projects={projects}
            jobs={recent.data} offline={workspace.data.offline} naev={workspace.data.naev}
            upload={selection.upload && upload?.upload_id === selection.upload ? upload : null}
            setUpload={value => { changeUpload(value); localStorage.setItem("naev-upload", JSON.stringify(value)); }}
            selection={selection} setSelection={setSelection} refresh={refresh} changed={changed}
            choose={id => { choose(id); navigate("home"); }} createProject={() => setNewProject(true)}
            navigate={navigate} openJob={openJob} compose={openCompose}
            openGraph={() => navigate("knowledge")} openFileReview={openFileReview} />}
          {route.page === "chat" && <AgentWorkspace key={scope || "empty"} project={project} scope={scope} projects={projects}
            offline={workspace.data.offline} naev={workspace.data.naev}
            upload={selection.upload && upload?.upload_id === selection.upload ? upload : null}
            setUpload={value => { changeUpload(value); localStorage.setItem("naev-upload", JSON.stringify(value)); }}
            selection={selection} setSelection={setSelection} refresh={refresh} changed={changed}
            choose={id => { choose(id); navigate("chat"); }} createProject={() => setNewProject(true)}
            navigate={navigate} openJob={openJob}
            openGraph={() => navigate("knowledge")} />}
          {route.page === "jobs" && (route.job ? <JobDetail key={`${route.job}:${fileReview?.filename ?? ""}:${route.reviewSelection ?? ""}`}
            id={route.job} refresh={refresh} changed={changed} openJob={openJob} fileReview={fileReview} reviewSelection={route.reviewSelection}
            onExit={() => goBack("jobs")} openChat={() => navigate("chat")}
            back={fileReview ? () => openFileReview(fileReview) : () => navigate("jobs")} deliver={openWorkbench} /> :
            fileReview ? <FileReviewQueue key={`${fileReview.projectId}:${fileReview.filename}`} file={fileReview} refresh={refresh}
              back={() => goBack("home")} openItem={entry => openFileReview(fileReview, entry)} /> :
            <JobList key={scope} scope={scope || undefined} refresh={refresh} openJob={openJob}
              onBack={navStack.length ? () => goBack("chat") : undefined} openChat={() => navigate("chat")} />)}
          {route.page === "knowledge" && (project ? <Knowledge key={scope} project={project} offline={workspace.data.offline}
            selection={selection} openJob={openJob} refresh={refresh} changed={changed}
            onBack={navStack.length ? () => goBack("chat") : undefined} /> : <Empty title="请先选择一个导入项目" />)}
          {route.page === "models" && <Models key={scope || "workspace"} project={project} projects={projects}
            workspace={workspace.data} offline={workspace.data.offline} changed={changed} />}
          {route.page === "glossary" && (workspace.data.naev ? <Glossary selected={scope === "naev" ? selection.ids : []}
            openJob={openJob} refresh={refresh} changed={changed} /> : <Empty title="此工作区未加载 Naev 快照" />)}
          {route.page === "evolution" && <Evolution offline={workspace.data.offline} naev={workspace.data.naev}
            scope={scope || undefined} refresh={refresh} changed={changed} openJob={openJob} />}
        </>}
      </main>
    </div>
    {newProject && <NewProject close={() => setNewProject(false)} created={item => {
      setNewProject(false); choose(item.project_id); setCreatedProject(item); changed(); navigate(route.page === "chat" ? "chat" : "home");
    }} />}
    {composer && <Compose key={`${composer.mode}:${composer.operation ?? ""}`} mode={composer.mode} initialOperation={composer.operation}
      project={project} scope={scope} selection={selection} close={() => setComposer(undefined)} openJob={openJob} />}
  </div>
  </>;
}

createRoot(document.getElementById("root")!).render(<App />);

import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { type FileReviewItem, type FileReviewScope, type JobPage, type Project, type Selection, type Workspace, json, language, params, post, stored, useQuery } from "./api";
import { Button, Empty, ErrorNote, Field, Grid, Head, Modal, Section, Status, useAction } from "./ui";
import { type Upload, Resources } from "./resources";
import { Compose, type Composer, FileReviewQueue, JobDetail, JobList } from "./jobs";
import { Knowledge, Models } from "./knowledge";
import { Glossary } from "./glossary";
import { Evolution } from "./evolution";

const pages = {
  home: "工作台", resources: "资源库", jobs: "任务", knowledge: "图谱与记忆", models: "模型设置",
  glossary: "术语变更", evolution: "进化中心",
};
type Page = keyof typeof pages;
type Route = { page: Page; job?: string; fileReview?: FileReviewScope; reviewSelection?: string };
function readRoute(): Route {
  const [path, search] = location.hash.slice(1).split("?", 2);
  const [page, detail] = path.split("/");
  const job = page === "jobs" && /^[a-f0-9]{32}$/.test(detail || "") ? detail : undefined;
  const route: Route = { page: Object.hasOwn(pages, page) ? page as Page : "home", job };
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

function Home({ project, scope, projects, jobs, offline, naev, choose, create, navigate, openJob, compose }: {
  project?: Project; scope: string; projects: Project[]; jobs?: JobPage; offline: boolean; naev: boolean;
  choose: (id: string) => void; create: () => void; navigate: (page: Page) => void; openJob: (id: string) => void;
  compose: (mode: Composer) => void;
}) {
  const recent = jobs?.jobs.slice(0, 6) ?? [];
  return <>
    <Head title="工作台" sub={project?.name || (scope === "naev" ? "Naev 示范区" : "游戏本地化项目")}>
      {scope && <Button onClick={() => compose("text")}>文本快译</Button>}
      <Button primary onClick={create}>新建项目</Button>
    </Head>
    {offline && <div className="note">离线演示 · 使用固定回执。可导入源文件、建图、人工改译和导出。</div>}
    {project && <>
      <div className="stats">{[[project.resources ?? 0, "源条目"], [project.approved_memory ?? 0, "已批准记忆"],
        [jobs?.total ?? 0, "项目任务"], [project.revision, "源版本"]].map(([count, title]) =>
        <div className="stat" key={title}><b>{count}</b><span>{title}</span></div>)}</div>
      <Section>项目流程</Section>
      <div className="row-set">
        <Button onClick={() => navigate("resources")}>{project.resources ? "浏览资源" : "导入源文件"}</Button>
        <span className="muted">→</span><Button onClick={() => navigate("knowledge")}>{project.graph_ready ? "查看图谱" : "构建图谱"}</Button>
        <span className="muted">→</span><Button onClick={() => navigate("jobs")}>任务与复核</Button>
        <span className="muted">→</span><Button onClick={() => navigate("resources")}>原格式交付</Button>
      </div>
    </>}
    <Section>项目</Section>
    {!projects.length && !naev && <Empty title="还没有本地化项目">创建一个项目，导入 CSV、XLSX 或 UTF-8 PO 源文件。</Empty>}
    {naev && <Grid columns="1fr 135px 85px"><b>Naev 示范区</b><span>英语 → 简体中文</span>
      <Button small ghost onClick={() => choose("naev")}>打开项目</Button></Grid>}
    {projects.map(item => <Grid key={item.project_id} columns="minmax(100px,1fr) 145px 105px 85px">
      <b className="clip">{item.name}</b><span>{language[item.source_lang]} → {language[item.target_lang]}</span>
      <Status value={item.graph_snapshot && item.graph_revision === item.revision ? "valid" : "pending"} />
      <Button small ghost onClick={() => choose(item.project_id)}>打开项目</Button>
    </Grid>)}
    {!scope && <div><Button onClick={() => compose("text")}>文本快译</Button></div>}
    {!!recent.length && <><Section>最近任务</Section>{recent.map(job => <Grid key={job.job_id} columns="110px minmax(110px,1fr) 90px 85px">
      <span className="mono">J-{job.job_id.slice(0, 6)}</span><span className="clip">{job.title ||
        (job.source_kind === "graph_build" ? "构建图谱" : job.source_kind === "text" ? "文本快译" : "资源任务")}</span>
      <Status value={job.status} /><Button small ghost onClick={() => openJob(job.job_id)}>打开任务</Button>
    </Grid>)}</>}
  </>;
}

function App() {
  const [route, setRoute] = useState(readRoute);
  const [selectedProject, setSelectedProject] = useState(() => stored("lore-project", ""));
  const [createdProject, setCreatedProject] = useState<Project>();
  const [upload, changeUpload] = useState<Upload | null>(() => stored("naev-upload", null));
  const [selection, setSelection] = useState<Selection>(() => ({ ids: [], upload: selectedProject === "naev" ? upload?.upload_id : undefined }));
  const [newProject, setNewProject] = useState(false);
  const [composer, setComposer] = useState<Composer>();
  const [refresh, setRefresh] = useState(0);
  const [connected, setConnected] = useState(false);
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
  const navigate = (page: Page, job?: string) => { location.hash = page + (job ? "/" + job : ""); setRoute({ page, job }); };
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
  const openResources = (id: string) => {
    if (id !== selectedProject) choose(id);
    navigate("resources");
  };
  const openCommand = () => { if (scope) setComposer("command"); };
  useEffect(() => {
    const navigateHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", navigateHash);
    const events = new EventSource("/job-events");
    events.onmessage = () => { setConnected(true); setRefresh(value => value + 1); };
    events.onerror = () => setConnected(false);
    return () => { window.removeEventListener("hashchange", navigateHash); events.close(); };
  }, []);
  useEffect(() => {
    function keyboard(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k" && scope && !document.querySelector("dialog[open]")) {
        event.preventDefault(); setComposer("command");
      }
    }
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, [scope]);
  useEffect(() => {
    document.querySelector("main.page")?.scrollTo(0, 0);
  }, [route.page, route.job, fileReview?.filename, route.reviewSelection, scope]);
  const nav = (page: Page) => <a href={`#${page}`} aria-current={route.page === page ? "page" : undefined}
    className={`nav${route.page === page ? " nav--active" : ""}`}>{pages[page]}</a>;
  return <div className="shell">
    <aside className="rail">
      <div className="rail__brand">LoRE</div>
      <div className="ws">
        <select className="ws__name" aria-label="切换项目" value={scope} onChange={e => { choose(e.target.value); navigate("home"); }}>
          {!scope && <option value="">选择项目</option>}{workspace.data?.naev && <option value="naev">Naev 示范区</option>}
          {projects.map(item => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
        </select>
        {scope && <div className="ws__meta">{project ? `${project.source_lang.toUpperCase()} → ${project.target_lang.toUpperCase()} · 源 v${project.revision}` : "EN → ZH · 内置"}</div>}
      </div>
      <nav aria-label="工作台导航">
        <div className="nav__label">项目</div>
        {nav("home")}{scope && nav("resources")}{nav("jobs")}{project && nav("knowledge")}{project && nav("models")}
        <div className="nav__label">维护</div>
        {workspace.data?.naev && nav("glossary")}{nav("evolution")}
      </nav>
      <div className="rail__foot">
        <div className={connected ? "live" : "rail__note"}>{connected ? "实时连接" : "正在连接"}</div>
        <div className="rail__note">{workspace.data?.offline ? "离线模式 · 单 worker" : workspace.data ? "在线模式 · 单 worker" : ""}</div>
      </div>
    </aside>
    <div className="canvas">
      <header className="topbar">
        <div className="crumb"><b>{name}</b><i>/</i><span>{pages[route.page]}</span>
          {route.job && <><i>/</i><span className="mono">J-{route.job.slice(0, 6)}</span></>}</div>
        <button type="button" className="cmdk" disabled={!scope} onClick={openCommand}>
          <span className="clip">下达任务，例如「把选中的条目翻译成简体中文并导出」</span><span className="kbd">Ctrl K</span>
        </button>
        <div className="topbar__right"><a className="tray" href="#jobs">任务</a></div>
      </header>
      <main className="page">
        <ErrorNote error={workspace.error || catalog.error || detail.error} />
        {!workspace.data && !workspace.error && <Empty title="正在连接工作台…" />}
        {workspace.data && <>
          {route.page === "home" && <Home project={project} scope={scope} projects={projects} jobs={recent.data}
            offline={workspace.data.offline} naev={workspace.data.naev} choose={id => { choose(id); navigate("resources"); }}
            create={() => setNewProject(true)} openJob={openJob} compose={setComposer} navigate={navigate} />}
          {route.page === "resources" && (scope ? <Resources key={scope} project={project}
            upload={selection.upload && upload?.upload_id === selection.upload ? upload : null} setUpload={value => {
              changeUpload(value); localStorage.setItem("naev-upload", JSON.stringify(value));
            }} selection={selection} setSelection={setSelection} refresh={refresh} changed={changed} openJob={openJob}
            compose={() => setComposer("selection")} openGraph={() => navigate("knowledge")} openFileReview={openFileReview} /> : <Empty title="请先创建项目" />)}
          {route.page === "jobs" && (route.job ? <JobDetail key={`${route.job}:${fileReview?.filename ?? ""}:${route.reviewSelection ?? ""}`}
            id={route.job} refresh={refresh} changed={changed} openJob={openJob} fileReview={fileReview} reviewSelection={route.reviewSelection}
            back={fileReview ? () => openFileReview(fileReview) : () => navigate("jobs")} deliver={openResources} /> :
            fileReview ? <FileReviewQueue key={`${fileReview.projectId}:${fileReview.filename}`} file={fileReview} refresh={refresh}
              back={() => openResources(fileReview.projectId)} openItem={entry => openFileReview(fileReview, entry)} /> :
            <JobList key={scope} scope={scope || undefined} refresh={refresh} openJob={openJob} compose={setComposer} />)}
          {route.page === "knowledge" && (project ? <Knowledge key={scope} project={project} offline={workspace.data.offline}
            selection={selection} openJob={openJob} refresh={refresh} changed={changed} /> : <Empty title="请先选择一个导入项目" />)}
          {route.page === "models" && (project ? <Models key={scope} project={project} offline={workspace.data.offline} changed={changed} /> :
            <Empty title="请先选择一个导入项目" />)}
          {route.page === "glossary" && (workspace.data.naev ? <Glossary selected={scope === "naev" ? selection.ids : []}
            openJob={openJob} refresh={refresh} changed={changed} /> : <Empty title="此工作区未加载 Naev 快照" />)}
          {route.page === "evolution" && <Evolution offline={workspace.data.offline} refresh={refresh} changed={changed} openJob={openJob} />}
        </>}
      </main>
    </div>
    {newProject && <NewProject close={() => setNewProject(false)} created={item => {
      setNewProject(false); choose(item.project_id); setCreatedProject(item); changed(); navigate("resources");
    }} />}
    {composer && <Compose mode={composer} project={project} scope={scope} selection={selection}
      close={() => setComposer(undefined)} openJob={openJob} />}
  </div>;
}

createRoot(document.getElementById("root")!).render(<App />);

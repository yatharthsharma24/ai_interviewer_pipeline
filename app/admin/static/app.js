/* Admin dashboard — no build step, driving the same JSON API the CLI drives.
   Keeping one source of truth for behaviour means there is no second implementation
   of the pipeline logic that can drift from the tested one. */

const view = document.getElementById("view");
const nav = document.getElementById("nav");
const toastHost = document.getElementById("toast");

/* ------------------------------------------------------------------ utilities */

/** Escape anything that came from a user, a candidate, or a model. */
const esc = (v) =>
  v === null || v === undefined
    ? ""
    : String(v).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const num = (v) => (v === null || v === undefined ? "—" : v);

function fmtDateTime(iso, tz) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  try {
    return d.toLocaleString(undefined, {
      weekday: "short", day: "2-digit", month: "short",
      hour: "2-digit", minute: "2-digit",
      ...(tz ? { timeZone: tz } : {}),
    });
  } catch { return d.toLocaleString(); }
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

function stamp(seconds) {
  const m = Math.floor((seconds || 0) / 60), s = Math.floor((seconds || 0) % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

const STATUS_TONE = {
  shortlisted: "ok", scheduled: "ok", graded: "ok", completed: "ok", open: "ok",
  new: "mute", pending: "mute", draft: "mute", notified: "info", in_progress: "info",
  rejected_incomplete: "warn", rejected_rules: "bad", rejected_score: "bad",
  rejected_round: "bad", terminated: "bad", no_show: "bad", failed: "bad",
  partly_notified: "warn", cancelled: "mute", closed: "mute",
};
const pill = (s) => `<span class="pill ${STATUS_TONE[s] || "mute"}">${esc(String(s).replace(/_/g, " "))}</span>`;

const VERDICT_TONE = { strong_hire: "ok", hire: "ok", strong_yes: "ok", yes: "ok",
                       borderline: "warn", maybe: "warn", no_hire: "bad", no: "bad" };
// Failover between OpenAI and Gemini is automatic and invisible to the candidate. Two
// candidates whose interviews ran on different models are not strictly comparable, so the
// record says which one actually served this call.
function providerNote(providers) {
  const used = Object.values(providers || {}).filter(Boolean);
  if (!used.length) return "";
  const unique = [...new Set(used)];
  return ` · <span class="hint">ran on ${esc(unique.join(" + "))}</span>`;
}

const verdictPill = (v) => (v ? `<span class="pill ${VERDICT_TONE[v] || "mute"}">${esc(v.replace(/_/g, " "))}</span>` : "—");

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  toastHost.appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 9000 : 5000);
}

/** Every API call goes through here so an error is never swallowed. */
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const detail = data?.detail;
    const msg = Array.isArray(detail)
      ? detail.map((d) => `${d.loc?.slice(1).join(".") || "field"}: ${d.msg}`).join("\n")
      : detail || text || `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return data;
}

/** Disable a button and show a spinner while a slow operation runs. */
async function busy(btn, label, fn) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> ${esc(label)}`;
  try { return await fn(); }
  finally { btn.disabled = false; btn.innerHTML = original; }
}

/** Wire every [data-act] button in the current view to a handler. */
function on(act, handler) {
  view.querySelectorAll(`[data-act="${act}"]`).forEach((el) => {
    el.addEventListener("click", (e) => handler(e.currentTarget, e));
  });
}

const formValues = (root) => {
  const out = {};
  root.querySelectorAll("[name]").forEach((el) => {
    out[el.name] = el.type === "checkbox" ? el.checked : el.value;
  });
  return out;
};

const numOrNull = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
const strOrNull = (v) => (v === "" || v === null || v === undefined ? null : v);
const csv = (v) => (v || "").split(",").map((s) => s.trim()).filter(Boolean);

/* ------------------------------------------------------------------ router */

const routes = [];
const route = (pattern, handler) => routes.push({ pattern, handler });

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, qs] = raw.split("?");
  return { path, query: new URLSearchParams(qs || "") };
}

async function render() {
  const { path, query } = parseHash();
  for (const { pattern, handler } of routes) {
    const names = [];
    const rx = new RegExp(
      "^" + pattern.replace(/:([a-z]+)/gi, (_, n) => { names.push(n); return "([^/]+)"; }) + "$"
    );
    const m = path.match(rx);
    if (!m) continue;
    const params = Object.fromEntries(names.map((n, i) => [n, m[i + 1]]));
    view.innerHTML = `<div class="empty">Loading…</div>`;
    try {
      await handler(params, query);
    } catch (err) {
      view.innerHTML = `<div class="err">${esc(err.message)}</div>`;
    }
    await paintNav(path);
    window.scrollTo(0, 0);
    return;
  }
  view.innerHTML = `<h1>Not found</h1><p class="lede">No such page.</p><a href="#/">Back to overview</a>`;
}

window.addEventListener("hashchange", render);

/* ------------------------------------------------------------------ nav */

let navJobs = [];

async function paintNav(active) {
  try { navJobs = await api("GET", "/jobs"); } catch { /* keep the last list */ }
  const item = (href, label, extra = "") =>
    `<a href="${href}" class="${active === href.slice(1) ? "on" : ""}">${esc(label)}${extra}</a>`;

  nav.innerHTML =
    item("#/", "Overview") +
    item("#/system", "System check") +
    `<div class="grp">Jobs</div>` +
    (navJobs.length
      ? navJobs.map((j) => item(`#/jobs/${j.id}`, j.title, ` <span class="pill mute">${esc(j.status)}</span>`)).join("")
      : `<div class="hint" style="padding:0 10px">None yet</div>`) +
    item("#/jobs/new", "+ New job");
}

/* ------------------------------------------------------------------ overview */

route("/", async () => {
  const [status, jobs] = await Promise.all([
    api("GET", "/system/status").catch(() => null),
    api("GET", "/jobs"),
  ]);

  const stats = await Promise.all(
    jobs.map((j) => api("GET", `/jobs/${j.id}/stats`).catch(() => null))
  );

  const bad = status?.checks.filter((c) => c.state === "error") || [];
  const warn = status?.checks.filter((c) => c.state === "warn") || [];

  view.innerHTML = `
    <h1>Overview</h1>
    <p class="lede">Screening, scheduling, and AI voice interviews for every open role.</p>

    <div class="panel">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">System</h2>
        <a class="btn sec" href="#/system">Full check</a>
      </div>
      ${
        !status
          ? `<p class="hint">Status unavailable.</p>`
          : bad.length || warn.length
          ? `<div style="margin-top:12px">${[...bad, ...warn]
              .map(
                (c) => `<div class="chk"><span class="lamp ${c.state}"></span>
                  <div><b>${esc(c.name)}</b><p>${esc(c.detail)}</p>
                  ${c.fix ? `<div class="fix">${esc(c.fix)}</div>` : ""}</div></div>`
              )
              .join("")}</div>`
          : `<p class="hint" style="margin-top:10px">All ${status.checks.length} checks are healthy.</p>`
      }
    </div>

    <h2>Jobs</h2>
    ${
      jobs.length === 0
        ? `<div class="panel"><div class="empty">No jobs yet. <a href="#/jobs/new">Create one</a> to start.</div></div>`
        : `<div class="panel scroll"><table>
            <thead><tr><th>Role</th><th>Status</th><th>Form</th>
              <th class="num">Applied</th><th class="num">Shortlisted</th>
              <th class="num">Scheduled</th><th class="num">Rounds</th></tr></thead>
            <tbody>${jobs
              .map((j, i) => {
                const s = stats[i];
                const by = s?.by_status || {};
                return `<tr class="click" data-go="#/jobs/${j.id}">
                  <td><b>${esc(j.title)}</b>${j.department ? `<div class="hint">${esc(j.department)}</div>` : ""}</td>
                  <td>${pill(j.status)}</td>
                  <td>${j.form_url ? `<span class="pill ok">linked</span>` : `<span class="pill mute">none</span>`}</td>
                  <td class="num">${s ? s.total : "—"}</td>
                  <td class="num">${by.shortlisted || 0}</td>
                  <td class="num">${by.scheduled || 0}</td>
                  <td class="num">${s ? s.rounds : "—"}</td>
                </tr>`;
              })
              .join("")}</tbody></table></div>`
    }`;

  view.querySelectorAll("[data-go]").forEach((el) =>
    el.addEventListener("click", () => (location.hash = el.dataset.go))
  );
});

/* ------------------------------------------------------------------ system check */

route("/system", async (_p, query) => {
  const probe = query.get("probe") === "1";
  const status = await api("GET", `/system/status?probe=${probe}`);
  const tone = { ok: "ok", warn: "warn", error: "bad" }[status.overall];

  view.innerHTML = `
    <h1>System check ${`<span class="pill ${tone}">${esc(status.overall)}</span>`}</h1>
    <p class="lede">Every component the pipeline depends on.
      ${probe ? "Live probes ran." : "Configuration only — run a deep check to make live calls."}</p>
    <div class="row" style="margin-bottom:16px">
      <button data-act="probe">Run deep check</button>
      <button class="sec" data-act="again">Refresh</button>
      <a class="btn sec" href="/docs" target="_blank" rel="noopener">API docs</a>
    </div>
    <div class="panel">
      ${status.checks
        .map(
          (c) => `<div class="chk">
            <span class="lamp ${c.state}"></span>
            <div style="flex:1">
              <b>${esc(c.name)} <span class="pill ${
                { ok: "ok", warn: "warn", error: "bad", off: "mute" }[c.state]
              }">${esc(c.state)}</span></b>
              <p>${esc(c.detail)}</p>
              ${c.fix ? `<div class="fix">→ ${esc(c.fix)}</div>` : ""}
            </div>
          </div>`
        )
        .join("")}
    </div>`;

  on("probe", (btn) =>
    busy(btn, "Probing…", async () => {
      location.hash = "#/system?probe=1";
      if (probe) await render();
    })
  );
  on("again", () => render());
});

/* ------------------------------------------------------------------ new job */

route("/jobs/new", async () => {
  view.innerHTML = `
    <div class="crumb"><a href="#/">Overview</a> / New job</div>
    <h1>New job opening</h1>
    <p class="lede">Everything the screener needs. You can change any of it later.</p>
    <form id="f">
      <div class="panel">
        <h3>Role</h3>
        <div class="grid2">
          <div><label>Title *</label><input name="title" required placeholder="Senior Backend Engineer"></div>
          <div><label>Department</label><input name="department" placeholder="Platform"></div>
          <div><label>Location</label><input name="location" placeholder="Bengaluru"></div>
          <div><label>Employment type</label><input name="employment_type" value="full-time"></div>
        </div>
        <label>Description</label>
        <textarea name="description" placeholder="Own and scale our payments and billing services."></textarea>
        <label>Responsibilities (comma separated)</label>
        <input name="responsibilities" placeholder="Design REST APIs, Own service reliability">
      </div>

      <div class="panel">
        <h3>Requirements</h3>
        <div class="grid2">
          <div><label>Minimum years of experience</label><input name="min_years_experience" type="number" step="0.5" value="0"></div>
          <div><label>Maximum years (optional)</label><input name="max_years_experience" type="number" step="0.5"></div>
        </div>
        <label>Must-have skills (comma separated)</label>
        <input name="required_skills" placeholder="Python, Django, PostgreSQL">
        <div class="hint">These drive the hard filter, the generated form's skills question, and the interview questions.</div>
        <label>Nice-to-have skills</label>
        <input name="preferred_skills" placeholder="Docker, AWS">
        <div class="grid3">
          <div><label>Education requirement</label><input name="education_requirement"></div>
          <div><label>Max notice period (days)</label><input name="max_notice_period_days" type="number"></div>
          <div><label>Compensation ceiling</label><input name="max_expected_ctc" type="number"></div>
        </div>
      </div>

      <div class="panel">
        <h3>Screening</h3>
        <label>Strictness</label>
        <select name="strictness">
          <option value="lenient">lenient — cutoff 45, 34% of must-haves, ±2.0 yrs</option>
          <option value="balanced" selected>balanced — cutoff 62, 60% of must-haves, ±1.0 yrs</option>
          <option value="strict">strict — cutoff 76, 85% of must-haves, ±0.5 yrs</option>
          <option value="very_strict">very_strict — cutoff 87, all must-haves, no tolerance</option>
        </select>
        <label>Extra guidance for the screener</label>
        <textarea name="screening_notes" placeholder="Weight production experience over side projects."></textarea>
      </div>

      <div class="row end"><a class="btn sec" href="#/">Cancel</a><button data-act="save">Create job</button></div>
    </form>`;

  on("save", (btn, e) => {
    e.preventDefault();
    const f = view.querySelector("#f");
    if (!f.reportValidity()) return;
    const v = formValues(f);
    return busy(btn, "Creating…", async () => {
      try {
        const job = await api("POST", "/jobs", {
          title: v.title,
          department: strOrNull(v.department),
          location: strOrNull(v.location),
          employment_type: strOrNull(v.employment_type),
          description: strOrNull(v.description),
          responsibilities: csv(v.responsibilities),
          min_years_experience: Number(v.min_years_experience || 0),
          max_years_experience: numOrNull(v.max_years_experience),
          required_skills: csv(v.required_skills),
          preferred_skills: csv(v.preferred_skills),
          education_requirement: strOrNull(v.education_requirement),
          max_notice_period_days: numOrNull(v.max_notice_period_days),
          max_expected_ctc: numOrNull(v.max_expected_ctc),
          strictness: v.strictness,
          screening_notes: strOrNull(v.screening_notes),
        });
        toast(`Created “${job.title}”.`, "ok");
        location.hash = `#/jobs/${job.id}`;
      } catch (err) { toast(err.message, "bad"); }
    });
  });
});

/* ------------------------------------------------------------------ job detail */

const jobTabs = (id, on_) => `
  <div class="tabs">
    <a href="#/jobs/${id}" class="${on_ === "overview" ? "on" : ""}">Overview</a>
    <a href="#/jobs/${id}/candidates" class="${on_ === "candidates" ? "on" : ""}">Candidates</a>
    <a href="#/jobs/${id}/rounds" class="${on_ === "rounds" ? "on" : ""}">Interview rounds</a>
  </div>`;

route("/jobs/:id", async ({ id }) => {
  const [job, stats] = await Promise.all([
    api("GET", `/jobs/${id}`),
    api("GET", `/jobs/${id}/stats`).catch(() => null),
  ]);
  const by = stats?.by_status || {};
  const seg = [
    ["shortlisted", "#35c46b", by.shortlisted || 0],
    ["scheduled", "#4f8cff", by.scheduled || 0],
    ["rejected — score", "#f0b429", by.rejected_score || 0],
    ["rejected — rules", "#f2545b", by.rejected_rules || 0],
    ["rejected — incomplete", "#a15c3a", by.rejected_incomplete || 0],
    ["rejected — round", "#8b3a40", by.rejected_round || 0],
    ["unscreened", "#4a5063", by.new || 0],
  ].filter(([, , n]) => n > 0);
  const total = seg.reduce((a, [, , n]) => a + n, 0) || 1;

  view.innerHTML = `
    <div class="crumb"><a href="#/">Overview</a> / ${esc(job.title)}</div>
    <h1>${esc(job.title)} ${pill(job.status)}</h1>
    <p class="lede">${esc([job.department, job.location, job.employment_type].filter(Boolean).join(" · ") || "No details set")}</p>
    ${jobTabs(id, "overview")}

    <div class="panel">
      <h3>Funnel</h3>
      <div class="stats">
        <div class="stat"><b>${stats?.total ?? 0}</b><span>Applied</span></div>
        <div class="stat"><b>${by.shortlisted || 0}</b><span>Shortlisted</span></div>
        <div class="stat"><b>${by.scheduled || 0}</b><span>Scheduled</span></div>
        <div class="stat"><b>${stats?.rounds ?? 0}</b><span>Rounds</span></div>
      </div>
      ${seg.length ? `<div class="funnel">${seg.map(([, c, n]) => `<i style="background:${c};flex:${n}"></i>`).join("")}</div>
        <div class="legend">${seg.map(([l, c, n]) => `<span><em style="background:${c}"></em>${esc(l)} ${n}</span>`).join("")}</div>` : ""}
    </div>

    <div class="cols">
      <div class="panel">
        <h3>Application form</h3>
        ${
          job.form_url
            ? `<dl class="kv">
                 <dt>Share link</dt><dd><a class="mono" href="${esc(job.form_url)}" target="_blank" rel="noopener">${esc(job.form_url)}</a></dd>
                 <dt>Edit</dt><dd><a class="mono" href="${esc(job.form_edit_url)}" target="_blank" rel="noopener">open in Google Forms</a></dd>
                 <dt>Mapped fields</dt><dd>${Object.keys(job.question_map || {}).length}</dd>
               </dl>
               <div class="row" style="margin-top:12px">
                 <button class="sec" data-act="send">Email the link…</button>
                 <button class="sec" data-act="sync">Sync responses</button>
               </div>`
            : `<p class="hint">No form attached yet.</p>
               <div class="row" style="margin-top:10px">
                 <button data-act="create-form">Generate a form</button>
                 <button class="sec" data-act="link-form">Link an existing form…</button>
               </div>
               <div class="hint" style="margin-top:8px">Generating builds the questions from this job spec. Linking adopts a form you built by hand and fuzzy-matches its questions.</div>`
        }
      </div>

      <div class="panel">
        <h3>Screening</h3>
        <dl class="kv">
          <dt>Strictness</dt><dd>${pill(job.strictness)}</dd>
          <dt>Must-have skills</dt><dd>${esc((job.required_skills || []).join(", ") || "none")}</dd>
          <dt>Nice to have</dt><dd>${esc((job.preferred_skills || []).join(", ") || "none")}</dd>
          <dt>Min experience</dt><dd>${job.min_years_experience} years</dd>
          <dt>Mandatory answers</dt><dd>${esc((job.required_fields || []).join(", "))}</dd>
        </dl>
        <div class="row" style="margin-top:12px">
          <button data-act="screen">Screen candidates</button>
          <button class="sec" data-act="screen-norules">Rules only (no model)</button>
        </div>
        <div class="hint" style="margin-top:8px">Screening only calls the model for candidates that clear the hard filters. With a local model this can take ~25s each.</div>
      </div>
    </div>

    <div class="panel">
      <h3>Danger zone</h3>
      <div class="row"><button class="danger" data-act="delete">Delete this job and all its candidates</button></div>
    </div>`;

  on("create-form", (btn) =>
    busy(btn, "Building the form…", async () => {
      try { await api("POST", `/jobs/${id}/form`, {}); toast("Form created.", "ok"); render(); }
      catch (e) { toast(e.message, "bad"); }
    })
  );

  on("link-form", async () => {
    const ref = prompt("Paste the Google Form URL or its ID:");
    if (!ref) return;
    try {
      const info = await api("POST", `/jobs/${id}/form/link`, { form_ref: ref });
      const mapped = Object.keys(info.question_map || {}).length;
      const un = (info.unmapped_questions || []).length;
      toast(`Linked. ${mapped} question(s) mapped${un ? `, ${un} not recognised` : ""}.`, "ok");
      render();
    } catch (e) { toast(e.message, "bad"); }
  });

  on("send", async () => {
    const to = prompt("Email the form link to (comma separated):");
    if (!to) return;
    try {
      const r = await api("POST", `/jobs/${id}/form/send`, { recipients: csv(to) });
      toast(`Sent to ${r.recipients} recipient(s).`, "ok");
    } catch (e) { toast(e.message, "bad"); }
  });

  on("sync", (btn) =>
    busy(btn, "Syncing…", async () => {
      try {
        const r = await api("POST", `/jobs/${id}/sync`, {});
        toast(`Fetched ${r.fetched} response(s): ${r.created} new, ${r.updated} updated.`, "ok");
        render();
      } catch (e) { toast(e.message, "bad"); }
    })
  );

  const runScreen = (btn, useLlm) =>
    busy(btn, "Screening…", async () => {
      try {
        const r = await api("POST", `/jobs/${id}/screen`, { use_llm: useLlm, rescreen: false });
        toast(
          `Screened ${r.total}: ${r.shortlisted} shortlisted, ` +
            `${r.rejected_incomplete} incomplete, ${r.rejected_rules} failed rules, ` +
            `${r.rejected_score} below cutoff.` + (r.errors.length ? `\n${r.errors.length} error(s).` : ""),
          r.errors.length ? "bad" : "ok"
        );
        render();
      } catch (e) { toast(e.message, "bad"); }
    });
  on("screen", (btn) => runScreen(btn, true));
  on("screen-norules", (btn) => runScreen(btn, false));

  on("delete", async () => {
    if (!confirm(`Delete “${job.title}” and every candidate on it? This cannot be undone.`)) return;
    try { await api("DELETE", `/jobs/${id}`); toast("Job deleted.", "ok"); location.hash = "#/"; }
    catch (e) { toast(e.message, "bad"); }
  });
});

/* ------------------------------------------------------------------ candidates */

route("/jobs/:id/candidates", async ({ id }, query) => {
  const filter = query.get("status") || "";
  const [job, candidates] = await Promise.all([
    api("GET", `/jobs/${id}`),
    api("GET", `/jobs/${id}/candidates${filter ? `?status=${filter}` : ""}`),
  ]);

  const options = ["", "shortlisted", "scheduled", "rejected_score", "rejected_rules",
                   "rejected_incomplete", "rejected_round", "new"];

  view.innerHTML = `
    <div class="crumb"><a href="#/">Overview</a> / <a href="#/jobs/${id}">${esc(job.title)}</a> / Candidates</div>
    <h1>Candidates</h1>
    <p class="lede">${candidates.length} shown. Sorted by fit score.</p>
    ${jobTabs(id, "candidates")}
    <div class="row" style="margin-bottom:14px">
      <select id="filter" style="width:auto">
        ${options.map((o) => `<option value="${o}" ${o === filter ? "selected" : ""}>${o ? esc(o.replace(/_/g, " ")) : "all statuses"}</option>`).join("")}
      </select>
    </div>
    ${
      candidates.length === 0
        ? `<div class="panel"><div class="empty">Nothing here yet. Sync responses on the job page, then screen them.</div></div>`
        : `<div class="panel scroll"><table>
            <thead><tr><th>Name</th><th>Email</th><th class="num">Exp</th>
              <th class="num">Score</th><th>Verdict</th><th>Status</th><th>Why</th></tr></thead>
            <tbody>${candidates
              .map(
                (c) => `<tr class="click" data-go="#/candidates/${c.id}">
                  <td><b>${esc(c.full_name || "—")}</b></td>
                  <td class="mono">${esc(c.email || "—")}</td>
                  <td class="num">${c.years_experience ?? "—"}</td>
                  <td class="num">${c.fit_score ?? "—"}</td>
                  <td>${verdictPill(c.recommendation)}</td>
                  <td>${pill(c.status)}</td>
                  <td>${esc((c.rationale || "").slice(0, 90))}${(c.rationale || "").length > 90 ? "…" : ""}</td>
                </tr>`
              )
              .join("")}</tbody></table></div>`
    }`;

  view.querySelector("#filter").addEventListener("change", (e) => {
    location.hash = `#/jobs/${id}/candidates${e.target.value ? `?status=${e.target.value}` : ""}`;
  });
  view.querySelectorAll("[data-go]").forEach((el) =>
    el.addEventListener("click", () => (location.hash = el.dataset.go))
  );
});

route("/candidates/:cid", async ({ cid }) => {
  const c = await api("GET", `/candidates/${cid}`);
  const a = c.assessment || {};
  const list = (items) => (items || []).map((s) => `<li>${esc(s)}</li>`).join("");

  view.innerHTML = `
    <div class="crumb"><a href="#/jobs/${c.job_id}/candidates">Candidates</a> / ${esc(c.full_name || "candidate")}</div>
    <h1>${esc(c.full_name || "Candidate")} ${pill(c.status)}</h1>
    <p class="lede">${c.fit_score !== null ? `Fit score <b>${c.fit_score}</b> · ${esc(c.recommendation || "")}` : "Not scored"}</p>

    <div class="cols">
      <div class="panel">
        <h3>Profile</h3>
        <dl class="kv">
          <dt>Email</dt><dd class="mono">${esc(c.email || "—")}</dd>
          <dt>Phone</dt><dd class="mono">${esc(c.phone || "—")}</dd>
          <dt>Experience</dt><dd>${c.years_experience ?? "—"} years</dd>
          <dt>Current role</dt><dd>${esc(c.current_role || "—")}</dd>
          <dt>Company</dt><dd>${esc(c.current_company || "—")}</dd>
          <dt>Skills</dt><dd>${esc((c.skills || []).join(", ") || "—")}</dd>
          <dt>Education</dt><dd>${esc(c.education || "—")}</dd>
          <dt>Location</dt><dd>${esc(c.location || "—")}</dd>
          <dt>Notice period</dt><dd>${c.notice_period_days ?? "—"} days</dd>
          <dt>Expected CTC</dt><dd>${c.expected_ctc ?? "—"}</dd>
          <dt>Resume</dt><dd>${c.resume_url ? `<a class="mono" href="${esc(c.resume_url)}" target="_blank" rel="noopener">open</a>` : "—"}</dd>
          <dt>LinkedIn</dt><dd>${c.linkedin ? `<a class="mono" href="${esc(c.linkedin)}" target="_blank" rel="noopener">open</a>` : "—"}</dd>
        </dl>
        ${c.cover_note ? `<h3>In their own words</h3><p>${esc(c.cover_note)}</p>` : ""}
      </div>

      <div class="panel">
        <h3>Screening verdict</h3>
        ${c.rationale ? `<p>${esc(c.rationale)}</p>` : `<p class="hint">Not screened yet.</p>`}
        ${c.missing_fields?.length ? `<h3>Missing answers</h3><ul class="tight">${list(c.missing_fields)}</ul>` : ""}
        ${c.rule_failures?.length ? `<h3>Failed hard rules</h3><ul class="tight">${list(c.rule_failures)}</ul>` : ""}
        ${a.strengths?.length ? `<h3>Strengths</h3><ul class="tight">${list(a.strengths)}</ul>` : ""}
        ${a.concerns?.length ? `<h3>Concerns</h3><ul class="tight">${list(a.concerns)}</ul>` : ""}
        ${a.missing_required_skills?.length ? `<h3>Skills with no evidence</h3><ul class="tight">${list(a.missing_required_skills)}</ul>` : ""}
        ${
          a.model_fit_score
            ? `<div class="hint" style="margin-top:12px">Score capped from ${a.model_fit_score} to ${c.fit_score}:
               the application listed skills without describing what was built with them.</div>`
            : ""
        }
      </div>
    </div>`;
});

/* ------------------------------------------------------------------ rounds */

route("/jobs/:id/rounds", async ({ id }) => {
  const [job, rounds] = await Promise.all([
    api("GET", `/jobs/${id}`),
    api("GET", `/jobs/${id}/rounds`),
  ]);

  view.innerHTML = `
    <div class="crumb"><a href="#/">Overview</a> / <a href="#/jobs/${id}">${esc(job.title)}</a> / Rounds</div>
    <h1>Interview rounds</h1>
    <p class="lede">Set an acceptable score, allocate slots, then invite.</p>
    ${jobTabs(id, "rounds")}
    <div class="row" style="margin-bottom:14px"><a class="btn" href="#/jobs/${id}/rounds/new">+ New round</a></div>
    ${
      rounds.length === 0
        ? `<div class="panel"><div class="empty">No rounds yet.</div></div>`
        : `<div class="panel scroll"><table>
            <thead><tr><th>Name</th><th class="num">Bar</th><th>Difficulty</th>
              <th>From</th><th>Slots</th><th>Status</th></tr></thead>
            <tbody>${rounds
              .map(
                (r) => `<tr class="click" data-go="#/rounds/${r.id}">
                  <td><b>${esc(r.name)}</b></td>
                  <td class="num">${r.acceptable_score}</td>
                  <td>${pill(r.difficulty)}</td>
                  <td>${esc(r.start_date)} ${esc(r.day_start_time)}</td>
                  <td>${r.slot_minutes} min</td>
                  <td>${pill(r.status)}</td>
                </tr>`
              )
              .join("")}</tbody></table></div>`
    }`;

  view.querySelectorAll("[data-go]").forEach((el) =>
    el.addEventListener("click", () => (location.hash = el.dataset.go))
  );
});

route("/jobs/:id/rounds/new", async ({ id }) => {
  const job = await api("GET", `/jobs/${id}`);
  const today = new Date(Date.now() + 86400000).toISOString().slice(0, 10);

  view.innerHTML = `
    <div class="crumb"><a href="#/jobs/${id}/rounds">${esc(job.title)}</a> / New round</div>
    <h1>New interview round</h1>
    <form id="f">
      <div class="panel">
        <h3>Who gets invited</h3>
        <div class="grid2">
          <div><label>Round name</label><input name="name" value="Technical Round 1"></div>
          <div><label>Acceptable score</label><input name="acceptable_score" type="number" min="0" max="100" value="70"></div>
        </div>
        <div class="hint">Candidates below this score are marked rejected and are <b>not</b> messaged.
          Part 1 already filtered below this job's cutoff, so a lower bar here rejects nobody.</div>
      </div>

      <div class="panel">
        <h3>When</h3>
        <div class="grid3">
          <div><label>First date</label><input name="start_date" type="date" value="${today}" required></div>
          <div><label>Day starts</label><input name="day_start_time" type="time" value="10:00"></div>
          <div><label>Day ends</label><input name="day_end_time" type="time" value="17:00"></div>
        </div>
        <div class="grid3">
          <div><label>Slot length (min)</label><input name="slot_minutes" type="number" value="30" min="5"></div>
          <div><label>Break between (min)</label><input name="break_minutes" type="number" value="0" min="0"></div>
          <div><label>Timezone</label><input name="timezone" value="Asia/Kolkata"></div>
        </div>
        <div class="check"><input type="checkbox" name="skip_weekends" id="sw" checked><label for="sw">Skip weekends</label></div>
      </div>

      <div class="panel">
        <h3>The interview</h3>
        <div class="grid2">
          <div><label>Mode</label>
            <select name="mode"><option value="online" selected>online</option><option value="onsite">onsite</option><option value="phone">phone</option></select></div>
          <div><label>AI interview difficulty</label>
            <select name="difficulty">
              <option value="easy">easy — 5 questions, accepts a reasonable answer</option>
              <option value="medium" selected>medium — 7 questions, one follow-up on vagueness</option>
              <option value="hard">hard — 8 questions, probes to the edge of knowledge</option>
              <option value="expert">expert — 9 questions, senior-staff level pressure</option>
            </select></div>
        </div>
        <label>Meeting link or address</label>
        <input name="location" placeholder="Leave blank — AI interview links are generated per candidate">
        <div class="hint">If you prepare AI interviews, each candidate's invitation carries their own link and this is ignored.</div>
        <label>Extra instructions for the candidate</label>
        <textarea name="instructions"></textarea>
        <label>Reply-to contact email</label>
        <input name="contact_email" type="email">
      </div>

      <div class="row end"><a class="btn sec" href="#/jobs/${id}/rounds">Cancel</a><button data-act="save">Create round</button></div>
    </form>`;

  on("save", (btn, e) => {
    e.preventDefault();
    const f = view.querySelector("#f");
    if (!f.reportValidity()) return;
    const v = formValues(f);
    return busy(btn, "Creating…", async () => {
      try {
        const r = await api("POST", `/jobs/${id}/rounds`, {
          name: v.name,
          acceptable_score: Number(v.acceptable_score),
          start_date: v.start_date,
          day_start_time: v.day_start_time,
          day_end_time: v.day_end_time,
          slot_minutes: Number(v.slot_minutes),
          break_minutes: Number(v.break_minutes),
          skip_weekends: v.skip_weekends,
          timezone: v.timezone,
          mode: v.mode,
          difficulty: v.difficulty,
          location: strOrNull(v.location),
          instructions: strOrNull(v.instructions),
          contact_email: strOrNull(v.contact_email),
        });
        toast("Round created.", "ok");
        location.hash = `#/rounds/${r.id}`;
      } catch (err) { toast(err.message, "bad"); }
    });
  });
});

/* ------------------------------------------------------------------ round detail */

route("/rounds/:rid", async ({ rid }) => {
  const round = await api("GET", `/rounds/${rid}`);
  const [slots, interviews] = await Promise.all([
    api("GET", `/rounds/${rid}/slots`).catch(() => []),
    api("GET", `/rounds/${rid}/interviews`).catch(() => []),
  ]);
  const byCandidate = Object.fromEntries(interviews.map((i) => [i.candidate_id, i]));

  view.innerHTML = `
    <div class="crumb"><a href="#/jobs/${round.job_id}/rounds">Rounds</a> / ${esc(round.name)}</div>
    <h1>${esc(round.name)} ${pill(round.status)}</h1>
    <p class="lede">Bar ${round.acceptable_score} · ${esc(round.difficulty)} difficulty ·
      ${esc(round.start_date)} from ${esc(round.day_start_time)} · ${round.slot_minutes} min slots ·
      ${esc(round.timezone)}</p>

    <div class="panel">
      <h3>1 · Who gets in, and when</h3>
      <div class="row">
        <button class="sec" data-act="preview">Preview</button>
        <button data-act="apply">Apply schedule</button>
      </div>
      <div class="hint" style="margin-top:8px">Preview changes nothing. Applying marks
        below-the-bar candidates rejected and assigns slots — still nothing is sent.</div>
      <div id="sched"></div>
    </div>

    <div class="panel">
      <h3>2 · Prepare the AI interviews</h3>
      <div class="row">
        <select id="diff" style="width:auto">
          ${["easy", "medium", "hard", "expert"]
            .map((d) => `<option value="${d}" ${d === round.difficulty ? "selected" : ""}>${d}</option>`)
            .join("")}
        </select>
        <button data-act="prepare">Prepare interviews</button>
      </div>
      <div class="hint" style="margin-top:8px">Generates a question plan per candidate from their
        resume and screening notes, and mints their personal join link. Do this <b>before</b>
        sending invitations so the link is included.</div>
    </div>

    <div class="panel">
      <h3>3 · Send the invitations</h3>
      <div class="row">
        <button class="sec" data-act="dry">Dry run</button>
        <button data-act="send">Send for real</button>
        <label class="check" style="margin:0"><input type="checkbox" id="retry"> re-send to everyone</label>
      </div>
      <div class="hint" style="margin-top:8px">Each candidate gets their own time and their own
        interview link, by email. Re-running only retries failures.</div>
    </div>

    <h2>Schedule</h2>
    ${
      slots.length === 0
        ? `<div class="panel"><div class="empty">No slots yet — apply the schedule above.</div></div>`
        : `<div class="panel scroll"><table>
            <thead><tr><th>Candidate</th><th>When</th><th>Invitation</th>
              <th>Interview</th><th class="num">Rating</th><th>Verdict</th><th class="num">Flags</th></tr></thead>
            <tbody>${slots
              .map((s) => {
                const iv = byCandidate[s.candidate_id];
                const c = s.candidate || {};
                return `<tr>
                  <td><a href="#/candidates/${s.candidate_id}">${esc(c.full_name || "#" + s.candidate_id)}</a>
                    <div class="hint">${esc(c.email || "")}</div></td>
                  <td>${fmtDateTime(s.scheduled_start, round.timezone)}</td>
                  <td>${pill(s.status)}${
                    Object.keys(s.notify_errors || {}).length
                      ? `<div class="hint" style="color:var(--bad)">${esc(Object.values(s.notify_errors).join("; ").slice(0, 70))}</div>`
                      : ""
                  }</td>
                  <td>${iv ? `<a href="#/interviews/${iv.id}">${pill(iv.status)}</a>` : `<span class="pill mute">not prepared</span>`}</td>
                  <td class="num">${iv?.overall_rating ?? "—"}</td>
                  <td>${verdictPill(iv?.recommendation)}</td>
                  <td class="num">${iv ? iv.violation_count : "—"}</td>
                </tr>`;
              })
              .join("")}</tbody></table></div>`
    }`;

  const sched = view.querySelector("#sched");

  const runSchedule = (btn, apply) =>
    busy(btn, apply ? "Applying…" : "Previewing…", async () => {
      try {
        const r = await api("POST", `/rounds/${rid}/schedule`, { apply });
        sched.innerHTML = `
          <div class="${apply ? "ok-box" : "panel"}" style="margin-top:14px">
            <b>${r.invited} invited · ${r.rejected} rejected${r.unscored ? ` · ${r.unscored} unscored` : ""}</b>
            <div class="hint" style="margin-top:6px">${esc(r.summary)}</div>
            ${
              r.rejected_candidates.length
                ? `<div style="margin-top:10px"><b>Below the bar</b><ul class="tight">${r.rejected_candidates
                    .map((c) => `<li>${esc(c.full_name)} — ${c.fit_score}</li>`).join("")}</ul></div>`
                : ""
            }
            ${
              r.unscored_candidates.length
                ? `<div style="margin-top:10px"><b>No fit score — decide manually</b><ul class="tight">${r.unscored_candidates
                    .map((c) => `<li>${esc(c.full_name)}</li>`).join("")}</ul></div>`
                : ""
            }
          </div>`;
        if (apply) { toast(`Scheduled ${r.invited} candidate(s).`, "ok"); render(); }
      } catch (e) { toast(e.message, "bad"); }
    });

  on("preview", (btn) => runSchedule(btn, false));
  on("apply", (btn) => runSchedule(btn, true));

  on("prepare", (btn) =>
    busy(btn, "Planning questions…", async () => {
      try {
        const list = await api("POST", `/rounds/${rid}/interviews/prepare`, {
          difficulty: view.querySelector("#diff").value,
          plan_questions: true,
          fetch_resumes: true,
        });
        toast(`Prepared ${list.length} interview(s) with join links.`, "ok");
        render();
      } catch (e) { toast(e.message, "bad"); }
    })
  );

  const runNotify = (btn, dry) => {
    if (!dry && !confirm("This will message real candidates. Continue?")) return;
    return busy(btn, dry ? "Rendering…" : "Sending…", async () => {
      try {
        const r = await api("POST", `/rounds/${rid}/notify`, {
          dry_run: dry ? true : false,
          retry_all: view.querySelector("#retry").checked,
        });
        toast(
          `${r.dry_run ? "DRY RUN — nothing sent. " : ""}sent ${r.sent}, partial ${r.partial}, ` +
            `failed ${r.failed}, skipped ${r.skipped}` +
            (r.errors.length ? `\n${r.errors.slice(0, 3).join("\n")}` : ""),
          r.failed ? "bad" : "ok"
        );
        render();
      } catch (e) { toast(e.message, "bad"); }
    });
  };
  on("dry", (btn) => runNotify(btn, true));
  on("send", (btn) => runNotify(btn, false));
});

/* ------------------------------------------------------------------ interview */

route("/interviews/:iid", async ({ iid }) => {
  const iv = await api("GET", `/interviews/${iid}`);
  const r = iv.ratings || {};
  const plan = iv.question_plan || {};
  const questions = Array.isArray(plan) ? plan : plan.questions || [];
  const list = (items) => (items || []).map((s) => `<li>${esc(s)}</li>`).join("");

  view.innerHTML = `
    <div class="crumb"><a href="#/rounds/${iv.round_id}">Round ${iv.round_id}</a> / Interview ${iv.id}</div>
    <h1>${esc(iv.candidate_name || "Interview")} ${pill(iv.status)}</h1>
    <p class="lede">${esc(iv.difficulty)} difficulty · ${fmtDuration(iv.duration_seconds)} ·
      ${iv.violation_count} proctoring flag(s)${providerNote(iv.providers)}</p>

    <div class="panel">
      <h3>Join link</h3>
      <p class="mono"><a href="${esc(iv.join_url)}" target="_blank" rel="noopener">${esc(iv.join_url)}</a></p>
      <div class="row">
        <button class="sec" data-act="copy">Copy link</button>
        ${iv.turns.length && iv.status !== "graded" ? `<button data-act="grade">Grade this interview</button>` : ""}
        ${iv.status === "graded" ? `<button class="sec" data-act="grade">Re-grade</button>` : ""}
      </div>
    </div>

    ${
      iv.summary
        ? `<div class="panel">
            <div class="row" style="justify-content:space-between">
              <h3 style="margin:0">Report</h3>
              <div><b style="font-size:22px">${iv.overall_rating}</b><span class="hint">/100</span> ${verdictPill(iv.recommendation)}</div>
            </div>
            <p style="margin-top:12px">${esc(iv.summary)}</p>
            ${
              r.competencies?.length
                ? `<div class="scroll"><table style="margin-top:10px">
                    <thead><tr><th>Competency</th><th>Level</th><th class="num">Score</th><th>Evidence</th></tr></thead>
                    <tbody>${r.competencies
                      .map((c) => `<tr><td>${esc(c.competency)}</td>
                        <td>${pill(c.level === "not_demonstrated" ? "new" : c.level === "weak" ? "rejected_score" : "shortlisted")}
                          <span class="hint">${esc(c.level.replace(/_/g, " "))}</span></td>
                        <td class="num">${c.score}</td><td>${esc(c.evidence)}</td></tr>`)
                      .join("")}</tbody></table></div>`
                : ""
            }
            ${r.strengths?.length ? `<h3>Strengths</h3><ul class="tight">${list(r.strengths)}</ul>` : ""}
            ${r.concerns?.length ? `<h3>Concerns</h3><ul class="tight">${list(r.concerns)}</ul>` : ""}
            ${r.red_flags?.length ? `<h3 style="color:var(--bad)">Red flags</h3><ul class="tight">${list(r.red_flags)}</ul>` : ""}
            ${r.communication ? `<h3>Communication</h3><p>${esc(r.communication)}</p>` : ""}
          </div>`
        : iv.turns.length
        ? `<div class="panel"><div class="empty">Not graded yet — use the button above.</div></div>`
        : ""
    }

    ${
      iv.events.length
        ? `<div class="panel">
            <h3>Proctoring</h3>
            ${iv.integrity_note ? `<p class="hint">${esc(iv.integrity_note)}</p>` : ""}
            <div class="scroll"><table>
              <thead><tr><th>At</th><th>Event</th><th class="num">Duration</th><th>Counted</th><th>Detail</th></tr></thead>
              <tbody>${iv.events
                .map((e) => `<tr><td class="mono">${stamp(e.at_seconds)}</td>
                  <td>${esc(e.kind.replace(/_/g, " "))}</td>
                  <td class="num">${e.duration_seconds.toFixed(1)}s</td>
                  <td>${e.counted ? `<span class="pill bad">counted</span>` : `<span class="pill mute">below threshold</span>`}</td>
                  <td class="hint">${esc(e.detail || "")}</td></tr>`)
                .join("")}</tbody></table></div>
            <div class="hint" style="margin-top:10px">A flag is not proof of misconduct — a dropped
              camera and a second monitor look identical from here. Review before it affects a decision.</div>
          </div>`
        : ""
    }

    ${
      iv.turns.length
        ? `<div class="panel"><h3>Transcript</h3>
            ${iv.turns
              .map((t) => `<div class="turn ${esc(t.speaker)}">
                <span class="ts">${stamp(t.at_seconds)}</span>
                <span class="who">${t.speaker === "interviewer" ? "Interviewer" : "Candidate"}</span>
                <span class="txt">${esc(t.text)}</span></div>`)
              .join("")}
          </div>`
        : `<div class="panel"><div class="empty">No transcript — the call has not happened yet.</div></div>`
    }

    ${
      questions.length
        ? `<div class="panel"><h3>Question plan</h3>
            ${plan.opening ? `<p class="hint"><b>Opening:</b> ${esc(plan.opening)}</p>` : ""}
            <ol class="tight">${questions
              .map((q) => `<li><b>${esc(q.question)}</b>
                <div class="hint">${esc(q.topic)} — ${esc(q.why || "")}</div></li>`)
              .join("")}</ol>
            ${plan.closing ? `<p class="hint" style="margin-top:8px"><b>Closing:</b> ${esc(plan.closing)}</p>` : ""}
          </div>`
        : ""
    }`;

  on("copy", async () => {
    try { await navigator.clipboard.writeText(iv.join_url); toast("Link copied.", "ok"); }
    catch { toast("Could not copy — select the link and copy manually.", "bad"); }
  });

  on("grade", (btn) =>
    busy(btn, "Grading…", async () => {
      try { await api("POST", `/interviews/${iid}/grade`); toast("Graded.", "ok"); render(); }
      catch (e) { toast(e.message, "bad"); }
    })
  );
});

/* ------------------------------------------------------------------ go */

render();

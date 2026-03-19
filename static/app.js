const state = {
  selectedJobId: null,
};

const summaryCards = document.getElementById("summaryCards");
const jobsBody = document.getElementById("jobsBody");
const jobDetail = document.getElementById("jobDetail");
const crawlForm = document.getElementById("crawlForm");
const crawlFeedback = document.getElementById("crawlFeedback");
const searchForm = document.getElementById("searchForm");
const results = document.getElementById("results");
const searchMeta = document.getElementById("searchMeta");

function fmtTime(value) {
  if (!value) return "n/a";
  return new Date(value * 1000).toLocaleString();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function statusClass(status) {
  if (status === "running") return "running";
  if (status === "completed") return "completed";
  return "resumable";
}

function renderSummary(summary) {
  const jobsByStatus = summary.jobs_by_status || {};
  const cards = [
    ["Total Jobs", summary.jobs_total ?? 0],
    ["Indexed Pages", summary.pages_total ?? 0],
    ["Frontier Rows", summary.frontier_total ?? 0],
    ["Running Jobs", jobsByStatus.running ?? 0],
  ];

  summaryCards.innerHTML = cards
    .map(
      ([label, value]) => `
        <article class="metric-card">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </article>
      `
    )
    .join("");
}

function renderJobs(jobs) {
  if (jobs.length === 0) {
    state.selectedJobId = null;
  } else if (!jobs.some((job) => job.job_id === state.selectedJobId)) {
    state.selectedJobId = jobs[0].job_id;
  }

  jobsBody.innerHTML = jobs
    .map((job) => {
      const counts = job.counts || {};
      const runtime = job.runtime || {};
      const selected = state.selectedJobId === job.job_id ? "selected" : "";
      const queueText = `${runtime.in_memory_queue_depth ?? 0}/${runtime.in_memory_queue_limit ?? job.queue_limit}`;
      const action =
        job.status === "resumable"
          ? `<button class="secondary" data-resume="${escapeHtml(job.job_id)}">Resume</button>`
          : `<span class="meta">live</span>`;

      return `
        <tr class="job-row ${selected}" data-job="${escapeHtml(job.job_id)}">
          <td>${escapeHtml(job.job_id)}</td>
          <td><span class="badge ${statusClass(job.status)}">${escapeHtml(job.status)}</span></td>
          <td class="job-cell-url">${escapeHtml(job.origin_url)}</td>
          <td>${escapeHtml(job.max_depth)}</td>
          <td>${escapeHtml(counts.indexed_pages ?? 0)}</td>
          <td>${escapeHtml(queueText)}</td>
          <td>${action}</td>
        </tr>
      `;
    })
    .join("");

  for (const row of jobsBody.querySelectorAll("[data-job]")) {
    row.addEventListener("click", () => {
      state.selectedJobId = row.dataset.job;
      const selected = jobs.find((job) => job.job_id === state.selectedJobId);
      renderJobs(jobs);
      renderJobDetail(selected || null);
    });
  }

  for (const button of jobsBody.querySelectorAll("[data-resume]")) {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const jobId = button.dataset.resume;
      await fetch(`/api/jobs/${encodeURIComponent(jobId)}/resume`, { method: "POST" });
      state.selectedJobId = jobId;
      refresh();
    });
  }

  const selected = jobs.find((job) => job.job_id === state.selectedJobId);
  renderJobDetail(selected || null);
}

function renderJobDetail(job) {
  if (!job) {
    jobDetail.className = "job-detail empty";
    jobDetail.textContent = "Select a job to inspect its current state.";
    return;
  }

  const counts = job.counts || {};
  const runtime = job.runtime || {};
  const events = job.events || [];

  jobDetail.className = "job-detail";
  jobDetail.innerHTML = `
    <div class="detail-summary">
      <div class="detail-heading">
        <div>
          <h3>${escapeHtml(job.job_id)}</h3>
          <div class="detail-url">${escapeHtml(job.origin_url)}</div>
        </div>
        <span class="badge ${statusClass(job.status)}">${escapeHtml(job.status)}</span>
      </div>
      <div class="detail-time">
        Created ${escapeHtml(fmtTime(job.created_at))} | Updated ${escapeHtml(fmtTime(job.updated_at))}
        | Completed ${escapeHtml(fmtTime(job.completed_at))}
      </div>
    </div>

    <div class="detail-grid">
      <article class="detail-card">
        <span>Status</span>
        <strong>${escapeHtml(job.status)}</strong>
        <div>Depth limit: ${escapeHtml(job.max_depth)}</div>
      </article>
      <article class="detail-card">
        <span>Queue Pressure</span>
        <strong>${escapeHtml(runtime.in_memory_queue_depth ?? 0)}/${escapeHtml(runtime.in_memory_queue_limit ?? job.queue_limit)}</strong>
        <div>Back pressure events: ${escapeHtml(runtime.backpressure_events ?? 0)}</div>
      </article>
      <article class="detail-card">
        <span>Indexed Pages</span>
        <strong>${escapeHtml(counts.indexed_pages ?? 0)}</strong>
        <div>Done: ${escapeHtml(counts.done ?? 0)} | Error: ${escapeHtml(counts.error ?? 0)}</div>
      </article>
      <article class="detail-card">
        <span>Workers</span>
        <strong>${escapeHtml(runtime.active_workers ?? 0)}/${escapeHtml(job.worker_count)}</strong>
        <div>Last rate wait: ${escapeHtml(runtime.last_rate_wait_ms ?? 0)} ms</div>
      </article>
    </div>

    <ol class="job-events">
      ${events
        .map(
          (event) => `
            <li>
              <strong>${escapeHtml(fmtTime(event.created_at))}</strong>
              <div>${escapeHtml(event.message)}</div>
            </li>
          `
        )
        .join("")}
    </ol>
  `;
}

async function refresh() {
  const [statusResponse, jobsResponse] = await Promise.all([fetch("/api/status"), fetch("/api/jobs")]);
  const status = await statusResponse.json();
  const jobsPayload = await jobsResponse.json();
  renderSummary(status);
  renderJobs(jobsPayload.jobs || []);
}

crawlForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(crawlForm);
  const payload = {
    origin: form.get("origin"),
    max_depth: Number(form.get("max_depth")),
    worker_count: Number(form.get("worker_count")),
    rate_limit: Number(form.get("rate_limit")),
    queue_limit: Number(form.get("queue_limit")),
  };

  const response = await fetch("/api/index", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    crawlFeedback.textContent = result.error || "Request failed";
    return;
  }

  crawlFeedback.textContent = `Started ${result.job_id}`;
  state.selectedJobId = result.job_id;
  refresh();
});

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(searchForm);
  const query = form.get("query");
  const limit = form.get("limit");
  const response = await fetch(`/api/search?q=${encodeURIComponent(query)}&limit=${encodeURIComponent(limit)}`);
  const payload = await response.json();
  const terms = payload.terms || [];
  searchMeta.textContent = `Matched ${payload.result_count ?? 0} result(s) for terms: ${terms.join(", ") || "none"}`;
  results.innerHTML = (payload.results || [])
    .map(
      (row) => `
        <article class="result">
          <a href="${escapeHtml(row.relevant_url)}" target="_blank" rel="noreferrer">${escapeHtml(row.relevant_url)}</a>
          <div class="result-meta">
            origin=${escapeHtml(row.origin_url)} | depth=${escapeHtml(row.depth)} | score=${escapeHtml(row.score)} | matched_terms=${escapeHtml(row.matched_terms)}
          </div>
          <div class="result-meta">${escapeHtml(row.title || "(no title)")}</div>
        </article>
      `
    )
    .join("");
});

refresh();
setInterval(refresh, 2000);

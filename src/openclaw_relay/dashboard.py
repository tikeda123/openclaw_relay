from __future__ import annotations


def render_dashboard_html() -> str:
    return """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenClaw Relay Chat Monitor</title>
  <style>
    :root {
      --bg: #f3efe8;
      --paper: rgba(255, 255, 255, 0.92);
      --paper-strong: #ffffff;
      --ink: #17212b;
      --muted: #6d7781;
      --line: rgba(23, 33, 43, 0.08);
      --brand: #0f5f8c;
      --a: #dcecf7;
      --a-line: #8cb6d3;
      --b: #e7f1e3;
      --b-line: #96b78e;
      --relay: #f8e2dc;
      --relay-line: #c56a55;
      --shadow: 0 24px 60px rgba(31, 41, 55, 0.10);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15, 95, 140, 0.10), transparent 26%),
        radial-gradient(circle at top right, rgba(150, 183, 142, 0.12), transparent 24%),
        linear-gradient(180deg, #f8f6f2 0%, var(--bg) 100%);
      font-family: "Aptos", "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif;
    }

    .shell {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }

    .hero,
    .panel {
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 28px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }

    .hero {
      padding: 28px 30px;
      margin-bottom: 18px;
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--brand);
      background: rgba(15, 95, 140, 0.08);
    }

    h1 {
      margin: 18px 0 12px;
      font-size: clamp(34px, 4.8vw, 56px);
      line-height: 1.02;
      letter-spacing: -0.04em;
    }

    .hero p {
      margin: 0;
      max-width: 58ch;
      color: var(--muted);
      font-size: 17px;
      line-height: 1.7;
    }

    .strip {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }

    .stack {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 18px;
    }

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.74);
      border: 1px solid var(--line);
      font-size: 13px;
      color: var(--muted);
    }

    .chip strong {
      color: var(--ink);
      font-weight: 700;
    }

    .chip.alert-critical {
      background: rgba(197, 106, 85, 0.12);
      border-color: rgba(197, 106, 85, 0.28);
      color: #7f2e20;
    }

    .chip.alert-warning {
      background: rgba(231, 180, 68, 0.14);
      border-color: rgba(196, 145, 41, 0.28);
      color: #7a5515;
    }

    .nav {
      display: flex;
      gap: 10px;
      margin-top: 18px;
      flex-wrap: wrap;
    }

    .nav-link {
      display: inline-flex;
      align-items: center;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      text-decoration: none;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.74);
      font-size: 13px;
    }

    .nav-link.active {
      background: rgba(15, 95, 140, 0.12);
      border-color: rgba(15, 95, 140, 0.22);
      color: var(--ink);
      font-weight: 700;
    }

    .filter-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 18px;
    }

    .filter-btn {
      appearance: none;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.72);
      color: var(--muted);
      border-radius: 999px;
      padding: 10px 14px;
      font: inherit;
      font-size: 13px;
      cursor: pointer;
      transition: background 120ms ease, color 120ms ease, border-color 120ms ease;
    }

    .filter-btn.active {
      background: rgba(15, 95, 140, 0.12);
      border-color: rgba(15, 95, 140, 0.22);
      color: var(--ink);
    }

    .panel {
      padding: 22px;
    }

    .panel h2 {
      margin: 0 0 8px;
      font-size: 28px;
      letter-spacing: -0.03em;
    }

    .panel-copy {
      margin: 0 0 18px;
      color: var(--muted);
      line-height: 1.6;
    }

    .timeline {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }

    .entry {
      display: flex;
    }

    .entry.from-a { justify-content: flex-start; }
    .entry.from-b { justify-content: flex-end; }
    .entry.relay { justify-content: center; }

    .bubble {
      width: min(100%, 880px);
      max-width: 88%;
      padding: 14px 16px;
      border-radius: 22px;
      border: 1px solid transparent;
    }

    .entry.from-a .bubble {
      background: var(--a);
      border-color: var(--a-line);
      border-bottom-left-radius: 8px;
    }

    .entry.from-b .bubble {
      background: var(--b);
      border-color: var(--b-line);
      border-bottom-right-radius: 8px;
    }

    .entry.relay .bubble {
      background: var(--relay);
      border-color: var(--relay-line);
      border-radius: 16px;
      max-width: 100%;
      color: #7f2e20;
    }

    .meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      font-size: 12px;
      color: var(--muted);
    }

    .meta-left {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    .sender {
      color: var(--ink);
      font-weight: 700;
    }

    .task-tag,
    .status-tag {
      display: inline-flex;
      align-items: center;
      padding: 3px 8px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.70);
      border: 1px solid var(--line);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .body {
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 15px;
      line-height: 1.66;
    }

    .loading {
      color: var(--muted);
      padding: 18px 4px;
    }

    @media (max-width: 720px) {
      .shell { padding: 14px; }
      .hero, .panel { border-radius: 20px; }
      .panel { padding: 18px; }
      .bubble { max-width: 100%; }
      .meta { flex-direction: column; gap: 6px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">OpenClaw Relay Chat Monitor</div>
      <h1>OptionABC001 と Worker 群の会話</h1>
      <p>この画面は Relay transport の request と reply だけを表示します。Telegram の人間会話や session 補助ログはここには出しません。</p>
      <div class="nav">
        <a class="nav-link active" href="/ui">Conversation</a>
        <a class="nav-link" href="/ops">Operations</a>
      </div>
      <div class="stack">
        <div class="strip" id="top-strip">
          <div class="chip">Loading summary...</div>
        </div>
        <div class="strip" id="alert-strip"></div>
        <div class="strip" id="worker-strip"></div>
      </div>
    </section>

    <section class="panel">
      <h2>Chat Timeline</h2>
      <p class="panel-copy">OptionABC001 から worker へ送った message と、worker から Relay 経由で返った reply だけを最新順で表示します。</p>
      <div class="filter-row" id="worker-filters"></div>
      <div class="timeline" id="timeline">
        <div class="loading">Loading conversations...</div>
      </div>
    </section>
  </div>

  <script>
    const fmtDate = (value) => {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString("ja-JP", { hour12: false });
    };

    const escapeHtml = (value) => String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");

    const fmtSeconds = (value) => {
      const number = Number(value);
      if (!Number.isFinite(number)) return "-";
      return `${number.toFixed(1)}s`;
    };

    const sortKey = (value) => {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return 0;
      return date.getTime();
    };

    let selectedWorker = "__all__";
    let showDeadletters = false;

    const RECENT_DEADLETTER_WINDOW_MS = 6 * 60 * 60 * 1000;

    const isDeadletter = (entry) => String(entry?.status || "").startsWith("DEADLETTER");

    function isArchivedDeadletter(entry, latestTimestamp) {
      if (!isDeadletter(entry)) return false;
      const latest = sortKey(latestTimestamp);
      const current = sortKey(entry?.at);
      if (!latest || !current) return true;
      return (latest - current) > RECENT_DEADLETTER_WINDOW_MS;
    }

    function renderTopStrip(data) {
      const metrics = data.metrics;
      const alertSummary = data.alerts?.summary || {};
      const inFlight =
        (metrics.messageStatusCounts.RESERVED || 0) +
        (metrics.messageStatusCounts.QUEUED_B || 0) +
        (metrics.messageStatusCounts.B_REPLIED || 0) +
        (metrics.messageStatusCounts.FAILED_B || 0) +
        (metrics.messageStatusCounts.FAILED_A_INJECTION || 0);
      const deadletter =
        (metrics.messageStatusCounts.DEADLETTER_A || 0) +
        (metrics.messageStatusCounts.DEADLETTER_B || 0);
      document.getElementById("top-strip").innerHTML = `
        <div class="chip"><strong>${metrics.ready ? "READY" : "NOT READY"}</strong> / node ${escapeHtml(data.nodeId)}</div>
        <div class="chip">latest poll <strong>${escapeHtml(fmtDate(data.lastPollCompletedAt))}</strong></div>
        <div class="chip">done <strong>${metrics.messageStatusCounts.DONE || 0}</strong></div>
        <div class="chip">in-flight <strong>${inFlight}</strong></div>
        <div class="chip">deadletter <strong>${deadletter}</strong></div>
        <div class="chip">watch pending <strong>${metrics.watchPendingFiles}</strong></div>
        <div class="chip">active alerts <strong>${alertSummary.total || 0}</strong></div>
      `;
    }

    function renderAlertStrip(data) {
      const container = document.getElementById("alert-strip");
      const snapshot = data.alerts || {};
      const activeAlerts = Array.isArray(snapshot.active) ? snapshot.active : [];
      if (!activeAlerts.length) {
        container.innerHTML = '<div class="chip"><strong>No active alerts</strong></div>';
        return;
      }
      container.innerHTML = activeAlerts.slice(0, 8).map((alert) => {
        const severity = String(alert.severity || "unknown");
        const worker = alert.worker ? ` / ${escapeHtml(alert.worker)}` : "";
        const summary = alert.summary || alert.alertname || "alert";
        const className = severity === "critical" ? "alert-critical" : "alert-warning";
        return `
          <div class="chip ${className}">
            <strong>${escapeHtml(severity.toUpperCase())}</strong>
            ${escapeHtml(summary)}${worker}
          </div>
        `;
      }).join("");
    }

    function renderWorkerStrip(data) {
      const container = document.getElementById("worker-strip");
      const workers = Array.isArray(data.workers) ? data.workers : [];
      if (!workers.length) {
        container.innerHTML = "";
        return;
      }
      container.innerHTML = workers.map((worker) => {
        const statusCounts = worker.messageStatusCounts || {};
        const retrySummary = worker.retrySummary || {};
        const deadletterCounts = worker.deadletterCounts || {};
        const latency = worker.latency || {};
        const dispatchLatency = latency.dispatch || {};
        const injectLatency = latency.inject || {};
        const done = statusCounts.DONE || 0;
        const inFlight =
          (statusCounts.RESERVED || 0) +
          (statusCounts.QUEUED_B || 0) +
          (statusCounts.B_REPLIED || 0) +
          (statusCounts.FAILED_B || 0) +
          (statusCounts.FAILED_A_INJECTION || 0);
        const deadletter =
          (statusCounts.DEADLETTER_A || 0) +
          (statusCounts.DEADLETTER_B || 0);
        return `
          <div class="chip">
            <strong>${escapeHtml(worker.displayName)}</strong>
            done ${done}
            in-flight ${inFlight}
            deadletter ${deadletter}
            retry-b ${retrySummary.bFailed || 0}
            retry-a ${retrySummary.aFailed || 0}
            dead-b ${deadletterCounts.B || 0}
            dead-a ${deadletterCounts.A || 0}
            dispatch ${fmtSeconds(dispatchLatency.avgSeconds || 0)}
            inject ${fmtSeconds(injectLatency.avgSeconds || 0)}
            tunnel ${worker.tunnelHealthy ? "ok" : "down"}
            http ${worker.httpHealthy ? "ok" : "down"}
          </div>
        `;
      }).join("");
    }

    function renderWorkerFilters(data) {
      const container = document.getElementById("worker-filters");
      const workers = Array.isArray(data.workers) ? data.workers : [];
      const timelineSource = Array.isArray(data.timeline) && data.timeline.length
        ? data.timeline
        : buildTimeline(data.messages || []);
      const latestTimestamp = timelineSource.reduce((latest, entry) => {
        return sortKey(entry?.at) > sortKey(latest) ? entry?.at : latest;
      }, null);
      const deadletterCount = timelineSource.filter((entry) => isArchivedDeadletter(entry, latestTimestamp)).length;
      const options = [
        { key: "__all__", label: "All workers" },
        ...workers.map((worker) => ({ key: worker.displayName, label: worker.displayName })),
      ];
      if (!options.some((option) => option.key === selectedWorker)) {
        selectedWorker = "__all__";
      }
      const workerButtons = options.map((option) => `
        <button
          type="button"
          class="filter-btn ${option.key === selectedWorker ? "active" : ""}"
          data-worker="${escapeHtml(option.key)}"
        >${escapeHtml(option.label)}</button>
      `).join("");
      const deadletterToggle = `
        <button
          type="button"
          class="filter-btn ${showDeadletters ? "active" : ""}"
          data-deadletters="toggle"
        >${showDeadletters ? "Hide archived deadletters" : `Show archived deadletters (${deadletterCount})`}</button>
      `;
      container.innerHTML = workerButtons + deadletterToggle;
      container.querySelectorAll(".filter-btn").forEach((button) => {
        if (button.dataset.deadletters === "toggle") {
          button.addEventListener("click", () => {
            showDeadletters = !showDeadletters;
            renderWorkerFilters(data);
            renderTimeline(data);
          });
          return;
        }
        button.addEventListener("click", () => {
          selectedWorker = button.dataset.worker || "__all__";
          renderWorkerFilters(data);
          renderTimeline(data);
        });
      });
    }

    function buildTimeline(messages) {
      const timeline = [];
      for (const message of messages) {
        timeline.push({
          kind: "a",
          taskId: message.conversationId || message.taskId,
          messageId: message.messageId || message.turnId,
          status: message.status,
          sender: message.from || message.fromGateway,
          at: message.createdAt || message.updatedAt,
          body: message.requestBody || "(request body unavailable)",
        });

        if (message.replyText) {
          timeline.push({
            kind: "b",
            taskId: message.conversationId || message.taskId,
            messageId: message.messageId || message.turnId,
            status: message.status,
            sender: message.to || message.toGateway,
            at: message.updatedAt,
            body: message.replyText,
          });
        } else if (message.lastError) {
          timeline.push({
            kind: "relay",
            taskId: message.conversationId || message.taskId,
            messageId: message.messageId || message.turnId,
            status: message.status,
            sender: "Relay",
            at: message.updatedAt,
            body: message.lastError,
          });
        } else {
          timeline.push({
            kind: "relay",
            taskId: message.conversationId || message.taskId,
            messageId: message.messageId || message.turnId,
            status: message.status,
            sender: "Relay",
            at: message.updatedAt,
            body: `${message.to || message.toGateway || "worker"} からの返答待ちです。`,
          });
        }
      }
      timeline.sort((left, right) => sortKey(left.at) - sortKey(right.at));
      return timeline;
    }

    function renderTimeline(data) {
      const container = document.getElementById("timeline");
      const timelineSource = Array.isArray(data.timeline) && data.timeline.length
        ? data.timeline
        : buildTimeline(data.messages || []);
      const workerFiltered = selectedWorker === "__all__"
        ? timelineSource
        : timelineSource.filter((entry) => (entry.worker || entry.sender) === selectedWorker);
      const latestTimestamp = workerFiltered.reduce((latest, entry) => {
        return sortKey(entry?.at) > sortKey(latest) ? entry?.at : latest;
      }, null);
      const filteredSource = showDeadletters
        ? workerFiltered
        : workerFiltered.filter((entry) => !isArchivedDeadletter(entry, latestTimestamp));
      const timeline = [...filteredSource].sort((left, right) => sortKey(right.at) - sortKey(left.at));
      if (!timeline.length) {
        container.innerHTML = showDeadletters
          ? '<div class="loading">会話データがまだありません。</div>'
          : '<div class="loading">表示対象の会話はありません。必要なら "Show archived deadletters" を押してください。</div>';
        return;
      }
      container.innerHTML = timeline.map((entry) => `
        <article class="entry ${entry.kind === "a" ? "from-a" : entry.kind === "b" ? "from-b" : "relay"}">
          <div class="bubble">
            <div class="meta">
              <div class="meta-left">
                <span class="sender">${escapeHtml(entry.sender)}</span>
                ${entry.worker && entry.worker !== entry.sender ? `<span class="task-tag">${escapeHtml(entry.worker)}</span>` : ""}
                <span class="task-tag">${escapeHtml(entry.taskId)}</span>
                ${entry.messageId ? `<span class="task-tag">msg:${escapeHtml(entry.messageId)}</span>` : ""}
                <span class="status-tag">${escapeHtml(entry.status)}</span>
              </div>
              <div>${escapeHtml(fmtDate(entry.at))}</div>
            </div>
            <div class="body">${escapeHtml(entry.body)}</div>
          </div>
        </article>
      `).join("");
    }

    async function loadDashboard() {
      const response = await fetch("/api/dashboard", { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`dashboard fetch failed: ${response.status}`);
      }
      const data = await response.json();
      renderTopStrip(data);
      renderAlertStrip(data);
      renderWorkerStrip(data);
      renderWorkerFilters(data);
      renderTimeline(data);
    }

    async function tick() {
      try {
        await loadDashboard();
      } catch (error) {
        console.error(error);
      }
    }

    tick();
    window.setInterval(tick, 5000);
  </script>
</body>
</html>
"""


def render_ops_html() -> str:
    return """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenClaw Relay Operations Monitor</title>
  <style>
    :root {
      --bg: #f4f1eb;
      --panel: rgba(255, 255, 255, 0.94);
      --line: rgba(21, 31, 38, 0.10);
      --ink: #182129;
      --muted: #6a737d;
      --brand: #0f5f8c;
      --good: #2f6b4f;
      --warn: #ad7a12;
      --bad: #9d3f32;
      --shadow: 0 24px 60px rgba(19, 31, 41, 0.10);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15, 95, 140, 0.08), transparent 28%),
        radial-gradient(circle at top right, rgba(47, 107, 79, 0.08), transparent 24%),
        linear-gradient(180deg, #fbf9f5 0%, var(--bg) 100%);
      font-family: "Aptos", "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif;
    }

    .shell {
      max-width: 1240px;
      margin: 0 auto;
      padding: 24px;
    }

    .hero, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 28px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }

    .hero {
      padding: 28px 30px;
      margin-bottom: 18px;
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      padding: 8px 14px;
      border-radius: 999px;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--brand);
      background: rgba(15, 95, 140, 0.08);
    }

    h1 {
      margin: 18px 0 12px;
      font-size: clamp(34px, 4.8vw, 56px);
      line-height: 1.02;
      letter-spacing: -0.04em;
    }

    .hero p, .panel-copy {
      margin: 0;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.7;
    }

    .nav {
      display: flex;
      gap: 10px;
      margin-top: 18px;
      flex-wrap: wrap;
    }

    .nav-link {
      display: inline-flex;
      align-items: center;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      text-decoration: none;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.74);
      font-size: 13px;
    }

    .nav-link.active {
      background: rgba(15, 95, 140, 0.12);
      border-color: rgba(15, 95, 140, 0.22);
      color: var(--ink);
      font-weight: 700;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 18px;
    }

    .panel {
      padding: 22px;
    }

    .panel h2 {
      margin: 0 0 8px;
      font-size: 26px;
      letter-spacing: -0.03em;
    }

    .span-12 { grid-column: span 12; }
    .span-8 { grid-column: span 8; }
    .span-6 { grid-column: span 6; }
    .span-4 { grid-column: span 4; }

    .card-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }

    .metric-card {
      border: 1px solid var(--line);
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.72);
      padding: 16px;
    }

    .metric-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .metric-value {
      margin-top: 10px;
      font-size: 32px;
      font-weight: 700;
      letter-spacing: -0.04em;
    }

    .metric-sub {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
    }

    .worker-list, .alert-list {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 18px;
    }

    .worker-card, .alert-card {
      border: 1px solid var(--line);
      border-radius: 22px;
      background: rgba(255, 255, 255, 0.76);
      padding: 16px;
    }

    .worker-head, .alert-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      flex-wrap: wrap;
    }

    .worker-name, .alert-name {
      font-size: 19px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .badges {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      font-size: 12px;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.75);
    }

    .badge.good {
      color: var(--good);
      border-color: rgba(47, 107, 79, 0.20);
      background: rgba(47, 107, 79, 0.08);
    }

    .badge.warn {
      color: var(--warn);
      border-color: rgba(173, 122, 18, 0.22);
      background: rgba(173, 122, 18, 0.10);
    }

    .badge.bad {
      color: var(--bad);
      border-color: rgba(157, 63, 50, 0.24);
      background: rgba(157, 63, 50, 0.10);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 14px;
      font-size: 14px;
    }

    th, td {
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }

    th {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 600;
    }

    .mono {
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 12px;
    }

    .empty {
      color: var(--muted);
      margin-top: 16px;
    }

    @media (max-width: 960px) {
      .span-8, .span-6, .span-4 { grid-column: span 12; }
    }

    @media (max-width: 720px) {
      .shell { padding: 14px; }
      .hero, .panel { border-radius: 20px; }
      .panel { padding: 18px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">OpenClaw Relay Operations Monitor</div>
      <h1>Relay の運用状態</h1>
      <p>会話画面とは分離し、運用監視だけを見る画面です。ready、alerts、worker 別 retry/deadletter/latency、直近の deadletter 候補をまとめて見ます。</p>
      <div class="nav">
        <a class="nav-link" href="/ui">Conversation</a>
        <a class="nav-link active" href="/ops">Operations</a>
      </div>
    </section>

    <div class="grid">
      <section class="panel span-12">
        <h2>Overview</h2>
        <p class="panel-copy">Relay 全体の状態と active alert 件数です。</p>
        <div class="card-grid" id="overview-grid">
          <div class="metric-card">Loading...</div>
        </div>
      </section>

      <section class="panel span-8">
        <h2>Workers</h2>
        <p class="panel-copy">worker ごとの health、retry、deadletter、latency を見ます。</p>
        <div class="worker-list" id="worker-list">
          <div class="empty">Loading workers...</div>
        </div>
      </section>

      <section class="panel span-4">
        <h2>Active Alerts</h2>
        <p class="panel-copy">Alertmanager から現在 Relay に入っている active alert です。</p>
        <div class="alert-list" id="alert-list">
          <div class="empty">Loading alerts...</div>
        </div>
      </section>

      <section class="panel span-6">
        <h2>Status Matrix</h2>
        <p class="panel-copy">worker ごとの status 件数です。</p>
        <div id="status-matrix"></div>
      </section>

      <section class="panel span-6">
        <h2>Deadletters</h2>
        <p class="panel-copy">直近の deadletter message を表示します。</p>
        <div id="deadletter-list"></div>
      </section>
    </div>
  </div>

  <script>
    const fmtDate = (value) => {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString("ja-JP", { hour12: false });
    };

    const fmtSeconds = (value) => {
      const number = Number(value);
      if (!Number.isFinite(number)) return "-";
      return `${number.toFixed(1)}s`;
    };

    const escapeHtml = (value) => String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");

    const badgeClass = (healthy) => healthy ? "good" : "bad";

    function renderOverview(data) {
      const metrics = data.metrics || {};
      const alerts = data.alerts?.summary || {};
      const messageStatus = metrics.messageStatusCounts || {};
      const inFlight =
        (messageStatus.RESERVED || 0) +
        (messageStatus.QUEUED_B || 0) +
        (messageStatus.B_REPLIED || 0) +
        (messageStatus.FAILED_B || 0) +
        (messageStatus.FAILED_A_INJECTION || 0);
      const deadletter =
        (messageStatus.DEADLETTER_A || 0) +
        (messageStatus.DEADLETTER_B || 0);
      document.getElementById("overview-grid").innerHTML = `
        <div class="metric-card">
          <div class="metric-label">ready</div>
          <div class="metric-value">${metrics.ready ? "OK" : "DOWN"}</div>
          <div class="metric-sub">last poll ${escapeHtml(fmtDate(data.lastPollCompletedAt))}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">active alerts</div>
          <div class="metric-value">${alerts.total || 0}</div>
          <div class="metric-sub">critical ${alerts.critical || 0} / warning ${alerts.warning || 0}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">done</div>
          <div class="metric-value">${messageStatus.DONE || 0}</div>
          <div class="metric-sub">in-flight ${inFlight}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">deadletter</div>
          <div class="metric-value">${deadletter}</div>
          <div class="metric-sub">watch pending ${metrics.watchPendingFiles || 0}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">mailbox backlog</div>
          <div class="metric-value">${Object.values(metrics.mailboxQueueDepths || {}).reduce((sum, value) => sum + Number(value || 0), 0)}</div>
          <div class="metric-sub">delivered ${metrics.mailboxMessageStatusCounts?.delivered || 0}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">node</div>
          <div class="metric-value">${escapeHtml(data.nodeId || "-")}</div>
          <div class="metric-sub">alerts webhook ${escapeHtml(fmtDate(metrics.lastAlertmanagerWebhookAt || data.alerts?.lastReceivedAt))}</div>
        </div>
      `;
    }

    function renderWorkers(data) {
      const workers = Array.isArray(data.workers) ? data.workers : [];
      const container = document.getElementById("worker-list");
      if (!workers.length) {
        container.innerHTML = '<div class="empty">worker がまだありません。</div>';
        return;
      }
      container.innerHTML = workers.map((worker) => {
        const status = worker.messageStatusCounts || {};
        const retry = worker.retrySummary || {};
        const dead = worker.deadletterCounts || {};
        const latency = worker.latency || {};
        const dispatch = latency.dispatch || {};
        const inject = latency.inject || {};
        const inFlight =
          (status.RESERVED || 0) +
          (status.QUEUED_B || 0) +
          (status.B_REPLIED || 0) +
          (status.FAILED_B || 0) +
          (status.FAILED_A_INJECTION || 0);
        return `
          <article class="worker-card">
            <div class="worker-head">
              <div class="worker-name">${escapeHtml(worker.displayName)}</div>
              <div class="mono">${escapeHtml(worker.name)}</div>
            </div>
            <div class="badges">
              <span class="badge ${badgeClass(worker.tunnelHealthy)}">tunnel ${worker.tunnelHealthy ? "ok" : "down"}</span>
              <span class="badge ${badgeClass(worker.httpHealthy)}">http ${worker.httpHealthy ? "ok" : "down"}</span>
              <span class="badge">done ${status.DONE || 0}</span>
              <span class="badge">in-flight ${inFlight}</span>
              <span class="badge ${dead.B ? "bad" : ""}">dead-b ${dead.B || 0}</span>
              <span class="badge ${dead.A ? "bad" : ""}">dead-a ${dead.A || 0}</span>
              <span class="badge ${retry.bFailed ? "warn" : ""}">retry-b ${retry.bFailed || 0}</span>
              <span class="badge ${retry.aFailed ? "warn" : ""}">retry-a ${retry.aFailed || 0}</span>
              <span class="badge">dispatch avg ${fmtSeconds(dispatch.avgSeconds || 0)}</span>
              <span class="badge">dispatch latest ${fmtSeconds(dispatch.latestSeconds || 0)}</span>
              <span class="badge">inject avg ${fmtSeconds(inject.avgSeconds || 0)}</span>
              <span class="badge">inject latest ${fmtSeconds(inject.latestSeconds || 0)}</span>
            </div>
          </article>
        `;
      }).join("");
    }

    function renderAlerts(data) {
      const alerts = Array.isArray(data.alerts?.active) ? data.alerts.active : [];
      const container = document.getElementById("alert-list");
      if (!alerts.length) {
        container.innerHTML = '<div class="empty">active alert はありません。</div>';
        return;
      }
      container.innerHTML = alerts.map((alert) => `
        <article class="alert-card">
          <div class="alert-head">
            <div class="alert-name">${escapeHtml(alert.alertname)}</div>
            <div class="badge ${alert.severity === "critical" ? "bad" : "warn"}">${escapeHtml(alert.severity || "unknown")}</div>
          </div>
          <div class="badges">
            ${alert.worker ? `<span class="badge">worker ${escapeHtml(alert.worker)}</span>` : ""}
            ${alert.endpoint ? `<span class="badge">endpoint ${escapeHtml(alert.endpoint)}</span>` : ""}
            ${alert.target ? `<span class="badge">target ${escapeHtml(alert.target)}</span>` : ""}
            <span class="badge">since ${escapeHtml(fmtDate(alert.startsAt))}</span>
          </div>
          <p class="panel-copy" style="margin-top: 12px;">${escapeHtml(alert.summary || alert.description || "")}</p>
        </article>
      `).join("");
    }

    function renderStatusMatrix(data) {
      const workers = Array.isArray(data.workers) ? data.workers : [];
      const container = document.getElementById("status-matrix");
      if (!workers.length) {
        container.innerHTML = '<div class="empty">status がまだありません。</div>';
        return;
      }
      container.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Worker</th>
              <th>Done</th>
              <th>Reserved</th>
              <th>Queued B</th>
              <th>B Replied</th>
              <th>Failed B</th>
              <th>Failed A</th>
              <th>Dead B</th>
              <th>Dead A</th>
            </tr>
          </thead>
          <tbody>
            ${workers.map((worker) => {
              const status = worker.messageStatusCounts || {};
              return `
                <tr>
                  <td>${escapeHtml(worker.displayName)}</td>
                  <td>${status.DONE || 0}</td>
                  <td>${status.RESERVED || 0}</td>
                  <td>${status.QUEUED_B || 0}</td>
                  <td>${status.B_REPLIED || 0}</td>
                  <td>${status.FAILED_B || 0}</td>
                  <td>${status.FAILED_A_INJECTION || 0}</td>
                  <td>${status.DEADLETTER_B || 0}</td>
                  <td>${status.DEADLETTER_A || 0}</td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      `;
    }

    function renderDeadletters(data) {
      const messages = Array.isArray(data.messages) ? data.messages : [];
      const deadletters = messages.filter((message) =>
        String(message.status || "").startsWith("DEADLETTER")
      );
      const container = document.getElementById("deadletter-list");
      if (!deadletters.length) {
        container.innerHTML = '<div class="empty">deadletter message はありません。</div>';
        return;
      }
      container.innerHTML = `
        <table>
          <thead>
              <tr>
                <th>Worker</th>
                <th>Conversation</th>
                <th>Status</th>
                <th>Updated</th>
                <th>Error</th>
              </tr>
          </thead>
          <tbody>
            ${deadletters.map((message) => `
              <tr>
                <td>${escapeHtml(message.to || message.toGateway || message.workerDisplayName || "-")}</td>
                <td class="mono">${escapeHtml(message.conversationId || message.taskId || "-")}</td>
                <td>${escapeHtml(message.status || "-")}</td>
                <td>${escapeHtml(fmtDate(message.updatedAt))}</td>
                <td>${escapeHtml(message.lastError || "-")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    }

    async function loadDashboard() {
      const response = await fetch("/api/dashboard", { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`dashboard fetch failed: ${response.status}`);
      }
      const data = await response.json();
      renderOverview(data);
      renderWorkers(data);
      renderAlerts(data);
      renderStatusMatrix(data);
      renderDeadletters(data);
    }

    async function tick() {
      try {
        await loadDashboard();
      } catch (error) {
        console.error(error);
      }
    }

    tick();
    window.setInterval(tick, 5000);
  </script>
</body>
</html>
"""

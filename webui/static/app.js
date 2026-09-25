/* Web 控制台的前端逻辑。
 *
 * 刻意不引入构建步骤与框架：单文件、原生 DOM、ES2019。
 * 状态是服务端说了算 —— 这里只做渲染与投递，任何"看起来对"的判断都要回到
 * /api/state 或 SSE 事件，避免两个界面各存一份真相。
 *
 * 动效只有三处，且都是短促的：数字滚动、进度条补间、表格逐行入场。
 * 系统开了"减少动效"时全部直接落终值（styles.css 里也有一份兜底）。
 */

(function () {
  "use strict";

  var DUR_BASE = 220;
  var STAGGER = 18;
  var STAGGER_MAX = 24;
  var LOG_CAP = 800;

  var reduceMotion = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var els = {};
  var state = null;
  var lastLogLine = 0;
  var stream = null;
  var fieldTimers = {};
  var weightTimer = null;
  var runsLoaded = false;
  var shownConfirmId = null;

  function $(id) { return document.getElementById(id); }

  function cacheElements() {
    ["url", "count", "count-minus", "count-plus", "browser", "use-uc",
     "no-record-text", "btn-detect", "btn-export", "btn-save-default",
     "file-import", "settings-note", "table", "table-body", "table-empty",
     "table-meta", "status", "status-text", "line-count", "log-scroll",
     "log-gutter", "log-lines", "btn-run", "btn-stop", "pct", "rounds",
     "progress", "progress-fill", "ok-count", "fail-count", "conn",
     "tab-run", "tab-history", "view-run", "view-history",
     "btn-hist-refresh", "btn-export-runs", "btn-export-answers",
     "hist-meta", "btn-purge", "purge-note", "purge-ask", "purge-ask-text",
     "btn-purge-yes", "btn-purge-no", "runs-body", "runs-empty", "runs-meta",
     "answers-body", "answers-empty", "ans-meta",
     "confirm-dialog", "confirm-title", "confirm-message",
     "confirm-yes", "confirm-no"
    ].forEach(function (id) { els[id] = $(id); });
  }

  function reduced() { return reduceMotion; }

  // ------------------------------------------------------------ 服务端通信

  function api(path, body) {
    var init = { headers: { "Content-Type": "application/json" } };
    if (body !== undefined) {
      init.method = "POST";
      init.body = JSON.stringify(body);
    }
    return fetch(path, init).then(function (resp) {
      return resp.json().catch(function () {
        return { ok: false, error: "HTTP " + resp.status + " 没有返回 JSON" };
      }).then(function (payload) {
        if (!resp.ok || payload.ok === false) {
          throw new Error(payload.error || ("HTTP " + resp.status));
        }
        return payload;
      });
    });
  }

  function fail(err) {
    showNote(err && err.message ? err.message : String(err));
  }

  function showNote(text) {
    els["settings-note"].textContent = text || "";
  }

  function sendField(name, value) {
    api("/api/field", { name: name, value: value })
      .then(function (r) { render(r.state); })
      .catch(fail);
  }

  function queueField(name, value) {
    var wait = name === "url" ? 400 : 0;
    clearTimeout(fieldTimers[name]);
    if (!wait) { sendField(name, value); return; }
    fieldTimers[name] = setTimeout(function () { sendField(name, value); }, wait);
  }

  // ------------------------------------------------------------ SSE

  function openStream() {
    if (stream) { stream.close(); }
    stream = new EventSource("/api/events");
    stream.addEventListener("hello", function (e) {
      setConn("ok");
      render(JSON.parse(e.data));
    });
    stream.addEventListener("state", function (e) { render(JSON.parse(e.data)); });
    stream.addEventListener("status", function (e) {
      var p = JSON.parse(e.data);
      paintStatus(p.text, p.tone);
    });
    stream.addEventListener("progress", function (e) { paintProgress(JSON.parse(e.data)); });
    stream.addEventListener("log", function (e) { appendLog([JSON.parse(e.data)]); });
    stream.addEventListener("questions", function (e) {
      var p = JSON.parse(e.data);
      renderTable(p.rows || []);
      // 表头计数跟着表一起走：等下一个 state 事件的话，探测完之后卡片上会
      // 挂着"未探测"好几秒 —— 与"点了没反应"同一类读不出来的状态。
      els["table-meta"].textContent = p.count ? p.count + " 题" : "未探测";
    });
    stream.addEventListener("gap", function () {
      // 事件被丢弃过：回到唯一真相源，别猜中间状态
      refresh();
    });
    stream.onerror = function () { setConn("lost"); };
  }

  function setConn(kind) {
    els.conn.dataset.state = kind === "lost" ? "lost" : "ok";
    els.conn.textContent = kind === "lost"
      ? "与服务器的连接中断，正在自动重连……长跑不会因此停止。"
      : "本机服务已连接（只监听 127.0.0.1，关掉本页不会中断运行）。";
  }

  function refresh() {
    api("/api/state").then(function (r) {
      render(r.state);
      return api("/api/log?since=" + lastLogLine);
    }).then(function (r) { appendLog(r.lines, true); })
      .catch(function () { setConn("lost"); });
  }

  // ------------------------------------------------------------ 渲染

  function render(next) {
    state = next;
    if (!state) { return; }
    fillForm(state.form);
    paintStatus(state.status, state.status_tone);
    paintAvailability(state.availability, state.busy, state.running);
    paintCounts(state.counts);
    els["line-count"].textContent = state.log_lines + " lines";
    els["table-meta"].textContent = state.questions
      ? state.questions + " 题" : "未探测";
    if (state.table) { renderTable(state.table); }
    paintConfirm(state.confirms);
  }

  function fillForm(form) {
    setIfDifferent(els.url, form.url);
    setIfDifferent(els.count, String(form.count));
    setIfDifferent(els["use-uc"], form.use_uc);
    setIfDifferent(els["no-record-text"], form.no_record_text);
    if (els.browser.options.length === 0) {
      ["edge", "chrome"].forEach(function (b) {
        var opt = document.createElement("option");
        opt.value = b;
        opt.textContent = b === "edge" ? "Edge" : "Chrome";
        els.browser.appendChild(opt);
      });
    }
    setIfDifferent(els.browser, form.browser);
    els["use-uc"].disabled = form.browser !== "chrome";
  }

  function setIfDifferent(el, value) {
    if (!el) { return; }
    if (el.type === "checkbox") {
      if (el.checked !== !!value) { el.checked = !!value; }
      return;
    }
    if (el.value !== String(value === undefined ? "" : value)) { el.value = value; }
  }

  function paintStatus(text, tone) {
    els["status-text"].textContent = text;
    els.status.dataset.tone = tone || "neutral";
    els["status-text"].classList.toggle("is-shimmer", tone === "running");
  }

  function paintAvailability(avail, busy, running) {
    var detectable = !!avail.selenium;
    els["btn-detect"].disabled = !detectable || running || busy.indexOf("detect") >= 0;
    els["file-import"].disabled = !avail.config_io;
    els["btn-export"].disabled = !avail.config_io;
    els["btn-save-default"].disabled = !avail.config_io;
    els["btn-run"].disabled = running;
    els["btn-stop"].disabled = !running;
    if (!avail.config_io) {
      showNote("未加载 src/config_io：配置导入/导出不可用。");
    } else if (!avail.selenium) {
      showNote("未加载 selenium：探测与运行不可用，请 pip install -r requirements.txt。");
    }
  }

  function paintCounts(c) {
    rollNumber(els["ok-count"], c.success);
    rollNumber(els["fail-count"], c.fail);
    els.rounds.textContent = c.round + " / " + c.total;
    var pct = c.total ? (c.round / c.total * 100) : 0;
    els.pct.textContent = pct.toFixed(1) + "%";
    els["progress-fill"].style.width = Math.min(100, pct) + "%";
    els.progress.setAttribute("aria-valuenow", String(Math.round(pct)));
  }

  function paintProgress(p) { paintCounts({
    success: p.success, fail: p.fail, round: p.round, total: p.total
  }); }

  // 数字滚动（react-bits 的 CountUp 观念）：只补显示值，真值始终在服务端
  function rollNumber(el, target) {
    var from = parseInt(el.textContent, 10);
    if (isNaN(from)) { from = 0; }
    if (reduced() || from === target) { el.textContent = String(target); return; }
    var start = performance.now();
    function tick(now) {
      var t = Math.min(1, (now - start) / DUR_BASE);
      var eased = 1 - Math.pow(1 - t, 3);
      el.textContent = String(Math.round(from + (target - from) * eased));
      if (t < 1) { requestAnimationFrame(tick); }
      else { el.textContent = String(target); }
    }
    requestAnimationFrame(tick);
  }

  function renderTable(rows) {
    var body = els["table-body"];
    body.textContent = "";
    els["table-empty"].hidden = rows.length > 0;
    var staggered = rows.length <= STAGGER_MAX && !reduced();
    rows.forEach(function (row, i) {
      var line = document.createElement("div");
      line.className = "table-row";
      line.setAttribute("role", "row");
      if (staggered) { line.style.animationDelay = (i * STAGGER) + "ms"; }

      line.appendChild(cell("cell-q", "Q" + row.q));
      var kind = pillKind(row.type);
      var pillCell = document.createElement("div");
      var pill = document.createElement("span");
      pill.className = "pill";
      pill.dataset.kind = kind;
      pill.textContent = row.label;
      pillCell.appendChild(pill);
      line.appendChild(pillCell);
      line.appendChild(cell("cell-n", row.n));

      var inputCell = document.createElement("div");
      inputCell.className = "cell-text";
      var input = document.createElement("input");
      input.type = "text";
      input.value = row.text || "";
      input.dataset.q = String(row.q);
      input.setAttribute("aria-label", "第 " + row.q + " 题权重");
      input.addEventListener("change", onWeightEdit);
      inputCell.appendChild(input);
      line.appendChild(inputCell);
      body.appendChild(line);
    });
  }

  function cell(cls, text) {
    var el = document.createElement("div");
    el.className = cls;
    el.textContent = text;
    return el;
  }

  function pillKind(type) {
    var t = String(type || "").toLowerCase();
    if (t.indexOf("matrix_multi") >= 0) { return "matrix_multi"; }
    if (t.indexOf("matrix") >= 0) { return "matrix"; }
    if (t === "multi" || t === "checkbox") { return "multi"; }
    if (t === "text" || t === "textarea" || t === "fillblank" || t === "input") { return "text"; }
    if (t === "sort" || t === "ordering" || t === "rank") { return "sort"; }
    return t;
  }

  function onWeightEdit(ev) {
    var texts = {};
    els["table-body"].querySelectorAll("input[data-q]").forEach(function (input) {
      texts[input.dataset.q] = input.value;
    });
    clearTimeout(weightTimer);
    var touched = ev.target;
    weightTimer = setTimeout(function () {
      api("/api/weights", { texts: texts })
        .then(function (r) { render(r.state); showNote(""); })
        .catch(function (err) { touched.style.borderColor = "#b3261e"; fail(err); });
    }, 150);
  }

  // ------------------------------------------------------------ 日志

  function appendLog(lines, replaceAll) {
    if (!lines || !lines.length) { return; }
    if (replaceAll) {
      els["log-lines"].textContent = "";
      els["log-gutter"].textContent = "";
      lastLogLine = 0;
    }
    var gutter = els["log-gutter"];
    var holder = els["log-lines"];
    lines.forEach(function (row) {
      if (row.n <= lastLogLine) { return; }
      lastLogLine = row.n;
      var num = document.createElement("div");
      num.textContent = pad4(row.n);
      gutter.appendChild(num);

      var line = document.createElement("div");
      line.className = "log-line";
      line.dataset.tag = row.tag;
      var prompt = document.createElement("span");
      prompt.className = "prompt";
      prompt.textContent = "❯";
      var ts = document.createElement("span");
      ts.className = "ts";
      ts.textContent = row.ts;
      var body = document.createElement("span");
      body.className = "body";
      body.textContent = " " + row.text;
      line.appendChild(prompt);
      line.appendChild(ts);
      line.appendChild(body);
      holder.appendChild(line);
    });
    while (holder.children.length > LOG_CAP) {
      holder.removeChild(holder.firstChild);
      gutter.removeChild(gutter.firstChild);
    }
    els["line-count"].textContent = lastLogLine + " lines";
    var scroller = els["log-scroll"];
    var nearBottom = scroller.scrollHeight - scroller.scrollTop
      - scroller.clientHeight < 60;
    if (nearBottom) { scroller.scrollTop = scroller.scrollHeight; }
  }

  function pad4(n) { return ("    " + n).slice(-4); }

  // ---------------------------------------------------------- 确认对话框
  //
  // 内容只来自服务端快照里的 confirms，前端不另存一份"问题"。
  // 因此断线重连、刷新页面、服务端超时撤下，全都由同一个 render 收敛。

  function paintConfirm(confirms) {
    var dlg = els["confirm-dialog"];
    var first = (confirms || [])[0];
    if (!first) {
      shownConfirmId = null;
      if (dlg.open) { dlg.close(); }
      return;
    }
    if (shownConfirmId === first.id) { return; }
    shownConfirmId = first.id;
    els["confirm-title"].textContent = first.title;
    els["confirm-message"].textContent = first.message;
    if (!dlg.open) { dlg.showModal(); }
  }

  function answerConfirm(accept) {
    var id = shownConfirmId;
    if (!id) { return; }
    shownConfirmId = null;              // 连点两下不该发出两次答案
    api("/api/confirm", { id: id, accept: accept })
      .then(function (r) { paintConfirm(r.state.confirms); })
      .catch(function (err) {
        showNote(err && err.message ? err.message : String(err));
        refresh();                      // 答案没送达：回到唯一真相源再判一次
      });
  }

  // ------------------------------------------------------------ 历史记录
  //
  // 这一栏里的文本大部分**来自被抓取的页面**（问卷 URL、报错信息、下拉选项文案），
  // 所以一律走 textContent；任何一处 innerHTML 都等于给自己做一个 XSS 入口。

  function setPurgeNote(text, kind) {
    els["purge-note"].textContent = text || "";
    els["purge-note"].dataset.state = kind || "";
  }

  function switchView(which) {
    var showHistory = which === "history";
    els["view-run"].hidden = showHistory;
    els["view-history"].hidden = !showHistory;
    els["tab-run"].setAttribute("aria-selected", showHistory ? "false" : "true");
    els["tab-history"].setAttribute("aria-selected", showHistory ? "true" : "false");
    if (showHistory && !runsLoaded) { loadRuns(); }
  }

  function loadRuns() {
    runsLoaded = true;
    return api("/api/history/runs").then(function (r) {
      paintRuns(r);
    }).catch(function (err) {
      runsLoaded = false;
      setPurgeNote("读批次失败：" + (err.message || err), "fail");
    });
  }

  function paintRuns(r) {
    renderRuns(r.runs || []);
    var s = r.stats || {};
    els["hist-meta"].textContent = s.total_runs
      ? (s.total_runs + " 批 · 成功率 " + Math.round((s.success_rate || 0) * 100) + "%")
      : "空库";
  }

  function renderRuns(runs) {
    var body = els["runs-body"];
    body.textContent = "";
    els["runs-empty"].hidden = runs.length > 0;
    els["runs-meta"].textContent = runs.length ? runs.length + " 批" : "";
    var staggered = runs.length <= STAGGER_MAX && !reduced();
    runs.forEach(function (run, i) {
      var line = document.createElement("div");
      line.className = "table-row run-row";
      line.setAttribute("role", "row");
      if (staggered) { line.style.animationDelay = (i * STAGGER) + "ms"; }
      line.appendChild(cell("cell-id", String(run.id)));
      line.appendChild(cell("cell-mono", run.started_at || ""));
      var st = document.createElement("div");
      var pill = document.createElement("span");
      pill.className = "st-" + (run.status || "");
      pill.textContent = run.status || "";
      st.appendChild(pill);
      line.appendChild(st);
      line.appendChild(cell("cell-n", String(run.total_submissions)));
      line.appendChild(cell("cell-n",
        (run.success_count || 0) + " / " + (run.fail_count || 0)));
      line.appendChild(cell("cell-url", run.survey_url || ""));
      line.addEventListener("click", function () { selectRun(run.id, line); });
      body.appendChild(line);
    });
  }

  function selectRun(runId, row) {
    els["view-history"].querySelectorAll(".run-row").forEach(function (n) {
      n.setAttribute("aria-selected", n === row ? "true" : "false");
    });
    els["ans-meta"].textContent = "读取中…";
    els["answers-body"].textContent = "";
    api("/api/history/answers?run_id=" + encodeURIComponent(runId))
      .then(function (r) { renderAnswers(r.answers || []); })
      .catch(function (err) {
        els["ans-meta"].textContent = "";
        setPurgeNote("读明细失败：" + (err.message || err), "fail");
      });
  }

  function renderAnswers(answers) {
    els["answers-empty"].hidden = answers.length > 0;
    els["ans-meta"].textContent = answers.length
      ? answers.length + " 条" : "无明细";
    var body = els["answers-body"];
    body.textContent = "";
    var staggered = answers.length <= STAGGER_MAX && !reduced();
    answers.forEach(function (a, i) {
      var line = document.createElement("div");
      line.className = "table-row ans-row";
      line.setAttribute("role", "row");
      if (staggered) { line.style.animationDelay = (i * STAGGER) + "ms"; }
      line.appendChild(cell("cell-n", String(a.submission_index)));
      line.appendChild(cell("cell-id", "Q" + a.question_number));
      line.appendChild(cell("cell-mono", a.question_type || ""));
      line.appendChild(cell("cell-mono", a.options_selected || ""));
      line.appendChild(cell("cell-url", a.text_answer || ""));
      line.appendChild(cell("cell-n", (a.elapsed_ms || 0) + "ms"));
      body.appendChild(line);
    });
  }

  function filenameOf(header) {
    if (!header) { return ""; }
    var star = /filename\*=UTF-8''([^;]+)/i.exec(header);
    if (star) {
      try { return decodeURIComponent(star[1]); } catch (e) { return ""; }
    }
    var plain = /filename="?([^";]+)"?/i.exec(header);
    return plain ? plain[1] : "";
  }

  function downloadCsv(kind) {
    setPurgeNote("正在导出 " + kind + " …", "busy");
    fetch("/api/history/export?kind=" + encodeURIComponent(kind))
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; })
            .then(function (p) { throw new Error(p.error || ("HTTP " + resp.status)); });
        }
        var name = filenameOf(resp.headers.get("Content-Disposition"))
          || ("history_" + kind + ".csv");
        return resp.blob().then(function (b) { return { blob: b, name: name }; });
      })
      .then(function (r) {
        download(r.blob, r.name);
        setPurgeNote("已导出 " + r.name, "");
      })
      .catch(function (err) { setPurgeNote(String(err.message || err), "fail"); });
  }

  // 一次性凭据：预览发一张，确认用掉一张；本地也只在"刚预览过"的那一次有效
  var purgeToken = null;

  function hidePurgeAsk() {
    purgeToken = null;
    els["purge-ask"].hidden = true;
  }

  function askPurge() {
    els["btn-purge"].disabled = true;
    setPurgeNote("正在统计范围…", "busy");
    api("/api/history/purge", {}).then(function (r) {
      if (!r.count) {
        hidePurgeAsk();
        setPurgeNote("没有 " + r.days + " 天前的批次，无需清理。", "");
        return;
      }
      purgeToken = r.token;
      els["purge-ask-text"].textContent =
        "将删除 " + r.count + " 条 " + r.days + " 天前的批次（连带它们的逐题答案），不可恢复。确认？";
      els["purge-ask"].hidden = false;
      setPurgeNote("", "");
    }).catch(function (err) {
      setPurgeNote(String(err.message || err), "fail");
    }).finally(function () {
      els["btn-purge"].disabled = false;
    });
  }

  function confirmPurge() {
    if (!purgeToken) { return; }
    var token = purgeToken;
    purgeToken = null;              // 先清：连点两下不该发出两次确认
    els["btn-purge-yes"].disabled = true;
    api("/api/history/purge", { token: token }).then(function (r) {
      hidePurgeAsk();
      setPurgeNote("已删除 " + r.removed + " 条批次", "");
      render(r.state);
      return loadRuns();
    }).catch(function (err) {
      setPurgeNote(String(err.message || err), "fail");
    }).finally(function () {
      els["btn-purge-yes"].disabled = false;
    });
  }

  // ------------------------------------------------------------ 动作

  function bindActions() {
    els.url.addEventListener("change", function () { queueField("url", els.url.value); });
    els.url.addEventListener("blur", function () { queueField("url", els.url.value); });
    els.count.addEventListener("change", function () { queueField("count", els.count.value); });
    els.browser.addEventListener("change", function () { sendField("browser", els.browser.value); });
    els["use-uc"].addEventListener("change", function () { sendField("use_uc", els["use-uc"].checked); });
    els["no-record-text"].addEventListener("change", function () {
      sendField("no_record_text", els["no-record-text"].checked);
    });

    els["count-minus"].addEventListener("click", function () { bumpCount(-1); });
    els["count-plus"].addEventListener("click", function () { bumpCount(1); });

    els["btn-detect"].addEventListener("click", function () {
      showNote("");
      api("/api/detect", {}).then(function (r) { render(r.state); }).catch(fail);
    });
    els["btn-run"].addEventListener("click", function () {
      showNote("");
      api("/api/run", {}).then(function (r) { render(r.state); }).catch(fail);
    });
    els["btn-stop"].addEventListener("click", function () {
      api("/api/stop", {}).then(function (r) { render(r.state); }).catch(fail);
    });
    els["btn-save-default"].addEventListener("click", function () {
      api("/api/config/save-default", {}).then(function (r) { render(r.state); }).catch(fail);
    });
    els["btn-export"].addEventListener("click", function () {
      api("/api/config/export", { name: "weight_config" })
        .then(function (resp) {
          if (!resp.ok) { return resp.text().then(function (t) { throw new Error(t); }); }
          return resp.blob();
        })
        .then(function (blob) { download(blob, "weight_config.json"); })
        .catch(fail);
    });
    els["file-import"].addEventListener("change", function (ev) {
      var file = ev.target.files && ev.target.files[0];
      if (!file) { return; }
      var reader = new FileReader();
      reader.onload = function () {
        api("/api/config/import", { name: file.name, content: String(reader.result) })
          .then(function (r) { render(r.state); showNote(""); })
          .catch(fail);
      };
      reader.readAsText(file, "utf-8");
      ev.target.value = "";
    });
    document.addEventListener("keydown", function (ev) {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
        // 确认框开着的时候不再叠一次"开始运行"：模态框挡住了鼠标，但挡不住
        // document 上的键盘监听，快捷键照样会发第二个请求、问第二个确认
        if (els["confirm-dialog"].open) { return; }
        els["btn-run"].click();
      }
    });

    els["tab-run"].addEventListener("click", function () { switchView("run"); });
    els["tab-history"].addEventListener("click", function () { switchView("history"); });
    els["btn-hist-refresh"].addEventListener("click", function () {
      setPurgeNote("", "");
      // 走 /api/history/refresh 而不是 /api/state：换视图只想看最新批次，
      // 没必要让浏览器再传一遍整张权重表
      api("/api/history/refresh", {}).then(function (r) {
        paintRuns(r);
        els["answers-body"].textContent = "";
        els["ans-meta"].textContent = "";
        els["answers-empty"].hidden = false;
      }).catch(function (err) {
        setPurgeNote("刷新失败：" + (err.message || err), "fail");
      });
    });
    els["btn-export-runs"].addEventListener("click", function () { downloadCsv("runs"); });
    els["btn-export-answers"].addEventListener("click", function () { downloadCsv("answers"); });
    els["btn-purge"].addEventListener("click", askPurge);
    els["btn-purge-yes"].addEventListener("click", confirmPurge);
    els["btn-purge-no"].addEventListener("click", function () {
      hidePurgeAsk();
      setPurgeNote("已取消，什么都没删。", "");
    });

    els["confirm-yes"].addEventListener("click", function () { answerConfirm(true); });
    els["confirm-no"].addEventListener("click", function () { answerConfirm(false); });
    // Esc / 点遮罩关闭都算"取消"——与桌面版 messagebox 关掉即否同一个语义
    els["confirm-dialog"].addEventListener("cancel", function (ev) {
      ev.preventDefault();
      answerConfirm(false);
    });
  }

  function bumpCount(delta) {
    var next = Math.min(9999, Math.max(1, (parseInt(els.count.value, 10) || 1) + delta));
    els.count.value = String(next);
    sendField("count", next);
  }

  function download(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    cacheElements();
    bindActions();
    setConn("ok");
    refresh();
    openStream();
  });
})();

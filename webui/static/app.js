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

  function $(id) { return document.getElementById(id); }

  function cacheElements() {
    ["url", "count", "count-minus", "count-plus", "browser", "use-uc",
     "no-record-text", "btn-detect", "btn-export", "btn-save-default",
     "file-import", "settings-note", "table", "table-body", "table-empty",
     "table-meta", "status", "status-text", "line-count", "log-scroll",
     "log-gutter", "log-lines", "btn-run", "btn-stop", "pct", "rounds",
     "progress", "progress-fill", "ok-count", "fail-count", "conn"
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
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") { els["btn-run"].click(); }
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

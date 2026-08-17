/* =============================================================================
 * chat_widget.js — AI Chatbot widget (Phase 1 MVP)
 * -----------------------------------------------------------------------------
 * A self-contained, dependency-free chat widget:
 *   • Floating toggle button (bottom-right)
 *   • Slide-in right sidebar with a MULTI-SELECT repository picker (checkboxes),
 *     message bubbles, and citations
 *   • Talks to  POST /api/chat  and  GET /api/chat/scopes
 *
 * Scope model: the user ticks one or more repositories to ask across. Requests
 * are sent as { scope_type:"repos", repo_ids:[...] }. Quick actions cover
 * "Select all", "Clear", and (when on a project page) "Project repos".
 *
 * Drop-in: add  <script src="/static/chat_widget.js" defer></script>  before
 * </body>. It injects its own styles and DOM. No build step.
 * ========================================================================== */
(function () {
  "use strict";
  if (window.__brdChatWidgetLoaded) return;
  window.__brdChatWidgetLoaded = true;

  // ── Context: which project (if any) is this page bound to? ────────────────
  var PROJECT_ID =
    (window.PROJECT_ID) ||
    new URLSearchParams(window.location.search).get("project_id") ||
    null;

  var repoOptions = [];     // [{repo_id, name, attached_to_project}]
  var projectRepoCount = 0;
  var busy = false;
  var pendingRepoId = null; // repo to single-select once the list (re)loads
  var mode = "docs";        // "docs" = ask about documents | "help" = how to use the tool
  // Separate conversation history per mode so switching tabs swaps threads.
  var convos = { docs: [], help: [] }; // entries: {t:'msg',text,who,isError} | {t:'cites',cites}

  // ── Styles ────────────────────────────────────────────────────────────────
  var css = `
  #brd-chat-toggle{position:fixed;right:22px;bottom:22px;z-index:99998;width:56px;height:56px;
    border-radius:50%;border:none;cursor:pointer;background:var(--accent,#0070C0);color:#fff;
    box-shadow:0 6px 18px rgba(0,0,0,.22);display:flex;align-items:center;justify-content:center;
    transition:transform .15s ease, box-shadow .15s ease;font-family:var(--font,'Segoe UI',sans-serif);}
  #brd-chat-toggle:hover{transform:translateY(-2px);box-shadow:0 10px 24px rgba(0,0,0,.28);}
  #brd-chat-toggle svg{width:26px;height:26px;}
  #brd-chat-panel{position:fixed;top:0;right:0;height:100vh;width:390px;max-width:92vw;z-index:99999;
    background:var(--surface,#fff);border-left:1px solid var(--border,#ddd);
    box-shadow:-8px 0 28px rgba(0,0,0,.16);display:flex;flex-direction:column;
    transform:translateX(105%);transition:transform .25s cubic-bezier(.4,0,.2,1);
    font-family:var(--font,'Segoe UI',sans-serif);color:var(--text,#1a1a1a);}
  #brd-chat-panel.open{transform:translateX(0);}
  #brd-chat-panel.full{width:100vw;max-width:100vw;}
  #brd-chat-panel.full #brd-chat-scopebar,
  #brd-chat-panel.full #brd-chat-msgs,
  #brd-chat-panel.full #brd-chat-foot{
    padding-left:max(16px,calc((100vw - 860px)/2));
    padding-right:max(16px,calc((100vw - 860px)/2));}
  #brd-chat-head{padding:14px 16px;background:var(--accent,#0070C0);color:#fff;display:flex;
    align-items:center;gap:10px;flex:0 0 auto;}
  #brd-chat-head .title{font-weight:600;font-size:15px;flex:1;}
  #brd-chat-head button{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.32);
    color:#fff;width:30px;height:30px;border-radius:7px;cursor:pointer;font-size:16px;line-height:1;}
  #brd-chat-scopebar{padding:10px 14px;border-bottom:1px solid var(--border,#ddd);
    background:var(--surface2,#f0f0f0);flex:0 0 auto;}
  .sb-mode{display:flex;margin-bottom:8px;border:1px solid var(--border,#ddd);
    border-radius:var(--radius,8px);overflow:hidden;}
  .sb-mode button{flex:1;padding:6px 8px;font-size:12px;border:none;background:#fff;
    color:var(--text,#1a1a1a);cursor:pointer;}
  .sb-mode button + button{border-left:1px solid var(--border,#ddd);}
  .sb-mode button.active{background:var(--accent,#0070C0);color:#fff;font-weight:600;}
  #brd-help-hint{font-size:12px;color:var(--muted,#777);line-height:1.4;padding:2px 2px 0;}
  #brd-chat-scopebar .sb-top{display:flex;align-items:center;margin-bottom:6px;}
  #brd-chat-scopebar label{font-size:11px;color:var(--muted,#888);text-transform:uppercase;letter-spacing:.04em;}
  #brd-scope-summary{font-size:11px;color:var(--accent,#0070C0);font-weight:600;margin-left:auto;}
  #brd-scope-toggle{background:none;border:none;cursor:pointer;color:var(--accent,#0070C0);font-size:12px;font-weight:600;padding:2px 4px;line-height:1;white-space:nowrap;}
  #brd-docs-scope.collapsed .sb-actions,#brd-docs-scope.collapsed #brd-repo-list,#brd-docs-scope.collapsed .sb-ans{display:none;}
  .sb-actions{display:flex;gap:6px;margin-bottom:6px;}
  .sb-actions button{font-size:11px;padding:3px 8px;border:1px solid var(--border,#ddd);background:#fff;
    border-radius:6px;cursor:pointer;color:var(--text,#1a1a1a);}
  .sb-actions button:hover{background:var(--surface2,#e9e9e9);}
  .sb-list{max-height:150px;overflow-y:auto;border:1px solid var(--border,#ddd);
    border-radius:var(--radius,8px);background:#fff;}
  .sb-item{display:flex;align-items:center;gap:8px;padding:6px 9px;font-size:13px;cursor:pointer;
    border-bottom:1px solid var(--surface2,#f0f0f0);}
  .sb-item:last-child{border-bottom:none;}
  .sb-item:hover{background:var(--surface2,#f5f5f5);}
  .sb-item input{margin:0;flex:0 0 auto;}
  .sb-item .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .sb-item .tag{font-size:10px;color:var(--accent,#0070C0);background:var(--user-bubble,#E6F1FB);
    padding:1px 6px;border-radius:8px;flex:0 0 auto;}
  .sb-empty{padding:10px;font-size:12px;color:var(--muted,#888);text-align:center;}
  .sb-ans{margin-top:8px;}
  .sb-ans label{display:block;font-size:11px;color:var(--muted,#888);text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px;}
  .sb-ans select{width:100%;padding:6px 8px;border:1px solid var(--border,#ddd);border-radius:var(--radius,8px);background:#fff;color:var(--text,#1a1a1a);font-size:12.5px;}
  #brd-chat-msgs{flex:1 1 auto;overflow-y:auto;padding:16px 14px;display:flex;flex-direction:column;gap:12px;}
  .brd-msg{max-width:88%;padding:10px 12px;border-radius:12px;font-size:13.5px;line-height:1.5;
    white-space:pre-wrap;word-wrap:break-word;}
  .brd-msg.user{align-self:flex-end;background:var(--user-bubble,#E6F1FB);border:1px solid var(--border,#ddd);
    border-bottom-right-radius:4px;}
  .brd-msg.bot{align-self:flex-start;background:var(--agent-bubble,#fff);border:1px solid var(--border,#ddd);
    border-bottom-left-radius:4px;}
  .brd-msg.bot.error{border-color:var(--error,#A32D2D);color:var(--error,#A32D2D);}
  .brd-msg.bot{white-space:normal;}
  .brd-msg .bmd-gap{height:7px;}
  .brd-msg .bmd-h{font-weight:600;margin:8px 0 3px;}
  .brd-msg .bmd-h1{font-size:15px;} .brd-msg .bmd-h2{font-size:14px;} .brd-msg .bmd-h3,.brd-msg .bmd-h4{font-size:13px;color:var(--accent,#0070C0);}
  .brd-msg .bmd-ul,.brd-msg .bmd-ol{margin:4px 0;padding-left:20px;}
  .brd-msg .bmd-ul li,.brd-msg .bmd-ol li{margin:2px 0;}
  .brd-msg code{background:var(--surface2,#eee);padding:1px 5px;border-radius:4px;font-size:12px;}
  .brd-msg .bmd-tbl{border-collapse:collapse;width:100%;margin:6px 0;font-size:12px;}
  .brd-msg .bmd-tbl th,.brd-msg .bmd-tbl td{border:1px solid var(--border,#ddd);padding:5px 7px;text-align:left;vertical-align:top;}
  .brd-msg .bmd-tbl th{background:var(--surface2,#f0f0f0);font-weight:600;}
  .brd-cites{align-self:flex-start;max-width:92%;display:flex;flex-direction:column;gap:5px;margin-top:-4px;}
  .brd-cite{font-size:11.5px;color:var(--muted,#666);background:var(--surface2,#f5f5f5);
    border:1px solid var(--border,#e3e3e3);border-radius:8px;padding:6px 8px;line-height:1.35;}
  .brd-cite b{color:var(--accent,#0070C0);}
  .brd-typing{align-self:flex-start;color:var(--muted,#888);font-size:13px;font-style:italic;}
  #brd-chat-foot{flex:0 0 auto;border-top:1px solid var(--border,#ddd);padding:10px;display:flex;gap:8px;
    background:var(--surface,#fff);}
  #brd-chat-input{flex:1;resize:none;height:42px;max-height:120px;padding:10px;border:1px solid var(--border,#ddd);
    border-radius:var(--radius,8px);font-family:inherit;font-size:13.5px;color:var(--text,#1a1a1a);}
  #brd-chat-send{flex:0 0 auto;width:42px;border:none;border-radius:var(--radius,8px);cursor:pointer;
    background:var(--accent,#0070C0);color:#fff;font-size:17px;}
  #brd-chat-send:disabled{opacity:.5;cursor:not-allowed;}
  #brd-chat-empty{color:var(--muted,#888);font-size:13px;text-align:center;margin:auto;padding:24px;}
  `;
  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  // ── DOM ─────────────────────────────────────────────────────────────────
  var toggle = document.createElement("button");
  toggle.id = "brd-chat-toggle";
  toggle.title = "Ask the AI assistant";
  toggle.setAttribute("aria-label", "Open AI chat");
  toggle.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 ' +
    '8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 ' +
    '4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';

  var panel = document.createElement("div");
  panel.id = "brd-chat-panel";
  panel.innerHTML =
    '<div id="brd-chat-head">' +
      '<span class="title">AI Assistant</span>' +
      '<button id="brd-chat-expand" title="Expand to full screen">⤢</button>' +
      '<button id="brd-chat-close" title="Close">&times;</button>' +
    '</div>' +
    '<div id="brd-chat-scopebar">' +
      '<div class="sb-mode">' +
        '<button type="button" data-mode="docs" class="active">📄 My documents</button>' +
        '<button type="button" data-mode="help">📘 How to use the tool</button>' +
      '</div>' +
      '<div id="brd-docs-scope">' +
        '<div class="sb-top"><label>Ask about</label>' +
          '<span id="brd-scope-summary">Loading…</span>' +
          '<button type="button" id="brd-scope-toggle" title="Hide sources">▾ Hide</button></div>' +
        '<div class="sb-actions">' +
          '<button type="button" data-act="all">Select all</button>' +
          '<button type="button" data-act="none">Clear</button>' +
          '<button type="button" data-act="proj" id="brd-act-proj" style="display:none">Project repos</button>' +
        '</div>' +
        '<div id="brd-repo-list" class="sb-list"><div class="sb-empty">Loading sources…</div></div>' +
        '<div class="sb-ans"><label for="brd-ans-mode">Answer mode</label>' +
          '<select id="brd-ans-mode">' +
            '<option value="hybrid">Hybrid — documents first, AI fills gaps (labelled)</option>' +
            '<option value="strict">Strict — uploaded documents only</option>' +
          '</select></div>' +
      '</div>' +
      '<div id="brd-help-hint" style="display:none">Ask how to use this tool, e.g. ' +
        '“how do I create a project?” or “how do I export to Word?” ' +
        'You’ll get step-by-step instructions.</div>' +
    '</div>' +
    '<div id="brd-chat-msgs"><div id="brd-chat-empty">Pick one or more repositories above, ' +
      'then ask a question. Answers are grounded in your selected documents.</div></div>' +
    '<div id="brd-chat-foot">' +
      '<textarea id="brd-chat-input" placeholder="Ask a question…" rows="1"></textarea>' +
      '<button id="brd-chat-send" title="Send">&#10148;</button>' +
    '</div>';

  document.body.appendChild(toggle);
  document.body.appendChild(panel);

  var elMsgs = panel.querySelector("#brd-chat-msgs");
  var elList = panel.querySelector("#brd-repo-list");
  var elSummary = panel.querySelector("#brd-scope-summary");
  var elActProj = panel.querySelector("#brd-act-proj");
  var elScopeToggle = panel.querySelector("#brd-scope-toggle");
  var elDocsScope = panel.querySelector("#brd-docs-scope");
  var elAnsMode = panel.querySelector("#brd-ans-mode");
  var elHelpHint = panel.querySelector("#brd-help-hint");
  var elModeBar = panel.querySelector(".sb-mode");
  var elExpand = panel.querySelector("#brd-chat-expand");
  var elInput = panel.querySelector("#brd-chat-input");
  var elSend = panel.querySelector("#brd-chat-send");
  var elEmpty = panel.querySelector("#brd-chat-empty");

  // ── Helpers ───────────────────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function scrollDown() { elMsgs.scrollTop = elMsgs.scrollHeight; }

  // Minimal, safe Markdown renderer for bot answers. Input is HTML-escaped
  // first, then a small set of Markdown constructs is converted to HTML:
  // tables, headings, bullet/numbered lists, **bold**, `code`.
  function mdInline(s) {
    return s
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }
  function _mdSep(l) {
    var c = l.split("|").map(function (x) { return x.trim(); }).filter(function (x) { return x.length; });
    return c.length > 0 && c.every(function (x) { return /^:?-+:?$/.test(x); });
  }
  function _mdRow(l) {
    var t = l.trim();
    if (t.charAt(0) === "|") t = t.slice(1);
    if (t.charAt(t.length - 1) === "|") t = t.slice(0, -1);
    return t.split("|").map(function (x) { return x.trim(); });
  }
  function renderMd(text) {
    var lines = esc(String(text == null ? "" : text)).replace(/\r\n/g, "\n").split("\n");
    var out = [], i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (line.indexOf("|") > -1 && i + 1 < lines.length && _mdSep(lines[i + 1])) {
        var h = _mdRow(line); i += 2; var rows = [];
        while (i < lines.length && lines[i].indexOf("|") > -1 && lines[i].trim() !== "") { rows.push(_mdRow(lines[i])); i++; }
        var t = '<table class="bmd-tbl"><thead><tr>' + h.map(function (x) { return "<th>" + mdInline(x) + "</th>"; }).join("") + "</tr></thead><tbody>";
        t += rows.map(function (r) { return "<tr>" + r.map(function (c) { return "<td>" + mdInline(c) + "</td>"; }).join("") + "</tr>"; }).join("") + "</tbody></table>";
        out.push(t); continue;
      }
      var hm = line.match(/^(#{1,4})\s+(.*)$/);
      if (hm) { out.push("<div class='bmd-h bmd-h" + hm[1].length + "'>" + mdInline(hm[2]) + "</div>"); i++; continue; }
      if (/^\s*[-*]\s+/.test(line)) {
        var items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { items.push("<li>" + mdInline(lines[i].replace(/^\s*[-*]\s+/, "")) + "</li>"); i++; }
        out.push("<ul class='bmd-ul'>" + items.join("") + "</ul>"); continue;
      }
      if (/^\s*\d+[.)]\s+/.test(line)) {
        var its = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) { its.push("<li>" + mdInline(lines[i].replace(/^\s*\d+[.)]\s+/, "")) + "</li>"); i++; }
        out.push("<ol class='bmd-ol'>" + its.join("") + "</ol>"); continue;
      }
      if (line.trim() === "") { out.push("<div class='bmd-gap'></div>"); i++; continue; }
      out.push("<div>" + mdInline(line) + "</div>"); i++;
    }
    return out.join("");
  }

  function _emptyHint() {
    return mode === "help"
      ? "Ask how to use the tool and you’ll get step-by-step instructions."
      : "Pick one or more repositories above, then ask a question. Answers are grounded in your selected documents.";
  }
  function _appendMsg(text, who, isError) {
    var d = document.createElement("div");
    d.className = "brd-msg " + (who === "user" ? "user" : "bot") + (isError ? " error" : "");
    // Render Markdown for normal bot answers; keep user text and errors plain.
    if (who === "bot" && !isError) d.innerHTML = renderMd(text);
    else d.textContent = text;
    elMsgs.appendChild(d);
    return d;
  }
  function _appendCites(cites) {
    if (!cites || !cites.length) return;
    var wrap = document.createElement("div");
    wrap.className = "brd-cites";
    cites.forEach(function (c) {
      var loc = esc(c.doc_name);
      if (c.heading) loc += " &rsaquo; " + esc(c.heading);
      if (c.page) loc += " (p." + esc(c.page) + ")";
      var el = document.createElement("div");
      el.className = "brd-cite";
      el.innerHTML = "<b>[" + esc(c.ref) + "]</b> " + loc;
      if (c.snippet) el.title = c.snippet;
      wrap.appendChild(el);
    });
    elMsgs.appendChild(wrap);
  }
  // Re-render the message area from the active mode's history (called on switch).
  function renderConvo() {
    elMsgs.innerHTML = "";
    var list = convos[mode] || [];
    if (!list.length) {
      elMsgs.innerHTML = '<div id="brd-chat-empty">' + esc(_emptyHint()) + "</div>";
      return;
    }
    list.forEach(function (e) {
      if (e.t === "cites") _appendCites(e.cites);
      else _appendMsg(e.text, e.who, e.isError);
    });
    scrollDown();
  }
  // toMode lets async replies land in the thread they were asked from, even if
  // the user switched modes while waiting.
  function addMsg(text, who, isError, toMode) {
    toMode = toMode || mode;
    convos[toMode].push({ t: "msg", text: text, who: who, isError: !!isError });
    if (toMode === mode) {
      var emp = elMsgs.querySelector("#brd-chat-empty"); if (emp) emp.remove();
      var d = _appendMsg(text, who, isError); scrollDown(); return d;
    }
  }
  function addCitations(cites, toMode) {
    if (!cites || !cites.length) return;
    toMode = toMode || mode;
    convos[toMode].push({ t: "cites", cites: cites });
    if (toMode === mode) { _appendCites(cites); scrollDown(); }
  }

  var typingEl = null;
  function showTyping() {
    typingEl = document.createElement("div");
    typingEl.className = "brd-typing";
    typingEl.textContent = "Thinking…";
    elMsgs.appendChild(typingEl);
    scrollDown();
  }
  function hideTyping() { if (typingEl) { typingEl.remove(); typingEl = null; } }

  // ── Repository selection (multi-select) ───────────────────────────────────
  function checkboxes() {
    return Array.prototype.slice.call(elList.querySelectorAll("input[type=checkbox]"));
  }
  function selectedIds() {
    return checkboxes().filter(function (b) { return b.checked; })
                       .map(function (b) { return b.value; });
  }
  function updateSummary() {
    var n = selectedIds().length, m = repoOptions.length;
    if (!m) { elSummary.textContent = "No repositories"; return; }
    if (n === 0) elSummary.textContent = "None selected";
    else if (n === m) elSummary.textContent = "All " + m + " selected";
    else if (n === 1) {
      var id = selectedIds()[0];
      var opt = repoOptions.find(function (r) { return r.repo_id === id; });
      elSummary.textContent = opt ? opt.name : "1 selected";
    } else elSummary.textContent = n + " of " + m + " selected";
  }
  function setAll(checked) {
    checkboxes().forEach(function (b) { b.checked = checked; });
    updateSummary();
  }
  function setProjectRepos() {
    checkboxes().forEach(function (b) {
      var opt = repoOptions.find(function (r) { return r.repo_id === b.value; });
      b.checked = !!(opt && opt.attached_to_project);
    });
    updateSummary();
  }
  function setSelectionToRepo(repoId) {
    var found = false;
    checkboxes().forEach(function (b) {
      var match = b.value === repoId;
      b.checked = match;
      if (match) found = true;
    });
    if (found) updateSummary();
    return found;
  }

  function renderRepoList() {
    elList.innerHTML = "";
    if (!repoOptions.length) {
      elList.innerHTML = '<div class="sb-empty">No repositories found</div>';
      elActProj.style.display = "none";
      updateSummary();
      return;
    }
    repoOptions.forEach(function (r) {
      var lab = document.createElement("label");
      lab.className = "sb-item";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = r.repo_id;
      cb.addEventListener("change", updateSummary);
      var nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = r.name;
      lab.appendChild(cb);
      lab.appendChild(nm);
      if (r.attached_to_project) {
        var tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = "project";
        lab.appendChild(tag);
      }
      elList.appendChild(lab);
    });

    elActProj.style.display = projectRepoCount > 0 ? "" : "none";

    // Initial selection: a pending single repo (from selectRepo) wins,
    // otherwise default to all repositories so the chat works immediately.
    if (pendingRepoId && setSelectionToRepo(pendingRepoId)) {
      pendingRepoId = null;
    } else {
      setAll(true);
    }
    updateSummary();
  }

  // ── Load scopes ─────────────────────────────────────────────────────────
  function loadScopes() {
    var url = "/api/chat/scopes" + (PROJECT_ID ? "?project_id=" + encodeURIComponent(PROJECT_ID) : "");
    fetch(url).then(function (r) { return r.json(); }).then(function (d) {
      repoOptions = d.repositories || [];
      projectRepoCount = d.project_repo_count || 0;
      renderRepoList();
    }).catch(function () {
      elList.innerHTML = '<div class="sb-empty">Could not load sources</div>';
      elSummary.textContent = "Error";
    });
  }

  // ── Mode: documents vs. how-to-use-the-tool ───────────────────────────────
  function setMode(m) {
    mode = (m === "help") ? "help" : "docs";
    Array.prototype.forEach.call(elModeBar.querySelectorAll("button"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-mode") === mode);
    });
    var help = mode === "help";
    elDocsScope.style.display = help ? "none" : "";
    elHelpHint.style.display = help ? "" : "none";
    elInput.placeholder = help ? "Ask how to use the tool…" : "Ask a question…";
    renderConvo();   // swap to this mode's own conversation thread
  }

  // ── Send ──────────────────────────────────────────────────────────────────
  function send() {
    if (busy) return;
    var q = (elInput.value || "").trim();
    if (!q) return;

    var payload;
    if (mode === "help") {
      payload = { question: q, scope_type: "help" };
    } else {
      var ids = selectedIds();
      if (!ids.length) {
        addMsg("Please select at least one repository to ask about.", "bot", true);
        return;
      }
      var ansMode = (elAnsMode && elAnsMode.value === "strict") ? "strict" : "hybrid";
      payload = { question: q, scope_type: "repos", repo_ids: ids, answer_mode: ansMode };
    }

    var askMode = mode;   // remember which thread this question belongs to
    addMsg(q, "user", false, askMode);
    elInput.value = "";
    elInput.style.height = "42px";
    busy = true; elSend.disabled = true;
    showTyping();

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (res) {
        hideTyping();
        if (!res.ok) {
          addMsg((res.d && (res.d.detail || res.d.message)) || "Request failed.", "bot", true, askMode);
          return;
        }
        addMsg(res.d.answer || "(no answer)", "bot", false, askMode);
        addCitations(res.d.citations, askMode);
      })
      .catch(function (e) {
        hideTyping();
        addMsg("Network error: " + e.message, "bot", true, askMode);
      })
      .finally(function () { busy = false; elSend.disabled = false; elInput.focus(); });
  }

  // ── Wiring ─────────────────────────────────────────────────────────────────
  function openPanel() { panel.classList.add("open"); elInput.focus(); }
  function closePanel() { panel.classList.remove("open"); }
  function toggleFull() {
    var full = panel.classList.toggle("full");
    elExpand.innerHTML = full ? "⤡" : "⤢";
    elExpand.title = full ? "Exit full screen" : "Expand to full screen";
    elInput.focus();
  }

  toggle.addEventListener("click", function () {
    // The original floating button now opens the full-page assistant.
    window.location.href = "/assistant";
  });
  elExpand.addEventListener("click", toggleFull);
  panel.querySelector("#brd-chat-close").addEventListener("click", closePanel);
  elSend.addEventListener("click", send);
  elInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  elInput.addEventListener("input", function () {
    elInput.style.height = "42px";
    elInput.style.height = Math.min(elInput.scrollHeight, 120) + "px";
  });
  panel.querySelector(".sb-actions").addEventListener("click", function (e) {
    var act = e.target && e.target.getAttribute("data-act");
    if (act === "all") setAll(true);
    else if (act === "none") setAll(false);
    else if (act === "proj") setProjectRepos();
  });
  // Collapse/expand the source picker so it doesn't cover the answer.
  function setScopeCollapsed(v) {
    elDocsScope.classList.toggle("collapsed", v);
    elScopeToggle.innerHTML = v ? "▸ Sources" : "▾ Hide";
    elScopeToggle.title = v ? "Show sources" : "Hide sources";
    try { localStorage.setItem("brd_scope_collapsed", v ? "1" : "0"); } catch (_) {}
  }
  elScopeToggle.addEventListener("click", function () {
    setScopeCollapsed(!elDocsScope.classList.contains("collapsed"));
  });
  try { if (localStorage.getItem("brd_scope_collapsed") === "1") setScopeCollapsed(true); } catch (_) {}
  elModeBar.addEventListener("click", function (e) {
    var m = e.target && e.target.getAttribute("data-mode");
    if (m) setMode(m);
  });

  // ── Public API ──────────────────────────────────────────────────────────
  // Other pages can drive the picker, e.g. when the user clicks a repository:
  //   window.brdChat.selectRepo(repoId, {open:true})  → selects just that repo
  //   window.brdChat.addRepo(repoId)                  → adds to current selection
  window.brdChat = {
    selectRepo: function (repoId, opts) {
      opts = opts || {};
      if (opts.open) openPanel();
      if (!setSelectionToRepo(repoId)) {
        pendingRepoId = repoId;   // not loaded yet (e.g. just created) — refresh
        loadScopes();
      }
    },
    addRepo: function (repoId) {
      var done = false;
      checkboxes().forEach(function (b) { if (b.value === repoId) { b.checked = true; done = true; } });
      if (done) updateSummary();
      return done;
    },
    helpMode: function () { setMode("help"); openPanel(); },
    open: openPanel,
    close: closePanel,
  };

  setMode("docs");
  loadScopes();
})();

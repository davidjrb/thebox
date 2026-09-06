/* hub.js — the little bits of interactivity the hub pages need. No frameworks. */
"use strict";
const $ = (s, r) => (r || document).querySelector(s);
const esc = s => (s || "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const human = n => { const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + " " + u[i]; };

async function postJSON(url, data) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
  let j = null; try { j = await r.json(); } catch (_) {}
  if (!r.ok || !j || !j.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
  return j;
}

/* ---------- seat page: peek at the pane ---------- */
document.addEventListener("click", async e => {
  const a = e.target.closest("#peek, .peek"); if (!a) return;
  e.preventDefault();
  const out = $("#peekout"); out.hidden = false; out.textContent = "capturing " + (a.dataset.tab ? a.dataset.session + " · " + a.dataset.tab : a.dataset.session) + "…";
  try {
    const r = await fetch("/api/capture?lines=60&session=" + encodeURIComponent(a.dataset.session) + (a.dataset.tab ? "&tab=" + encodeURIComponent(a.dataset.tab) : ""));
    const j = await r.json(); out.textContent = j.ok ? j.output : ("error: " + j.error);
  } catch (err) { out.textContent = "error: " + err.message; }
});

/* ---------- board ---------- */
function hubBoard() {
  const msg = $("#msg");
  const say = (t, cls) => { msg.textContent = t; msg.className = "small " + (cls || ""); };
  const reload = () => location.reload();
  const act = async (data) => {
    say("saving…");
    try { const j = await postJSON("/api/board", data); say(j.git && j.git.ok ? "saved · " + j.git.detail : "saved locally; git: " + (j.git && j.git.detail), j.git && j.git.ok ? "ok" : "warn"); setTimeout(reload, 250); }
    catch (err) { say(err.message, "err"); }
  };
  $("#addform").addEventListener("submit", e => {
    e.preventDefault();
    const title = $("#addtitle").value.trim(); if (!title) return;
    act({action: "add", title, note: $("#addnote").value.trim()});
  });
  document.querySelectorAll(".item").forEach(it => {
    const idx = +it.dataset.idx, title = it.dataset.title;
    it.querySelectorAll("[data-act]").forEach(b => b.addEventListener("click", () => {
      const a = b.dataset.act;
      if (a === "toggle") return act({action: "toggle", idx, title});
      if (a === "delete") { if (confirm("Delete “" + title + "” and its notes?")) act({action: "delete", idx, title}); return; }
      if (a === "retitle") { const t = prompt("New title (#tags allowed):", title); if (t && t.trim() && t.trim() !== title) act({action: "retitle", idx, title, new_title: t.trim()}); }
    }));
    const f = it.querySelector(".noterow");
    f.addEventListener("submit", e => { e.preventDefault(); const text = f.querySelector("input").value.trim(); if (text) act({action: "note", idx, title, text}); });
  });
  const filter = $("#filter"), showdone = $("#showdone");
  try { showdone.checked = localStorage.getItem("board.showdone") === "1"; } catch (_) {}
  const apply = () => {
    const q = filter.value.trim().toLowerCase();
    try { localStorage.setItem("board.showdone", showdone.checked ? "1" : "0"); } catch (_) {}
    document.querySelectorAll(".item").forEach(it => {
      const hideDone = it.classList.contains("done") && !showdone.checked && !q;
      it.hidden = hideDone || (q && !it.dataset.text.includes(q));
    });
  };
  filter.addEventListener("input", apply); showdone.addEventListener("change", apply); apply();
  if (location.hash) { const t = $(location.hash); if (t) { t.hidden = false; t.scrollIntoView(); } }
}

/* ---------- send page ---------- */
function hubSend(preselect) {
  const sel = $("#session"), tabSel = $("#tab"), tabRow = $("#tabrow"), hint = $("#hint"), promptEl = $("#prompt"), pendingEl = $("#pending");
  const fileInput = $("#file"), sendBtn = $("#send"), resultEl = $("#result"), prog = $("#prog"), bar = prog.firstElementChild;
  let sessions = [], pending = [], seq = 0, sending = false, firstTabs = true;
  const preTab = new URLSearchParams(location.search).get("tab") || "";
  async function loadSessions() {
    hint.textContent = "refreshing…";
    try { sessions = await (await fetch("/api/sessions", {cache: "no-store"})).json(); }
    catch (e) { sel.innerHTML = '<option value="">⚠ failed</option>'; hint.textContent = e.message; return; }
    if (!sessions.length) { sel.innerHTML = '<option value="">— no herdr workspaces —</option>'; hint.textContent = "start one: seat <name>"; return; }
    sessions.sort((a, b) => a.name.localeCompare(b.name));
    sel.innerHTML = sessions.map(s => '<option value="' + esc(s.name) + '">' + esc(s.name) + (s.seat ? "" : " (no seat record)") + "</option>").join("");
    if (preselect && sessions.some(s => s.name === preselect)) sel.value = preselect;
    updateTabs();
  }
  function updateTabs() {
    const s = sessions.find(x => x.name === sel.value);
    const tabs = (s && s.tabs) || [];
    const named = tabs.filter(t => t.name);
    if (!s || tabs.length <= 1 || !named.length) {
      tabRow.hidden = true; tabSel.innerHTML = "";
    } else {
      tabRow.hidden = false;
      tabSel.innerHTML = '<option value="">— any (group\'s agent) —</option>' +
        tabs.map(t => '<option value="' + esc(t.name) + '">' + esc(t.name) + " · " + esc(t.agent || "no agent") + " · " + esc(t.status) + "</option>").join("");
      if (firstTabs && preTab && tabs.some(t => t.name === preTab)) tabSel.value = preTab;
    }
    firstTabs = false;
    updateHint();
  }
  function updateHint() {
    const s = sessions.find(x => x.name === sel.value); if (!s) { hint.textContent = ""; return; }
    const n = (s.tabs || []).length;
    hint.textContent = s.status + " · " + n + " tab" + (n === 1 ? "" : "s") + " · agent " + (s.agent || "none");
  }
  function addFiles(list) { for (const f of list) { if (!f) continue; const id = ++seq; pending.push({id, file: f, name: f.name || ("file-" + id), size: f.size, type: f.type, url: /^image\//.test(f.type) ? URL.createObjectURL(f) : null}); } render(); }
  function render() {
    pendingEl.innerHTML = pending.map(p => '<div class="chip">' + (p.url ? '<img src="' + p.url + '" alt="">' : '<div class="ic">' + esc((p.name.split(".").pop() || "?").slice(0, 3).toUpperCase()) + "</div>") +
      '<div class="meta"><span class="nm">' + esc(p.name) + '</span><span class="muted small">' + human(p.size) + "</span></div>" +
      '<button type="button" class="ghost small" data-rm="' + p.id + '">×</button></div>').join("");
    pendingEl.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => { const i = pending.findIndex(p => p.id === +b.dataset.rm); if (i >= 0) { if (pending[i].url) URL.revokeObjectURL(pending[i].url); pending.splice(i, 1); } render(); });
  }
  $("#addbtn").onclick = () => fileInput.click();
  fileInput.onchange = () => { addFiles(fileInput.files); fileInput.value = ""; };
  const dz = $("#dropzone");
  window.addEventListener("dragover", e => { if (e.dataTransfer && e.dataTransfer.types.includes("Files")) { e.preventDefault(); dz.classList.add("drag"); } });
  window.addEventListener("dragleave", () => dz.classList.remove("drag"));
  window.addEventListener("drop", e => { if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); addFiles(e.dataTransfer.files); } dz.classList.remove("drag"); });
  window.addEventListener("paste", e => { const items = e.clipboardData && e.clipboardData.items; if (!items) return; let got = false; for (const it of items) if (it.kind === "file") { const f = it.getAsFile(); if (f) { addFiles([f]); got = true; } } if (got) e.preventDefault(); });
  const setResult = (cls, html) => resultEl.innerHTML = '<div class="box ' + cls + '">' + html + "</div>";
  const setProgress = p => { prog.hidden = p < 0; bar.style.width = (p < 0 ? 0 : Math.round(p * 100)) + "%"; };
  function doSend() {
    if (sending) return;
    const session = sel.value;
    if (!session) return setResult("err", "Pick a seat first.");
    if (!promptEl.value.trim() && !pending.length) return setResult("err", "Add a prompt or a file.");
    const fd = new FormData(); fd.append("session", session); fd.append("prompt", promptEl.value);
    fd.append("mode", (document.querySelector('input[name=mode]:checked') || {}).value || "prompt");
    const tab = tabRow.hidden ? "" : tabSel.value;
    if (tab) fd.append("tab", tab);
    for (const p of pending) fd.append("files", p.file, p.name);
    sending = true; sendBtn.disabled = true; sendBtn.textContent = "Sending…"; setProgress(0);
    const xhr = new XMLHttpRequest(); xhr.open("POST", "/api/send");
    xhr.upload.onprogress = e => { if (e.lengthComputable) setProgress(e.loaded / e.total); };
    xhr.onload = () => {
      sending = false; sendBtn.disabled = false; sendBtn.textContent = "Send →"; setProgress(-1);
      let d = null; try { d = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status < 300 && d && d.ok) {
        setResult("ok", "Sent to <b>" + esc(d.session) + "</b>" + (d.tab ? " · tab <b>" + esc(d.tab) + "</b>" : "") + " · " + (d.delivered ? '<span class="ok">nudge delivered</span>' : '<span class="err">nudge NOT delivered</span> ' + esc(d.error || "")) +
          '<div class="small">message: <a href="' + esc(d.url) + '">' + esc(d.url) + "</a> · " + (d.files || []).length + " file(s)</div>");
        promptEl.value = ""; pending.forEach(p => p.url && URL.revokeObjectURL(p.url)); pending = []; render(); promptEl.focus();
      } else setResult("err", "<b>Send failed:</b> " + esc((d && d.error) || ("HTTP " + xhr.status)));
    };
    xhr.onerror = () => { sending = false; sendBtn.disabled = false; sendBtn.textContent = "Send →"; setProgress(-1); setResult("err", "Network error."); };
    xhr.send(fd);
  }
  sendBtn.onclick = doSend; sel.onchange = updateTabs; $("#refresh").onclick = loadSessions;
  promptEl.addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doSend(); } });
  loadSessions();
}

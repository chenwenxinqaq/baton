// Progressive enhancements for Unified Memory & Skills Web Console.

(() => {
  // 1. Invoker Commands fallback for browsers without native commandForElement support.
  if (typeof HTMLButtonElement !== "undefined" && !("commandForElement" in HTMLButtonElement.prototype)) {
    document.addEventListener("click", (event) => {
      const targetEl = event.target instanceof Element ? event.target : null;
      const btn = targetEl ? targetEl.closest("button[commandfor][command]") : null;
      if (!btn) return;
      const dialogId = btn.getAttribute("commandfor");
      const dialog = dialogId ? document.getElementById(dialogId) : null;
      if (!(dialog instanceof HTMLDialogElement)) return;
      const cmd = btn.getAttribute("command");
      if (cmd === "show-modal" && !dialog.open) {
        dialog.showModal();
      } else if (cmd === "close" && dialog.open) {
        dialog.close();
      }
    });
  }

  // 2. Light-dismiss fallback for <dialog closedby="any"> and returnValue reset.
  if (typeof HTMLDialogElement !== "undefined") {
    const dialogs = document.querySelectorAll("dialog");
    for (const dialog of dialogs) {
      dialog.addEventListener("close", () => {
        dialog.returnValue = "";
      });
    }

    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (e, dialog) => {
        const r = dialog.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
        let pressedOutside = false;
        dialog.addEventListener("pointerdown", (e) => {
          pressedOutside = outside(e, dialog);
        });
        dialog.addEventListener("pointerup", (e) => {
          if (pressedOutside && outside(e, dialog)) {
            dialog.close();
          }
        });
      }
    }
  }

  // 3. Live elapsed-time refresh via Temporal.ZonedDateTime + Intl.DurationFormat when natively available.
  if (typeof Temporal !== "undefined") {
    const UNITS = ["years", "months", "weeks", "days", "hours", "minutes"];
    const suffixFor = (mode) => {
      if (mode === "new") return "写入";
      if (mode === "upd") return "更新";
      return "";
    };

    const refreshRelativeTimes = () => {
      const tz = Temporal.Now.timeZoneId();
      const now = Temporal.Now.zonedDateTimeISO(tz);
      for (const el of document.querySelectorAll("time[data-ts]")) {
        const rawTs = el.getAttribute("data-ts") || "";
        if (!rawTs.includes("T")) continue;
        try {
          const eventZdt = Temporal.Instant.from(rawTs).toZonedDateTimeISO(tz);
          const elapsed = now.since(eventZdt, { largestUnit: "year", smallestUnit: "minute" });
          if (elapsed.sign < 0) continue;
          const mode = el.getAttribute("data-mode") || "bare";
          const suffix = suffixFor(mode);
          let leadUnit = null;
          for (const unit of UNITS) {
            if (elapsed[unit] > 0) {
              leadUnit = unit;
              break;
            }
          }
          if (!leadUnit) {
            el.textContent = `刚刚${suffix}`;
            continue;
          }
          const singleUnitDuration = Temporal.Duration.from({ [leadUnit]: elapsed[leadUnit] });
          const humanSpan = singleUnitDuration.toLocaleString("zh-CN", { style: "short" }).replace(/\s+/g, "");
          el.textContent = `${humanSpan}前${suffix}`;
        } catch {
          // Keep server-rendered text if timestamp format cannot be parsed as an Instant.
        }
      }
    };

    refreshRelativeTimes();
    setInterval(() => {
      if (document.visibilityState === "visible") {
        refreshRelativeTimes();
      }
    }, 60_000);
  }

  // 4. Cmd+S / Ctrl+S shortcut inside Markdown editor textareas (IME-safe).
  for (const textarea of document.querySelectorAll(".edit-form textarea")) {
    textarea.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (textarea.form) {
          textarea.form.requestSubmit();
        }
      }
    });
  }

  const syncNotices = document.getElementById("sync-notices");
  const syncCards = new Map();
  let dismissedJobs = [];
  try {
    const storedJobs = JSON.parse(sessionStorage.getItem("dismissed-sync-jobs"));
    if (Array.isArray(storedJobs)) dismissedJobs = storedJobs;
  } catch {
    // Status notifications still work when storage is unavailable.
  }

  function renderSyncNotice(job) {
    if (dismissedJobs.includes(job.id)) return;
    const previous = syncCards.get(job.id);
    const snapshot = JSON.stringify(job);
    if (previous?.snapshot === snapshot) return;

    const card = document.createElement("div");
    card.className = "sync-notice";
    card.dataset.state = job.state;
    const indicator = document.createElement("span");
    indicator.className = "sync-notice-indicator";
    indicator.setAttribute("aria-hidden", "true");
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = job.label ? `${job.title} · ${job.label}` : job.title;
    copy.append(title);
    if (job.message) {
      const message = document.createElement("p");
      message.textContent = job.message;
      copy.append(message);
    }
    if (job.link) {
      const link = document.createElement("a");
      link.href = job.link;
      link.textContent = job.link_text;
      copy.append(link);
    }
    card.append(indicator, copy);
    if (job.state !== "running") {
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "sync-notice-dismiss";
      dismiss.setAttribute("aria-label", `关闭通知：${title.textContent}`);
      dismiss.textContent = "×";
      dismiss.addEventListener("click", () => {
        dismissedJobs = [...dismissedJobs, job.id].slice(-32);
        try { sessionStorage.setItem("dismissed-sync-jobs", JSON.stringify(dismissedJobs)); } catch {}
        card.remove();
        syncCards.delete(job.id);
      });
      card.append(dismiss);
    }
    if (previous) previous.card.replaceWith(card);
    else syncNotices.prepend(card);
    syncCards.set(job.id, { snapshot, card });
  }

  let syncPollTimer;
  let syncPollVersion = 0;
  async function watchSyncJobs() {
    clearTimeout(syncPollTimer);
    const version = ++syncPollVersion;
    if (document.hidden) return;
    let delay = 8000;
    try {
      const response = await fetch("/sync_jobs");
      if (!response.ok) throw new Error("Sync status unavailable");
      const jobs = await response.json();
      if (version !== syncPollVersion) return;
      const current = new Set(jobs.map(job => job.id));
      for (const [id, entry] of syncCards) {
        if (id !== "submit-error" && !current.has(id)) {
          entry.card.remove();
          syncCards.delete(id);
        }
      }
      for (const job of jobs) renderSyncNotice(job);
      if (jobs.some(job => job.state === "running")) delay = 1200;
    } catch {
      if (syncCards.size) delay = 2000;
    }
    if (version === syncPollVersion && !document.hidden) {
      syncPollTimer = setTimeout(watchSyncJobs, delay);
    }
  }

  async function submitBackgroundSync(form, submitter) {
    const body = new URLSearchParams(new FormData(form));
    if (submitter.name) body.append(submitter.name, submitter.value);
    submitter.disabled = true;
    try {
      const response = await fetch(new URL(form.action).pathname, {
        method: "POST", headers: { Accept: "application/json" }, body, keepalive: true,
      });
      if (response.status !== 202) {
        const detail = response.headers.get("Content-Type")?.startsWith("application/json")
          ? (await response.json()).error : "请刷新页面后重试。";
        throw new Error(detail);
      }
      syncCards.get("submit-error")?.card.remove();
      syncCards.delete("submit-error");
    } catch (error) {
      dismissedJobs = dismissedJobs.filter(id => id !== "submit-error");
      renderSyncNotice({ id: "submit-error", state: "error", title: "无法启动同步", message: error.message });
    } finally {
      submitter.disabled = false;
      watchSyncJobs();
    }
  }

  if (syncNotices) {
    watchSyncJobs();
    document.addEventListener("visibilitychange", watchSyncJobs);
    window.addEventListener("pageshow", event => { if (event.persisted) watchSyncJobs(); });
  }

  // Keep sync in the background and prevent double-submission of other forms.
  for (const form of document.querySelectorAll("form[method='post'], form[method='POST']")) {
    form.addEventListener("submit", (event) => {
      if (!form.checkValidity()) return;
      const submitter =
        event.submitter instanceof HTMLButtonElement
          ? event.submitter
          : form.querySelector("button[type='submit']");
      const action = new URL(form.action).pathname;
      if (syncNotices && submitter && (action === "/sync" || (action === "/sync_setup" && submitter.value === "sync"))) {
        event.preventDefault();
        if (!submitter.disabled) submitBackgroundSync(form, submitter);
        return;
      }
      if (submitter) {
        requestAnimationFrame(() => {
          submitter.disabled = true;
        });
      }
    });
  }

  for (const input of document.querySelectorAll("[data-import-file]")) {
    input.closest("[data-import-file-picker]").hidden = false;
    const form = input.form;
    const status = form.querySelector("[data-import-file-status]");
    const title = form.elements.namedItem("title");
    const body = form.elements.namedItem("body");
    const button = form.querySelector('button[type="submit"]');
    input.addEventListener("change", async () => {
      const [file] = input.files;
      if (!file) {
        button.disabled = false;
        return;
      }
      button.disabled = true;
      try {
        if (!/\.(md|mdc|txt)$/i.test(file.name) || file.size > body.maxLength * 4) {
          throw new Error("请选择大小合适的 Markdown 或纯文本文件。");
        }
        const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
        if (file !== input.files[0]) return;
        if (text.length > body.maxLength || !text.trim() || text.includes("\0")) {
          throw new Error("文件为空、过长或包含无法导入的字符。");
        }
        title.value = file.name.replace(/\.[^.]+$/, "").slice(0, title.maxLength);
        body.value = text;
        status.textContent = "已载入文件，可以核对后加入待确认。";
      } catch (error) {
        if (file === input.files[0]) {
          status.textContent = error instanceof TypeError ? "文件须为 UTF-8 文本。" : error.message;
        }
      } finally {
        if (file === input.files[0]) button.disabled = false;
      }
    });
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }

  for (const btn of document.querySelectorAll("[data-copy-target]")) {
    btn.addEventListener("click", async () => {
      const src = document.getElementById(btn.getAttribute("data-copy-target"));
      if (!src) return;
      const orig = btn.dataset.origLabel || btn.textContent;
      btn.dataset.origLabel = orig;
      try {
        await copyText(src.value ?? src.textContent ?? "");
        btn.textContent = "已复制";
      } catch {
        btn.textContent = "复制失败";
      }
      window.setTimeout(() => {
        btn.textContent = orig;
      }, 1500);
    });
  }

  document.addEventListener("click", async (event) => {
    const btn = event.target && event.target.closest ? event.target.closest("[data-copy-code]") : null;
    if (!btn) return;
    const wrap = btn.closest(".code-wrap");
    const code = wrap && wrap.querySelector("pre code");
    if (!code) return;
    const orig = btn.dataset.origLabel || btn.textContent;
    btn.dataset.origLabel = orig;
    try {
      await copyText(code.textContent ?? "");
      btn.textContent = "已复制";
    } catch {
      btn.textContent = "复制失败";
    }
    window.setTimeout(() => {
      btn.textContent = orig;
    }, 1500);
  });

  // Live Markdown preview, char/token counter, and pre-save secret warning in the editor.
  const editorBody = document.getElementById("body");
  const editorPreview = document.getElementById("editor-preview");
  const editorMetrics = document.getElementById("editor-metrics");
  const editorSecretAlert = document.getElementById("editor-secret-alert");
  const editorTabs = Array.from(document.querySelectorAll("[data-editor-tab]"));
  if (editorBody && editorPreview && editorTabs.length) {
    const form = editorBody.closest("form");
    let previewSeq = 0;
    let editorMode = "write";

    function estimateLocalTokens(text) {
      if (!text) return 0;
      let ascii = 0;
      for (let i = 0; i < text.length; i += 1) {
        if (text.charCodeAt(i) < 128) ascii += 1;
      }
      const nonAscii = text.length - ascii;
      return Math.max(1, Math.round(ascii / 4 + nonAscii / 1.5));
    }

    function updateEditorMetrics(chars, tokens) {
      if (!editorMetrics) return;
      const c = typeof chars === "number" ? chars : editorBody.value.length;
      const t = typeof tokens === "number" ? tokens : estimateLocalTokens(editorBody.value);
      editorMetrics.textContent = `${c} 字 · 约 ${t} tokens`;
      editorMetrics.classList.toggle("is-long", c > 6000);
    }

    async function fetchPreview() {
      if (!form) return;
      const seq = ++previewSeq;
      const data = new URLSearchParams();
      const csrfInput = form.querySelector('input[name="csrf"]');
      if (csrfInput) data.set("csrf", csrfInput.value);
      const libInput = form.querySelector('input[name="library"]');
      if (libInput) data.set("library", libInput.value);
      const origTopic = form.querySelector('input[name="orig_topic"]');
      if (origTopic) data.set("orig_topic", origTopic.value);
      const titleInput = form.querySelector('input[name="title"]');
      if (titleInput) data.set("title", titleInput.value);
      const descInput = form.querySelector('input[name="description"]');
      if (descInput) data.set("description", descInput.value);
      data.set("body", editorBody.value);

      editorPreview.textContent = "正在渲染预览…";
      try {
        const res = await fetch("/preview_markdown", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
          body: data.toString(),
          credentials: "same-origin",
        });
        if (seq !== previewSeq) return;
        if (!res.ok) {
          editorPreview.textContent = "预览渲染失败，请回到编辑模式继续。";
          return;
        }
        const payload = await res.json();
        if (seq !== previewSeq) return;
        editorPreview.innerHTML = payload.html || "";
        updateEditorMetrics(payload.chars, payload.tokens);
        if (editorSecretAlert) {
          if (payload.secret_warning) {
            editorSecretAlert.textContent = payload.secret_warning;
            editorSecretAlert.hidden = false;
          } else {
            editorSecretAlert.textContent = "";
            editorSecretAlert.hidden = true;
          }
        }
      } catch {
        if (seq === previewSeq) {
          editorPreview.textContent = "预览请求失败，请回到编辑模式继续。";
        }
      }
    }

    function setEditorMode(mode) {
      editorMode = mode === "preview" ? "preview" : "write";
      for (const tab of editorTabs) {
        tab.setAttribute("aria-selected", tab.dataset.editorTab === editorMode ? "true" : "false");
      }
      const showPreview = editorMode === "preview";
      editorBody.hidden = showPreview;
      editorPreview.hidden = !showPreview;
      if (showPreview) {
        fetchPreview();
      } else {
        editorBody.focus();
      }
    }

    for (const tab of editorTabs) {
      tab.addEventListener("click", () => setEditorMode(tab.dataset.editorTab));
    }
    editorBody.addEventListener("input", () => updateEditorMetrics());
    updateEditorMetrics();
  }

  // Quick command palette (Cmd/Ctrl+K) across all mounted libraries + quick actions.
  let paletteDialog = null;
  let paletteInput = null;
  let paletteList = null;
  let paletteEmpty = null;
  let paletteItems = [];
  let paletteIndex = 0;
  let paletteSeq = 0;
  let paletteTimer = 0;

  function currentLibraryKey() {
    const params = new URLSearchParams(window.location.search);
    return params.get("library") || "primary";
  }

  function ensurePalette() {
    if (paletteDialog) return paletteDialog;
    const dialog = document.createElement("dialog");
    dialog.className = "cmdk-dialog";
    dialog.setAttribute("aria-label", "快速跳转与搜索");

    const head = document.createElement("div");
    head.className = "cmdk-head";
    const input = document.createElement("input");
    input.type = "search";
    input.className = "cmdk-input";
    input.placeholder = "搜索所有记忆库、标题、关键词，或输入操作名称…";
    input.setAttribute("aria-label", "快速搜索");
    const escHint = document.createElement("kbd");
    escHint.className = "cmdk-kbd";
    escHint.textContent = "ESC";
    head.append(input, escHint);

    const list = document.createElement("div");
    list.className = "cmdk-list";
    list.setAttribute("role", "listbox");

    const empty = document.createElement("p");
    empty.className = "cmdk-empty";
    empty.hidden = true;
    empty.textContent = "没有匹配的记忆或操作。";

    const foot = document.createElement("div");
    foot.className = "cmdk-foot";
    foot.textContent = "↑↓ 选择 · Enter 打开 · Esc 关闭";

    dialog.append(head, list, empty, foot);
    document.body.appendChild(dialog);

    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closePalette();
    });
    input.addEventListener("input", () => {
      window.clearTimeout(paletteTimer);
      paletteTimer = window.setTimeout(() => fetchPalette(input.value), 120);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        movePalette(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        movePalette(-1);
      } else if (event.key === "Enter") {
        const active = paletteItems[paletteIndex];
        if (active && active.href) {
          event.preventDefault();
          closePalette();
          window.location.href = active.href;
        }
      } else if (event.key === "Escape") {
        event.preventDefault();
        closePalette();
      }
    });

    paletteDialog = dialog;
    paletteInput = input;
    paletteList = list;
    paletteEmpty = empty;
    return dialog;
  }

  function setPaletteActive(index) {
    const rows = paletteList ? paletteList.querySelectorAll(".cmdk-item") : [];
    if (!rows.length) {
      paletteIndex = 0;
      return;
    }
    paletteIndex = ((index % rows.length) + rows.length) % rows.length;
    rows.forEach((row, idx) => {
      const active = idx === paletteIndex;
      row.classList.toggle("is-active", active);
      row.setAttribute("aria-selected", active ? "true" : "false");
      if (active) row.scrollIntoView({ block: "nearest" });
    });
  }

  function movePalette(delta) {
    setPaletteActive(paletteIndex + delta);
  }

  function renderPalette(data) {
    paletteList.textContent = "";
    paletteItems = [];

    const appendSection = (label, entries, kind) => {
      if (!entries || !entries.length) return;
      const heading = document.createElement("div");
      heading.className = "cmdk-group";
      heading.textContent = label;
      paletteList.appendChild(heading);

      for (const entry of entries) {
        const idx = paletteItems.length;
        paletteItems.push(entry);
        const link = document.createElement("a");
        link.className = "cmdk-item";
        link.href = entry.href;
        link.setAttribute("role", "option");

        const main = document.createElement("div");
        main.className = "cmdk-item-main";

        const top = document.createElement("div");
        top.className = "cmdk-item-title";
        if (kind === "item" && entry.layer) {
          const badge = document.createElement("span");
          badge.className = "cmdk-layer";
          badge.textContent = entry.layer;
          top.appendChild(badge);
        }
        const titleText = document.createElement("span");
        titleText.textContent = entry.title || entry.ref || "";
        top.appendChild(titleText);
        if (kind === "item" && entry.expired) {
          const exp = document.createElement("span");
          exp.className = "cmdk-expired";
          exp.textContent = "已过期";
          top.appendChild(exp);
        }
        main.appendChild(top);

        const sub = entry.description || entry.subtitle || "";
        if (sub) {
          const desc = document.createElement("div");
          desc.className = "cmdk-item-sub";
          desc.textContent = sub;
          main.appendChild(desc);
        }
        link.appendChild(main);

        if (kind === "item" && entry.library) {
          const meta = document.createElement("span");
          meta.className = "cmdk-item-meta";
          meta.textContent = entry.library;
          link.appendChild(meta);
        } else if (kind === "action" && entry.shortcut) {
          const kbd = document.createElement("kbd");
          kbd.className = "cmdk-kbd";
          kbd.textContent = entry.shortcut;
          link.appendChild(kbd);
        }

        link.addEventListener("mouseenter", () => setPaletteActive(idx));
        paletteList.appendChild(link);
      }
    };

    if (data.query) {
      appendSection("记忆条目", data.items, "item");
      appendSection("快捷操作", data.actions, "action");
    } else {
      appendSection("最近更新", data.items, "item");
      appendSection("快捷操作", data.actions, "action");
    }

    paletteEmpty.hidden = paletteItems.length > 0;
    setPaletteActive(0);
  }

  async function fetchPalette(q) {
    const seq = ++paletteSeq;
    try {
      const url = `/quick_search?library=${encodeURIComponent(currentLibraryKey())}&q=${encodeURIComponent(q || "")}`;
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok || seq !== paletteSeq) return;
      const data = await response.json();
      if (seq !== paletteSeq) return;
      renderPalette(data);
    } catch {
      // Ignore transient cancellation or local server reload errors.
    }
  }

  function openPalette() {
    const dialog = ensurePalette();
    if (!dialog.open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
    }
    paletteInput.value = "";
    paletteInput.focus();
    fetchPalette("");
  }

  function closePalette() {
    if (!paletteDialog) return;
    if (typeof paletteDialog.close === "function" && paletteDialog.open) {
      paletteDialog.close();
    } else {
      paletteDialog.removeAttribute("open");
    }
  }

  window.__openQuickPalette = openPalette;

  for (const trigger of document.querySelectorAll("[data-cmdk-trigger]")) {
    trigger.addEventListener("click", () => openPalette());
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      for (const menu of document.querySelectorAll("details.action-menu[open]")) {
        menu.removeAttribute("open");
      }
    }
    if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (paletteDialog && paletteDialog.open) {
        closePalette();
      } else {
        openPalette();
      }
      return;
    }
    if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "n") {
      const tag = (event.target && event.target.tagName) ? event.target.tagName.toLowerCase() : "";
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      event.preventDefault();
      window.location.href = `/?library=${encodeURIComponent(currentLibraryKey())}&view=new#detail-title`;
    }
  });

  document.addEventListener("click", (event) => {
    for (const menu of document.querySelectorAll("details.action-menu[open]")) {
      if (!menu.contains(event.target)) {
        menu.removeAttribute("open");
      }
    }
    const diffBtn = event.target.closest("[data-version-diff]");
    if (diffBtn) {
      const li = diffBtn.closest("li");
      const slot = li ? li.querySelector(".version-diff-slot") : null;
      if (!slot) return;
      if (!slot.hidden && slot.dataset.loaded === "1") {
        slot.hidden = true;
        diffBtn.textContent = "对比差异";
        return;
      }
      if (slot.dataset.loaded === "1") {
        slot.hidden = false;
        diffBtn.textContent = "收起差异";
        return;
      }
      const library = diffBtn.getAttribute("data-library") || currentLibraryKey();
      const version = diffBtn.getAttribute("data-version") || "";
      diffBtn.disabled = true;
      diffBtn.textContent = "加载中…";
      fetch(`/version_diff?library=${encodeURIComponent(library)}&version=${encodeURIComponent(version)}`, { cache: "no-store" })
        .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
        .then(({ ok, data }) => {
          slot.replaceChildren();
          if (!ok || data.error) {
            const p = document.createElement("p");
            p.className = "field-hint";
            p.textContent = data.error || "无法加载版本差异。";
            slot.appendChild(p);
          } else if (data.identical) {
            const p = document.createElement("p");
            p.className = "field-hint";
            p.textContent = "该历史备份与当前正文内容完全一致。";
            slot.appendChild(p);
          } else {
            const summary = document.createElement("div");
            summary.className = "version-diff-meta";
            const stats = [];
            if (data.added) stats.push(`当前新增 +${data.added} 行`);
            if (data.removed) stats.push(`当前删去 -${data.removed} 行`);
            if (data.verified_at) stats.push(`备份核实日期 ${data.verified_at}`);
            if (data.valid_until) stats.push(`备份有效期 ${data.valid_until}`);
            summary.textContent = stats.join(" · ") || "正文差异对比（备份 → 当前）";
            slot.appendChild(summary);

            const box = document.createElement("div");
            box.className = "diff-view";
            for (const line of data.diff_lines || []) {
              const row = document.createElement("div");
              let cls = "diff-ctx";
              if (line.startsWith("+++") || line.startsWith("---")) cls = "diff-meta";
              else if (line.startsWith("@@")) cls = "diff-hunk";
              else if (line.startsWith("+")) cls = "diff-add";
              else if (line.startsWith("-")) cls = "diff-del";
              row.className = `diff-line ${cls}`;
              row.textContent = line;
              box.appendChild(row);
            }
            slot.appendChild(box);
            if (data.truncated) {
              const more = document.createElement("p");
              more.className = "field-hint";
              more.textContent = "差异较长，仅展示前 300 行。";
              slot.appendChild(more);
            }
          }
          slot.dataset.loaded = "1";
          slot.hidden = false;
          diffBtn.disabled = false;
          diffBtn.textContent = "收起差异";
        })
        .catch(() => {
          slot.replaceChildren();
          const p = document.createElement("p");
          p.className = "field-hint";
          p.textContent = "无法加载版本差异。";
          slot.appendChild(p);
          slot.hidden = false;
          diffBtn.disabled = false;
          diffBtn.textContent = "重试对比";
        });
    }
  });

  // Desktop software updater UI (only active inside Electron where preload exposes window.umemDesktop).
  const desktopApi = window.umemDesktop;
  const railVersionEl = document.getElementById("rail-version");
  const updateBannerEl = document.getElementById("app-update-banner");
  if (desktopApi && (railVersionEl || updateBannerEl)) {
    const formatBytes = (bytes) => {
      const n = Number(bytes || 0);
      if (!Number.isFinite(n) || n <= 0) return "";
      if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
      if (n >= 1024) return `${(n / 1024).toFixed(0)} KB`;
      return `${n} B`;
    };
    const makeBtn = (label, cls, onClick, disabled) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = cls;
      btn.textContent = label;
      if (disabled) btn.disabled = true;
      if (onClick) btn.addEventListener("click", onClick);
      return btn;
    };
    const firstSummaryLine = (notes) => {
      const lines = String(notes || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      if (!lines.length) return "";
      const text = lines[0].replace(/^#+\s*/, "");
      return text.length > 140 ? `${text.slice(0, 140)}…` : text;
    };
    const isSnoozed = (version) => {
      try { return Boolean(version && sessionStorage.getItem("umem_update_snooze") === version); } catch { return false; }
    };
    const setSnoozed = (version) => {
      try {
        if (version) sessionStorage.setItem("umem_update_snooze", version);
        else sessionStorage.removeItem("umem_update_snooze");
      } catch {}
    };

    let latestState = null;
    let upToDateUntil = 0;
    let upToDateTimer = null;
    const renderUpdateUi = (state) => {
      if (!state || typeof state !== "object") return;
      latestState = state;

      if (railVersionEl) {
        railVersionEl.replaceChildren();
        railVersionEl.hidden = false;
        const verSpan = document.createElement("span");
        verSpan.className = "rail-ver-tag";
        verSpan.textContent = state.currentVersion ? `v${state.currentVersion}` : "";
        railVersionEl.appendChild(verSpan);

        let btnLabel = "检查更新";
        let btnClass = "rail-ver-btn";
        let btnDisabled = false;
        let btnAction = () => {
          setSnoozed("");
          desktopApi.checkForUpdates().then((next) => {
            if (next && next.status === "up-to-date") {
              upToDateUntil = Date.now() + 4500;
              if (upToDateTimer) clearTimeout(upToDateTimer);
              upToDateTimer = setTimeout(() => {
                upToDateUntil = 0;
                renderUpdateUi(latestState);
              }, 4600);
            }
            renderUpdateUi(next);
          }).catch(() => {});
        };

        if (state.status === "checking") {
          btnLabel = "检查中…";
          btnDisabled = true;
        } else if (state.status === "downloading") {
          const pct = state.progress && typeof state.progress.percent === "number" ? `${state.progress.percent}%` : "";
          btnLabel = pct ? `下载 ${pct}` : "下载中…";
          btnClass = "rail-ver-btn is-available";
          btnDisabled = true;
        } else if (state.status === "verifying") {
          btnLabel = "校验中…";
          btnClass = "rail-ver-btn is-available";
          btnDisabled = true;
        } else if (state.status === "ready") {
          btnLabel = "重启更新";
          btnClass = "rail-ver-btn is-ready";
          btnAction = () => desktopApi.installUpdate().then(renderUpdateUi).catch(() => {});
        } else if (state.status === "installing") {
          btnLabel = "安装中…";
          btnClass = "rail-ver-btn is-ready";
          btnDisabled = true;
        } else if (state.status === "available" && state.latestVersion) {
          btnLabel = `更新 v${state.latestVersion}`;
          btnClass = "rail-ver-btn is-available";
          btnAction = () => {
            setSnoozed("");
            if (state.canAutoInstall) {
              desktopApi.downloadUpdate().then(renderUpdateUi).catch(() => {});
            } else {
              desktopApi.openReleasePage(state.releaseUrl).catch(() => {});
            }
          };
        } else if (state.status === "error") {
          btnLabel = "重试更新";
          btnClass = "rail-ver-btn is-error";
        } else if (state.status === "up-to-date" && Date.now() < upToDateUntil) {
          btnLabel = "已是最新";
          btnClass = "rail-ver-btn is-ready";
        }
        railVersionEl.appendChild(makeBtn(btnLabel, btnClass, btnAction, btnDisabled));
      }

      if (!updateBannerEl) return;
      updateBannerEl.replaceChildren();
      updateBannerEl.className = "update-banner";

      const mountBanner = ({ variant = "", title, detail = "", progress = null, actions = [] }) => {
        if (variant) updateBannerEl.classList.add(variant);
        const main = document.createElement("div");
        main.className = "update-banner-main";
        const titleEl = document.createElement("div");
        titleEl.className = "update-banner-title";
        titleEl.textContent = title;
        main.appendChild(titleEl);
        if (detail) {
          const detailEl = document.createElement("div");
          detailEl.className = "update-banner-detail";
          detailEl.textContent = detail;
          main.appendChild(detailEl);
        }
        if (progress) {
          const bar = document.createElement("progress");
          bar.className = "update-progress";
          if (typeof progress.percent === "number") {
            bar.max = 100;
            bar.value = progress.percent;
          }
          main.appendChild(bar);
        }
        updateBannerEl.appendChild(main);
        if (actions.length) {
          const box = document.createElement("div");
          box.className = "update-banner-actions";
          for (const btn of actions) box.appendChild(btn);
          updateBannerEl.appendChild(box);
        }
        updateBannerEl.hidden = false;
      };

      const skipped = Boolean(
        state.latestVersion && (state.skippedVersion === state.latestTag || state.skippedVersion === state.latestVersion)
      );

      if (state.notice && state.notice.message) {
        const isErr = state.notice.kind === "error";
        const actions = [
          makeBtn("知道了", "btn btn-ghost btn-compact", () => {
            desktopApi.dismissNotice().then(renderUpdateUi).catch(() => {});
          }),
        ];
        if (isErr) {
          actions.unshift(
            makeBtn("查看日志", "btn btn-ghost btn-compact", () => desktopApi.openUpdateLog().catch(() => {})),
            makeBtn("前往发布页", "btn btn-primary btn-compact", () => desktopApi.openReleasePage(state.releaseUrl).catch(() => {}))
          );
        }
        mountBanner({
          variant: isErr ? "is-error" : "is-success",
          title: state.notice.message,
          detail: state.notice.detail || "",
          actions,
        });
        return;
      }

      if (state.status === "downloading") {
        const p = state.progress || {};
        const parts = [];
        if (p.transferred) {
          parts.push(p.total ? `${formatBytes(p.transferred)} / ${formatBytes(p.total)}` : formatBytes(p.transferred));
        }
        if (p.bytesPerSecond) parts.push(`${formatBytes(p.bytesPerSecond)}/s`);
        const pctText = typeof p.percent === "number" ? `（${p.percent}%）` : "";
        mountBanner({
          title: `正在下载 Unified Memory ${state.latestVersion}${pctText}`,
          detail: parts.join(" · ") || "下载完成后将自动校验 SHA-256 与应用签名。",
          progress: p,
          actions: [
            makeBtn("取消", "btn btn-ghost btn-compact", () => {
              desktopApi.cancelDownload().then(renderUpdateUi).catch(() => {});
            }),
          ],
        });
        return;
      }

      if (state.status === "verifying") {
        mountBanner({
          title: `正在校验 Unified Memory ${state.latestVersion} 更新包…`,
          detail: "正在解压并验证 Bundle ID、版本号、签名与内嵌运行环境。",
          progress: {},
        });
        return;
      }

      if (state.status === "installing") {
        mountBanner({
          variant: "is-success",
          title: `正在安装 Unified Memory ${state.latestVersion}…`,
          detail: "应用将自动退出、原子替换程序包并重新打开，记忆库不受影响。",
        });
        return;
      }

      if (state.status === "ready" && state.latestVersion && !isSnoozed(`ready:${state.latestVersion}`)) {
        mountBanner({
          variant: "is-success",
          title: `Unified Memory ${state.latestVersion} 已校验就绪`,
          detail: "重启后将自动替换当前应用并刷新客户端启动器；异常时自动回滚，记忆库不受影响。",
          actions: [
            makeBtn("立即重启并安装", "btn btn-primary btn-compact", () => {
              desktopApi.installUpdate().then(renderUpdateUi).catch(() => {});
            }),
            makeBtn("稍后", "btn btn-ghost btn-compact", () => {
              setSnoozed(`ready:${state.latestVersion}`);
              renderUpdateUi(latestState);
            }),
          ],
        });
        return;
      }

      if (state.status === "available" && state.latestVersion && !skipped && !isSnoozed(`avail:${state.latestVersion}`)) {
        const summary = firstSummaryLine(state.releaseNotes);
        const detail = state.canAutoInstall
          ? (summary || `当前版本 ${state.currentVersion}，支持应用内一键下载、SHA-256 校验与安全替换。`)
          : (state.installBlockDetail || `当前版本 ${state.currentVersion}，请前往 GitHub 发布页下载替换。`);
        const actions = [];
        if (state.canAutoInstall) {
          actions.push(makeBtn("立即下载并更新", "btn btn-primary btn-compact", () => {
            desktopApi.downloadUpdate().then(renderUpdateUi).catch(() => {});
          }));
        } else {
          actions.push(makeBtn("前往发布页", "btn btn-primary btn-compact", () => {
            desktopApi.openReleasePage(state.releaseUrl).catch(() => {});
          }));
        }
        actions.push(makeBtn("查看更新说明", "btn btn-ghost btn-compact", () => {
          desktopApi.openReleasePage(state.releaseUrl).catch(() => {});
        }));
        actions.push(makeBtn("跳过此版本", "btn btn-ghost btn-compact", () => {
          desktopApi.skipUpdate(state.latestTag || state.latestVersion).then(renderUpdateUi).catch(() => {});
        }));
        actions.push(makeBtn("稍后", "btn btn-ghost btn-compact", () => {
          setSnoozed(`avail:${state.latestVersion}`);
          renderUpdateUi(latestState);
        }));
        mountBanner({
          title: `发现新版本 Unified Memory ${state.latestVersion}`,
          detail,
          actions,
        });
        return;
      }

      if (state.status === "error" && state.error) {
        mountBanner({
          variant: "is-error",
          title: "自动更新未完成",
          detail: state.error,
          actions: [
            makeBtn("重试", "btn btn-primary btn-compact", () => {
              if (state.latestVersion && state.canAutoInstall) {
                desktopApi.downloadUpdate().then(renderUpdateUi).catch(() => {});
              } else {
                desktopApi.checkForUpdates().then(renderUpdateUi).catch(() => {});
              }
            }),
            makeBtn("前往发布页", "btn btn-ghost btn-compact", () => {
              desktopApi.openReleasePage(state.releaseUrl).catch(() => {});
            }),
            makeBtn("查看日志", "btn btn-ghost btn-compact", () => {
              desktopApi.openUpdateLog().catch(() => {});
            }),
          ],
        });
        return;
      }

      updateBannerEl.hidden = true;
    };

    desktopApi.onUpdateState(renderUpdateUi);
    desktopApi.getUpdateState().then(renderUpdateUi).catch(() => {});
  }
})();

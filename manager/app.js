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
  });
})();

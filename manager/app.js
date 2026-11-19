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

  // 5. Prevent accidental double-submission after form validation succeeds.
  for (const form of document.querySelectorAll("form[method='post'], form[method='POST']")) {
    form.addEventListener("submit", (event) => {
      if (!form.checkValidity()) return;
      const submitter =
        event.submitter instanceof HTMLButtonElement
          ? event.submitter
          : form.querySelector("button[type='submit']");
      if (submitter) {
        requestAnimationFrame(() => {
          submitter.disabled = true;
        });
      }
    });
  }
})();

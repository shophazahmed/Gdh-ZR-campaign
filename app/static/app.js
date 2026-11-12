// Progressive enhancements only; every page works without JavaScript.
document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-autosubmit]")) event.target.form.requestSubmit();
});

document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-back]");
  if (link && document.referrer.startsWith(location.origin) && history.length > 1) {
    event.preventDefault();
    history.back();
  }
});

// Canvassing status on the voter cards: save in the background so the list keeps its place.
document.addEventListener("change", async (event) => {
  const select = event.target.closest("[data-status-select]");
  if (!select) return;
  const form = select.form;
  const card = form.closest(".vcard");
  const state = form.querySelector(".save-state");
  const previous = [...card.classList].find((c) => c.startsWith("st-"));
  state.textContent = "Saving…";
  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    if (previous) card.classList.remove(previous);
    card.classList.add("st-" + data.status);
    const oldKey = previous ? previous.slice(3) : null;
    const bump = (key, delta) => {
      const el = document.querySelector(`[data-count="${key}"]`);
      if (el) el.textContent = Math.max(0, parseInt(el.textContent, 10) + delta);
    };
    if (data.changed && oldKey && oldKey !== data.status) { bump(oldKey, -1); bump(data.status, 1); }
    const who = card.querySelector("[data-who]");
    if (who && data.changed) {
      let line = who.querySelector("[data-who-status]");
      if (!line) { line = document.createElement("span"); line.dataset.whoStatus = ""; who.prepend(line); }
      const name = document.createElement("strong");
      name.textContent = data.by;
      line.replaceChildren(`${data.status === "new" ? "Changed" : data.label} by `, name, ` · ${data.at}`);
    }
    state.textContent = "Saved ✓";
    setTimeout(() => { state.textContent = ""; }, 2000);
  } catch (err) {
    state.textContent = "Not saved, try again";
    form.submit();  // fall back to a normal post (shows the error page if the session expired)
  }
});

// Type-to-filter for long lists (house assignments).
document.addEventListener("input", (event) => {
  const input = event.target.closest("[data-filter-rows]");
  if (!input) return;
  const term = input.value.trim().toLowerCase();
  document.querySelectorAll(`${input.dataset.filterRows} > [data-filter-text]`).forEach((row) => {
    row.hidden = term !== "" && !row.dataset.filterText.includes(term);
  });
});

// Confirm for a single button (forms use data-confirm on the form itself).
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-confirm-click]");
  if (button && !window.confirm(button.dataset.confirmClick)) event.preventDefault();
});

// Small message at the bottom of the screen.
function toast(text) {
  const el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "status");
  el.textContent = text;
  document.body.append(el);
  setTimeout(() => el.remove(), 3500);
}

// Viber can't pre-fill text for a number: copy the campaign message, then open the chat.
document.addEventListener("click", async (event) => {
  const link = event.target.closest("a[data-copy]");
  if (!link) return;
  event.preventDefault();
  let copied = false;
  try {
    await navigator.clipboard.writeText(link.dataset.copy);
    copied = true;
  } catch (err) { /* no clipboard access: open the chat anyway */ }
  toast(copied ? "Message copied. In Viber, long-press the text box and tap Paste."
               : "Couldn't copy the message. Type it in Viber.");
  setTimeout(() => { window.location.href = link.href; }, 400);
});

// Send the campaign picture with the message through the phone's share sheet (pick WhatsApp,
// then the voter). Only shown on browsers that can share files.
(async () => {
  const button = document.querySelector("[data-share-picture]");
  if (!button || !navigator.canShare) return;
  let file = null;
  try {
    const response = await fetch(button.dataset.sharePicture);
    file = new File([await response.blob()], "campaign.jpg", { type: "image/jpeg" });
  } catch (err) { return; }
  if (!navigator.canShare({ files: [file] })) return;
  button.hidden = false;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.shareText).catch(() => {});
      await navigator.share({ files: [file], text: button.dataset.shareText });
    } catch (err) { /* cancelled */ }
  });
})();

// 100% confirmed toggle on the voter cards ('admin' account only): save in the background.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-confirm-toggle]");
  if (!form) return;
  event.preventDefault();
  const input = form.querySelector("input[name=confirmed]");
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    input.value = data.confirmed ? "0" : "1";
    button.classList.toggle("on", data.confirmed);
    button.setAttribute("aria-pressed", data.confirmed ? "true" : "false");
    button.querySelector("span").textContent = data.confirmed ? "100% confirmed" : "Mark confirmed";
    toast(`${data.confirmed ? "Confirmed" : "Removed"} · ${data.metrics.confirmed} of ${data.metrics.total} (${data.metrics.pct}%)`);
  } catch (err) {
    form.submit();
  } finally {
    button.disabled = false;
  }
});

// Bulk assign on Search: tick all on the page / clear, with a live count.
(() => {
  const tools = document.querySelector("[data-pick-tools]");
  if (!tools) return;
  const boxes = () => [...document.querySelectorAll("input.pick")];
  const count = tools.querySelector("[data-pick-count]");
  const scope = document.querySelector("#bulk-assign select[name=scope]");
  const update = () => { count.textContent = boxes().filter((b) => b.checked).length; };
  tools.hidden = false;
  tools.addEventListener("click", (event) => {
    const button = event.target.closest("[data-pick]");
    if (!button) return;
    boxes().forEach((b) => { b.checked = button.dataset.pick === "page"; });
    if (scope && button.dataset.pick === "page") scope.value = "selected";
    update();
  });
  document.addEventListener("change", (event) => {
    if (event.target.matches("input.pick")) { if (scope) scope.value = "selected"; update(); }
  });
  update();
})();

// PDF lists: on phones, open the share sheet with the file (pick WhatsApp, then the agent).
// Anywhere sharing files isn't possible, the link simply downloads the PDF.
document.addEventListener("click", async (event) => {
  const link = event.target.closest("[data-share-pdf]");
  if (!link || !navigator.canShare || !window.File) return;
  if (!navigator.canShare({ files: [new File([""], "x.pdf", { type: "application/pdf" })] })) return;
  event.preventDefault();
  if (link._file) {  // second tap: the PDF is ready, a fresh tap always opens the share sheet
    try { await navigator.share({ files: [link._file], title: link._file.name }); } catch (err) { /* cancelled */ }
    return;
  }
  const label = link.innerHTML;
  link.textContent = "Preparing…";
  try {
    const response = await fetch(link.href, { credentials: "same-origin" });
    if (!response.ok || !(response.headers.get("Content-Type") || "").includes("pdf")) {
      link.innerHTML = label;
      window.location = link.href;  // shows the error (e.g. too many voters) on the normal page
      return;
    }
    const name = (/filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "") || [])[1] || "voters.pdf";
    const file = new File([await response.blob()], name, { type: "application/pdf" });
    link.innerHTML = label;
    try {
      await navigator.share({ files: [file], title: name });
    } catch (err) {
      if (err && err.name === "NotAllowedError") {
        // iPhone: preparing took too long for this tap. Keep the file; the next tap sends it.
        link._file = file;
        link.textContent = "Tap to send PDF ›";
        link.classList.add("primary");
        setTimeout(() => { link._file = null; link.innerHTML = label; link.classList.remove("primary"); }, 120000);
      } else if (err && err.name !== "AbortError") {
        window.location = link.href;
      }
    }
  } catch (err) {
    link.innerHTML = label;
    window.location = link.href;
  }
});

// Share voter cards on WhatsApp (owner only). Step 1 prepares the cards; step 2 is a tap on
// "Send on WhatsApp", because phones only open the share sheet straight after a tap.
document.addEventListener("change", (event) => {
  const to = event.target.closest("[data-share-to]");
  if (!to) return;
  const form = to.form;
  form.querySelector("[data-share-number]").hidden = to.value !== "number";
  form.querySelector("[data-share-assign]").hidden = !/^\d+$/.test(to.value);
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-share-form]");
  if (!form) return;
  event.preventDefault();
  const status = form.querySelector("[data-share-status]");
  const button = form.querySelector("button.primary");
  const data = new FormData(form);
  if (!data.getAll("ids").length) {
    document.querySelectorAll("input.pick:checked").forEach((b) => data.append("ids", b.value));
  }
  if (!data.getAll("ids").length) { status.textContent = "Tick the voters to share first."; return; }
  status.textContent = "Preparing…";
  let reply;
  try {
    const response = await fetch(form.action, { method: "POST", body: data, credentials: "same-origin",
                                                headers: { "X-Requested-With": "fetch" } });
    reply = await response.json();
    if (!reply.ok) { status.textContent = reply.error || "Couldn't share."; return; }
  } catch (err) { status.textContent = "Couldn't reach the portal. Try again."; return; }
  const who = reply.to ? ` for ${reply.to}` : "";
  const assigned = reply.assigned ? ` ${reply.assigned} assigned to ${reply.to}.` : "";
  if (reply.mode === "text") {
    status.textContent = `Opening WhatsApp${who}…${assigned}`;
    window.open(reply.wa_url, "_blank", "noopener");
    return;
  }
  const files = [];
  for (const [i, url] of reply.cards.entries()) {
    const blob = await (await fetch(url, { credentials: "same-origin" })).blob();
    files.push(new File([blob], reply.names[i], { type: "image/png" }));
  }
  if (navigator.canShare && navigator.canShare({ files })) {
    status.textContent = `Ready: ${files.length} card${files.length > 1 ? "s" : ""}${who}.${assigned}` +
      (reply.to ? ` In WhatsApp, pick ${reply.to}.` : "");
    const send = document.createElement("button");
    send.type = "button";
    send.className = "button primary";
    send.textContent = "Send on WhatsApp";
    send.addEventListener("click", async () => {
      try { await navigator.share({ files }); status.textContent = "Shared."; send.remove(); }
      catch (err) { if (err.name !== "AbortError") status.textContent = "Sharing didn't work; try 'Text only'."; }
    });
    button.after(send);
    return;
  }
  // A PC: save the cards, then open WhatsApp (Web) at the chat so they can be attached
  files.forEach((file) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(file);
    a.download = file.name;
    a.click();
  });
  status.textContent = `Saved ${files.length} card${files.length > 1 ? "s" : ""}${who}. Attach ${files.length > 1 ? "them" : "it"} in WhatsApp.${assigned}`;
  if (reply.direct) window.open(reply.wa_url, "_blank", "noopener");
});

// Coming back from a voter: return to that voter's card in the list, not the top of the page.
// Search pages aren't kept in the phone's memory (privacy), so the browser reloads them on Back;
// we remember only which card was opened, in this tab, and scroll to it.
(() => {
  const KEY = "lastVoterCard";
  const here = () => location.pathname + location.search;
  const store = (value) => { try { sessionStorage.setItem(KEY, JSON.stringify(value)); } catch (e) { /* private mode */ } };
  const recall = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch (e) { return null; } };
  document.addEventListener("click", (event) => {
    const card = event.target.closest(".vcard[id^='v-']");
    if (card && event.target.closest("a")) store({ page: here(), id: card.id });
  });
  const show = (id) => {
    const card = document.getElementById(id);
    if (!card) return false;
    card.scrollIntoView({ block: "center" });
    card.classList.add("just-viewed");
    setTimeout(() => card.classList.remove("just-viewed"), 2500);
    return true;
  };
  const restore = () => {
    if (location.hash.startsWith("#v-")) { show(location.hash.slice(1)); return; }
    const last = recall();
    if (last && last.page === here()) show(last.id);
  };
  window.addEventListener("pageshow", restore);
})();

// Copy a link to the clipboard (Agents page: dashboard links).
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const label = button.textContent;
  try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = "Copied ✓"; }
  catch (err) { window.prompt("Copy this link:", button.dataset.copy); }
  setTimeout(() => { button.textContent = label; }, 2000);
});

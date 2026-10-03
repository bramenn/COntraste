// Contraste front end. No dependencies. External text always goes through textContent, never innerHTML.
(() => {
  const $ = (id) => document.getElementById(id);
  // Keep the active filter chip visible: on phones the row scrolls, so centre the selected one.
  document.querySelectorAll(".filters").forEach((nav) => {
    const cur = nav.querySelector("[aria-current]");
    if (cur) nav.scrollLeft = Math.max(0, cur.offsetLeft - (nav.clientWidth - cur.offsetWidth) / 2);
  });
  // The running check is remembered per browser so its progress survives moving to another page.
  const JOB_KEY = "contraste-job";
  const job = {
    get: () => { try { return localStorage.getItem(JOB_KEY); } catch { return null; } },
    set: (id) => { try { localStorage.setItem(JOB_KEY, id); } catch {} },
    clear: () => { try { localStorage.removeItem(JOB_KEY); } catch {} },
  };
  // Tab title shows the progress, and a system notification says when a check is done.
  const baseTitle = document.title;
  const tabTitle = (prefix) => { document.title = prefix ? `(${prefix}) ${baseTitle}` : baseTitle; };
  let notifyAsked = false;
  const askToNotify = () => {
    // Asked once, and never on the click itself: the browser prompt there made the start feel stuck.
    if (notifyAsked || !("Notification" in window) || Notification.permission !== "default") return;
    notifyAsked = true;
    Notification.requestPermission().catch(() => {});
  };
  const notify = (ev, always) => {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    if (!always && document.visibilityState === "visible") return;
    const n = new Notification(`Verificación lista: ${ev.rating || "ver resultado"}`, { body: ev.title || "", tag: ev.url, icon: "/static/logo.png" });
    n.onclick = () => { window.focus(); location.assign(ev.url); n.close(); };
  };
  // --- Account: CSRF header, captcha, sign-in dialog and the draft kept while signing in ---------
  const CSRF = document.querySelector('meta[name="csrf"]')?.content || "";
  const TS = document.querySelector('meta[name="turnstile"]')?.content;
  const DRAFT_KEY = "contraste-draft";
  const draft = {
    get: () => { try { return localStorage.getItem(DRAFT_KEY); } catch { return null; } },
    set: (t) => { try { localStorage.setItem(DRAFT_KEY, t); } catch {} },
    clear: () => { try { localStorage.removeItem(DRAFT_KEY); } catch {} },
  };
  const captcha = (action) => new Promise((resolve) => {
    if (!TS || !window.turnstile) return resolve("");
    const box = document.createElement("div");
    box.className = "ts-box";
    document.body.append(box);
    const done = (t) => { resolve(t); setTimeout(() => box.remove(), 0); };
    window.turnstile.render(box, { sitekey: TS, action, appearance: "interaction-only", callback: done, "error-callback": () => done("") });
  });
  // POST as the signed-in user. The captcha is only asked for when an account is involved.
  const post = async (url, fd, withCaptcha = true, onSent) => {
    if (withCaptcha && CSRF) fd.set("cf-turnstile-response", await captcha("check"));
    if (onSent) onSent();  // the captcha is done: from here on it is the request itself
    return fetch(url, { method: "POST", body: fd, headers: CSRF ? { "X-CSRF": CSRF } : {} });
  };
  // The balance shown in the header and next to the forms follows every spend without a reload.
  const showBalance = (n) => {
    if (typeof n !== "number") return;
    document.querySelectorAll("[data-balance]").forEach((el) => { el.textContent = n; });
    document.querySelectorAll("[data-balance-word]").forEach((el) => { el.textContent = n === 1 ? "verificación" : "verificaciones"; });
    document.querySelectorAll("[data-balance-pill]").forEach((el) => {
      el.classList.toggle("empty", n === 0);
      el.setAttribute("aria-label", `Te quedan ${n} verificaciones. Ver tu cuenta`);
    });
  };
  const loginDialog = $("login-dialog");
  // Handles "needs an account" and "no credits". Returns true when it took over.
  const gate = (res, data, text) => {
    if (res.status === 401 && data.error === "login_required") {
      if (text) draft.set(text);
      if (loginDialog) loginDialog.showModal(); else location.assign("/entrar");
      return true;
    }
    if (res.status === 402) { if (text) draft.set(text); location.assign(data.next || "/cuenta?sin_creditos=1"); return true; }
    return false;
  };
  document.querySelectorAll(".login-form").forEach((f) => {
    const status = f.querySelector(".form-status");
    const say = (msg) => { status.textContent = msg; status.hidden = !msg; };
    const next = () => (draft.get() ? "/" : f.dataset.next || location.pathname);
    f.querySelector("[data-google]")?.addEventListener("click", () => {
      const consent = f.elements.consent;
      if (!consent.checked) return consent.reportValidity();
      location.assign(`/auth/google?consent=1&next=${encodeURIComponent(next())}`);
    });
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = f.querySelector('[type="submit"]'), fd = new FormData(f), label = btn.textContent;
      fd.set("next", next());
      btn.disabled = true; btn.setAttribute("aria-busy", "true"); btn.textContent = "Comprobando…"; say("");
      try {
        fd.set("cf-turnstile-response", await captcha("login"));
        btn.textContent = "Enviando…";
        const res = await fetch("/auth/magic", { method: "POST", body: fd });
        const data = await res.json().catch(() => ({}));
        say(res.ok ? `Te enviamos un enlace a ${fd.get("email")}. Ábrelo en este navegador para entrar.` : data.error || "No pudimos enviar el enlace.");
        if (res.ok) { btn.removeAttribute("aria-busy"); btn.textContent = "Enlace enviado"; return; }  // stays disabled: one send per form
      } catch { say("No hay conexión con el servidor."); }
      btn.disabled = false; btn.removeAttribute("aria-busy"); btn.textContent = label;
    });
  });

  // --- Instant navigation: a page is fetched as soon as the pointer rests on its link (or a finger
  //     touches it), so by the time of the click it is already here. Only plain pages of this site.
  // Chrome prerenders the whole page instead (speculation rules in base.html); this covers the other browsers.
  const fetched = new Set();
  const PRERENDERS = HTMLScriptElement.supports?.("speculationrules");
  const SKIP = /^\/(api|admin|auth|dev|media|static|salir)\b|\.(xml|png|webp|txt)$/;
  const prefetch = (a) => {
    const url = new URL(a.href, location.href);
    const key = url.pathname + url.search;
    if (PRERENDERS || url.origin !== location.origin || SKIP.test(url.pathname) || a.hasAttribute("download") || a.target === "_blank"
        || fetched.has(key) || key === location.pathname + location.search) return;
    fetched.add(key);
    document.head.append(Object.assign(document.createElement("link"), { rel: "prefetch", href: url.href, as: "document" }));
  };
  document.addEventListener("pointerover", (e) => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (a) setTimeout(() => { if (a.matches(":hover")) prefetch(a); }, 65);
  }, { passive: true });
  document.addEventListener("touchstart", (e) => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (a) prefetch(a);
  }, { passive: true });

  // --- Headline crawl: same reading speed however many headlines there are --------------------------
  const crawl = document.querySelector(".crawl-track");
  if (crawl) crawl.style.animationDuration = `${Math.max(30, (+crawl.dataset.n || 6) * 7)}s`;

  // --- Survey invitation: shown after some use; "Ahora no" hides it for a week in this browser -------
  const toast = $("survey-toast");
  if (toast) {
    const KEY = "contraste-survey-later";
    let later = 0;
    try { later = +localStorage.getItem(KEY) || 0; } catch {}
    if (Date.now() - later > 7 * 864e5) toast.hidden = false;
    toast.querySelector("[data-dismiss]").addEventListener("click", () => {
      toast.hidden = true;
      try { localStorage.setItem(KEY, String(Date.now())); } catch {}
    });
  }

  // --- Contribute evidence ------------------------------------------------------------------------
  const contrib = $("contrib");
  if (contrib) {
    const err = contrib.querySelector(".form-error");
    contrib.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = contrib.querySelector("button"), fd = new FormData(contrib);
      btn.disabled = true; err.hidden = true;
      try {
        const res = await post(`/api/articles/${encodeURIComponent(contrib.dataset.article)}/contributions`, fd, false);
        const data = await res.json().catch(() => ({}));
        if (gate(res, data)) return;
        if (!res.ok) { err.textContent = data.error || "No pudimos recibir el aporte."; err.hidden = false; return; }
        showBalance(data.remaining);
        contrib.replaceChildren(Object.assign(document.createElement("p"), { className: "note",
          textContent: data.status === "frozen" ? "Recibimos tu aporte. Esta verificación está recibiendo muchos aportes, así que el equipo lo revisará. Verás el resultado en tu cuenta."
                                                : "Recibimos tu aporte y lo estamos revisando. Verás el resultado en tu cuenta en unos minutos." }));
      } catch { err.textContent = "No hay conexión con el servidor."; err.hidden = false; } finally { btn.disabled = false; }
    });
  }

  const guessPayload = (text) => {
    const fd = new FormData();
    if (/^(https?:\/\/)?[\w.-]+\.[a-z]{2,}(\/\S*)?$/i.test(text) && !/\s/.test(text)) fd.append("url", text);
    else fd.append("text", text);
    return fd;
  };

  // --- "Hoy" tabs (without JS both lists are shown) -----------------------------------------
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const selectTab = (tab) => tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
    $(t.getAttribute("aria-controls")).hidden = !on;
  });
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => selectTab(t));
    t.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        selectTab(next); next.focus();
      }
    });
  });
  if (tabs.length) selectTab(tabs[0]);

  // --- Check form ----------------------------------------------------------------------------
  const form = $("checker");
  if (form) {
    const q = $("q"), file = $("file"), chip = $("chip"), go = $("go"), err = $("form-error");
    let image = null, lastInput = null;

    const grow = () => { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, window.innerHeight * 0.4) + "px"; };
    q.addEventListener("input", grow);
    q.addEventListener("focus", () => setTimeout(() => q.scrollIntoView({ block: "nearest", behavior: "smooth" }), 300));

    const showError = (msg) => { err.textContent = msg; err.hidden = !msg; };
    const setImage = (f) => {
      if (!f) { image = null; chip.classList.remove("on"); form.classList.remove("has-image"); file.value = ""; $("img-note").hidden = true; return; }
      if (!/^image\/(jpeg|png|webp|gif)$/.test(f.type)) return showError("Usa una imagen JPG, PNG, WEBP o GIF.");
      if (f.size > 10 * 1024 * 1024) return showError("La imagen pesa más de 10 MB.");
      image = f; showError("");
      $("chip-name").textContent = f.name || "Imagen pegada";
      chip.classList.add("on"); form.classList.add("has-image"); $("img-note").hidden = false;
    };
    $("attach").addEventListener("click", () => file.click());
    file.addEventListener("change", () => setImage(file.files[0]));
    $("chip-x").addEventListener("click", () => setImage(null));
    document.addEventListener("paste", (e) => {
      const it = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
      if (it) { e.preventDefault(); setImage(it.getAsFile()); }
    });
    ["dragenter", "dragover"].forEach((t) => form.addEventListener(t, (e) => { e.preventDefault(); form.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((t) => form.addEventListener(t, (e) => { e.preventDefault(); form.classList.remove("drag"); }));
    form.addEventListener("drop", (e) => setImage(e.dataTransfer.files[0]));
    $("example").addEventListener("click", (e) => { q.value = e.currentTarget.dataset.text; grow(); setImage(null); form.requestSubmit(); });
    q.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) form.requestSubmit(); });

    const submit = async (payload) => {
      showError("");
      const goLabel = go.textContent;
      go.disabled = true; go.setAttribute("aria-busy", "true"); go.textContent = "Comprobando…";
      try {
        const res = await post("/api/checks", payload, true, () => {
          go.textContent = "Verificando…"; showPanel(payload.get("image") ? "Recibiendo tu imagen" : "Recibiendo el contenido");
        });
        const data = await res.json().catch(() => ({}));
        if (data.url || !res.ok) hidePanel();
        if (gate(res, data, image ? "" : q.value.trim())) return;
        if (!res.ok) return showError(data.error || "No pudimos iniciar la verificación. Intenta de nuevo.");
        draft.clear();
        showBalance(data.remaining);
        if (data.url) return location.assign(data.url);
        history.replaceState(null, "", "/?verificacion=" + encodeURIComponent(data.id));
        job.set(data.id);
        follow(data.id);
      } catch {
        hidePanel();
        showError("No hay conexión con el servidor. Revisa tu internet e intenta de nuevo.");
      } finally {
        go.disabled = false; go.removeAttribute("aria-busy"); go.textContent = goLabel;
      }
    };

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = q.value.trim(), fd = new FormData();
      if (image) fd.append("image", image);
      else if (!text) return showError("Pega un enlace, escribe una afirmación o adjunta una imagen.");
      const payload = image ? fd : guessPayload(text);
      lastInput = payload;
      submit(payload);
    });
    // Back from signing in: the text written before is waiting.
    const saved = draft.get();
    if (saved && !q.value) { q.value = saved; grow(); if (CSRF) { draft.clear(); form.requestSubmit(); } }
    $("retry").addEventListener("click", () => { $("progress").hidden = true; if (lastInput) submit(lastInput); else q.focus(); });

    // --- Progress: stages, claims, queries and live source tiles ----------------------------------
    const STAGES = [[0, 32], [32, 35], [35, 60], [60, 100]];
    const STATE = { reading: "leyendo", ok: "leída", omitted: "omitida", assessed: "contrastada" };
    const STANCE = { confirma: "confirma", contradice: "contradice", contexto: "aporta contexto", no_relacionada: "no relacionada" };
    let timers = [], since = 0;
    const tick = (t0) => {
      const s = Math.floor((Date.now() - t0) / 1000);
      $("clock").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    };
    // Before the server answers (duplicate search, account checks) the panel is already live, so pressing
    // the button visibly starts something.
    const showPanel = (label) => {
      timers.forEach(clearInterval); timers = [];
      since = Date.now();
      const box = $("progress"), now = $("now"), eta = $("eta");
      box.hidden = false; $("job-error").hidden = true; $("found").hidden = true; $("srcbox").hidden = true; $("ticker").hidden = true;
      $("claims").replaceChildren(); $("tiles").replaceChildren();
      document.querySelectorAll("#stages .stage").forEach((el, i) => {
        el.classList.remove("done"); el.classList.toggle("now", i === 0); el.querySelector(".fill").style.width = "0";
      });
      now.textContent = label; eta.textContent = "Iniciando…";
      tick(since);
      const steps = ["Revisando si ya lo verificamos", "Preparando la investigación"];
      let n = 0;
      timers.push(setInterval(() => tick(since), 400));
      timers.push(setInterval(() => { if (n < steps.length) now.textContent = steps[n++]; }, 1500));
      box.scrollIntoView({ block: "start", behavior: "smooth" });
      box.focus({ preventScroll: true });
    };
    const hidePanel = () => { timers.forEach(clearInterval); timers = []; since = 0; $("progress").hidden = true; };
    const follow = (id, resumed = false) => {
      timers.forEach(clearInterval); timers = [];
      const box = $("progress"), now = $("now"), eta = $("eta"), tiles = $("tiles"), ticker = $("ticker");
      const stages = [...document.querySelectorAll("#stages .stage")];
      const sources = new Map();
      let real = 0, shown = 0, queries = [], qi = 0, finished = false;
      box.hidden = false; $("job-error").hidden = true; $("found").hidden = true; $("srcbox").hidden = true; ticker.hidden = true;
      $("claims").replaceChildren(); tiles.replaceChildren();
      now.textContent = "Empezando la investigación"; eta.textContent = "Calculando tiempo…";
      if (!since) box.scrollIntoView({ block: "start", behavior: "smooth" });
      // Move the keyboard/screen-reader focus to the live progress, so it is obvious the check started.
      if (!resumed) { box.focus({ preventScroll: true }); setTimeout(askToNotify, 3000); }

      const paint = (p) => stages.forEach((el, i) => {
        const [a, b] = STAGES[i];
        el.classList.toggle("done", p >= b);
        el.classList.toggle("now", p >= a && p < b && !finished);
        el.querySelector(".fill").style.width = p >= b ? "100%" : p <= a ? "0" : Math.round(((p - a) / (b - a)) * 100) + "%";
        if (p >= a && p < b) el.setAttribute("aria-current", "step"); else el.removeAttribute("aria-current");
      });
      // Creep the bar forward inside the current stage, never past its end.
      const t0 = since || Date.now();
      since = 0;
      timers.push(setInterval(() => {
        tick(t0);
        if (finished) return;
        const cap = (STAGES.find(([a, b]) => real >= a && real < b) || [0, 100])[1] - 0.5;
        shown = Math.max(real, shown + (cap - shown) * 0.03);
        paint(shown);
      }, 400));
      timers.push(setInterval(() => {
        if (!queries.length || real >= 60 || finished) { ticker.hidden = true; return; }
        ticker.hidden = false;
        const b = document.createElement("b"); b.textContent = `«${queries[qi++ % queries.length]}»`;
        ticker.replaceChildren("Buscando ", b);
      }, 1600));

      const counts = () => {
        const v = [...sources.values()], n = (s) => v.filter((x) => x.state === s).length;
        const read = v.length - n("reading");
        const pl = (k, one, many) => `${k} ${k === 1 ? one : many}`;
        $("counts").textContent = [pl(v.length, "fuente", "fuentes"), pl(read - n("omitted"), "leída", "leídas"),
          pl(n("omitted"), "omitida", "omitidas"), pl(n("assessed"), "contrastada", "contrastadas")].join(" · ");
      };
      const upsert = (src) => {
        let li = sources.get(src.id)?.el;
        if (!li) {
          li = document.createElement("li");
          li.append(Object.assign(document.createElement("span"), { className: "dot" }), document.createTextNode(src.name));
          tiles.append(li);
        }
        const prev = sources.get(src.id) || {};
        if (prev.state === "assessed" && src.state !== "assessed") return;
        sources.set(src.id, { ...prev, ...src, el: li });
        li.className = `tile ${src.state}${src.stance ? " st-" + src.stance : ""}${src.kind === "datos" || prev.kind === "datos" ? " datos" : ""}`;
        li.title = src.state === "assessed" ? `${src.name}: ${STANCE[src.stance] || ""}` : src.reason ? `${src.name}: ${src.reason}` : `${src.name}: ${STATE[src.state]}`;
        $("srcbox").hidden = false; counts();
      };

      const es = new EventSource(`/api/checks/${encodeURIComponent(id)}/events`);
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data);
        if (ev.type === "queued") {
          now.textContent = ev.position === 1 ? "En cola: eres la siguiente verificación" : `En cola · posición ${ev.position}`;
          eta.textContent = "Empieza apenas se libere un cupo.";
          tabTitle(`En cola ${ev.position}`);
        } else if (ev.type === "step") {
          real = Math.max(real, ev.progress);
          tabTitle(`${ev.progress} %`);
          if (ev.claims?.length) {
            $("claims").replaceChildren(...ev.claims.map((c) => Object.assign(document.createElement("li"), { textContent: c })));
            $("found").hidden = false;
          }
          if (ev.queries?.length) queries = ev.queries;
          if (ev.source) upsert(ev.source);
          now.textContent = ev.label;
          const secs = (Date.now() - t0) / 1000;
          if (real > 8 && secs > 3) {
            const left = Math.max(5, Math.round((secs * (100 - real)) / real));
            eta.textContent = left < 60 ? `Faltan unos ${Math.ceil(left / 5) * 5} segundos` : `Faltan unos ${Math.round(left / 60)} minutos`;
          }
        } else if (ev.type === "done") {
          es.close(); finished = true; real = shown = 100; paint(100); now.textContent = "Listo"; eta.textContent = "";
          job.clear();
          tabTitle("Listo");
          notify(ev, false);
          if (!resumed) return location.assign(ev.url);
          // Coming back to a check that finished meanwhile: offer the result instead of jumping to it.
          const a = Object.assign(document.createElement("a"), { href: ev.url, textContent: "Ver el resultado" });
          now.replaceChildren("Listo · ", a);
        } else if (ev.type === "error") {
          es.close(); finished = true; paint(shown);
          job.clear();
          $("job-error-msg").textContent = ev.message;
          $("job-error").hidden = false; eta.textContent = ""; ticker.hidden = true;
        }
      };
      es.onopen = () => { if (!finished && real < 100) eta.textContent = "Conectado. Recibiendo avances…"; };
      es.onerror = () => {
        if (finished) return;
        eta.textContent = es.readyState === EventSource.CLOSED
          ? "Se perdió la conexión. Recarga la página." : "Reconectando…";
      };
    };
    const fromUrl = new URLSearchParams(location.search).get("verificacion");
    const pending = fromUrl || job.get();
    if (pending && /^[a-z0-9]{6}$/.test(pending)) follow(pending, !fromUrl);
  }

  // --- Article: card, sharing and read beacon -------------------------------------------------
  const art = $("article");
  if (art) {
    const id = art.dataset.id, url = art.dataset.url, title = art.dataset.title;
    const img = $("card-img"), dl = $("download"), share = $("share");
    const names = { post: "publicacion", story: "historia" };
    let fmt = "post";
    document.querySelectorAll('input[name="fmt"]').forEach((r) => r.addEventListener("change", () => {
      fmt = r.value;
      img.src = `/api/cards/${id}/${fmt}.webp?v=${new URL(img.src, location.href).searchParams.get("v") || ""}`;
      img.width = 1080; img.height = fmt === "story" ? 1920 : 1350;
      share.classList.toggle("story", fmt === "story");
      dl.href = img.src; dl.download = `contraste-${id}-${names[fmt]}.png`;
    }));

    const native = $("share-native");
    if (navigator.share) {
      native.hidden = false;
      native.addEventListener("click", async () => {
        try {
          const blob = await (await fetch(`/api/cards/${id}/${fmt}.png`)).blob();
          const f = new File([blob], `contraste-${id}.png`, { type: "image/png" });
          const data = navigator.canShare?.({ files: [f] }) ? { files: [f], text: `${title} ${url}` } : { title, url };
          await navigator.share(data);
        } catch (e) { /* share sheet dismissed */ }
      });
    }
    $("copy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(url); $("copy-status").textContent = "Enlace copiado."; }
      catch { $("copy-status").textContent = url; }
    });

    // A read counts after 5 s with the page visible. No cookies.
    let visible = 0, since = document.visibilityState === "visible" ? Date.now() : null, sent = false;
    const tick = () => {
      if (sent) return;
      const total = visible + (since ? Date.now() - since : 0);
      if (total >= 5000) {
        sent = true;
        navigator.sendBeacon(`/api/views/${id}`, new Blob([JSON.stringify({ visible_ms: total })], { type: "text/plain" }));
      }
    };
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") since = Date.now();
      else if (since) { visible += Date.now() - since; since = null; }
    });
    setInterval(tick, 1000);
  }

  // --- Running check indicator (every page but the front page) and the header check box ------
  let tracker = null;
  const track = (id) => {
    tracker?.close();
    document.querySelector(".job-pill")?.remove();
    const pill = document.createElement("a");
    pill.className = "job-pill";
    pill.href = "/?verificacion=" + id;
    const dot = Object.assign(document.createElement("span"), { className: "dot" });
    const text = Object.assign(document.createElement("span"), { textContent: "Verificando…" });
    pill.append(dot, text);
    pill.setAttribute("aria-live", "polite");
    document.querySelector(".site-head .wrap")?.insertBefore(pill, document.querySelector(".site-head .nav"));
    const narrow = matchMedia("(max-width: 719px)");
    const say = (full, short) => { text.textContent = narrow.matches ? short : full; pill.setAttribute("aria-label", full); };
    const es = tracker = new EventSource(`/api/checks/${encodeURIComponent(id)}/events`);
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.type === "queued") { say(`En cola · posición ${ev.position}`, `Cola ${ev.position}`); tabTitle(`En cola ${ev.position}`); }
      else if (ev.type === "step") { say(`Verificando · ${ev.progress} %`, `${ev.progress} %`); tabTitle(`${ev.progress} %`); }
      else if (ev.type === "done") {
        es.close(); pill.href = ev.url; pill.classList.add("ready"); say("Listo: ver resultado", "Ver resultado");
        tabTitle("Listo"); notify(ev, true);
        pill.addEventListener("click", job.clear);
      } else if (ev.type === "error") {
        es.close(); pill.classList.add("failed"); say(`No se pudo verificar: ${ev.message}`, "Error");
        pill.title = ev.message; tabTitle("Error");
        pill.href = "/"; pill.addEventListener("click", job.clear);
      }
    };
  };
  const running = job.get();
  if (!form && running && /^[a-z0-9]{6}$/.test(running)) track(running);

  const head = $("head-check");
  if (head) {
    const input = $("hq");
    input.addEventListener("input", () => input.setCustomValidity(""));
    head.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      const fail = (msg) => { input.setCustomValidity(msg); input.reportValidity(); };
      if (!text) return fail("Pega un enlace o escribe lo que quieres verificar.");
      const btn = head.querySelector("button");
      btn.disabled = true;
      try {
        const res = await post("/api/checks", guessPayload(text));
        const data = await res.json().catch(() => ({}));
        if (gate(res, data, text)) return;
        if (!res.ok) return fail(data.error || "No pudimos iniciar la verificación.");
        showBalance(data.remaining);
        if (data.url) return location.assign(data.url);  // already checked: the result exists
        job.set(data.id); input.value = ""; track(data.id);
      } catch {
        fail("No hay conexión con el servidor.");
      } finally {
        btn.disabled = false;
      }
    });
  }
})();

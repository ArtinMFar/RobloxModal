import { DEFAULT_RELAY } from "./config.js";

// The VNC viewer (noVNC) is about 50 files; it loads when a session starts, so the rest of the
// page works straight away and keeps working if the viewer cannot load.
let novnc = null;
async function loadViewer() {
  if (!novnc) {
    const [rfb, defs, table] = await Promise.all([
      import("./vendor/novnc/core/rfb.js"),
      import("./vendor/novnc/core/input/keysymdef.js"),
      import("./vendor/novnc/core/input/keysym.js"),
    ]);
    novnc = { RFB: rfb.default, keysyms: defs.default, KeyTable: table.default };
  }
  return novnc;
}

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Modal's list prices per hour (October 2026); the relay asks for 4 GiB + 0.5 GiB per core.
const RATE_CPU = 0.0473;
const RATE_GIB = 0.008;
const memoryGiB = (cpu) => Math.min(32, 4 + 0.5 * cpu);
const hourlyCost = (cpu) => cpu * RATE_CPU + memoryGiB(cpu) * RATE_GIB;

// ---------------------------------------------------------------------------------- settings

const SETTINGS_KEY = "robloxmodal.settings.v1";
const SESSION_KEY = "robloxmodal.session.v1";

const defaults = () => ({
  mode: matchMedia("(pointer: coarse)").matches ? "mobile" : "pc",
  cpu: 8,
  tokenId: "",
  tokenSecret: "",
  idleMinutes: 10,
  maxHours: 3,
  quality: 6,
  relay: DEFAULT_RELAY,
});

// `store` is "localStorage" or "sessionStorage". A browser that blocks site data can throw on
// merely reading window.localStorage, so even that happens inside the try: without storage,
// settings last for this page only.
function readStore(store, key) {
  try {
    return JSON.parse(window[store].getItem(key) || "null");
  } catch {
    return null;
  }
}
function writeStore(store, key, value) {
  try {
    if (value === null) window[store].removeItem(key);
    else window[store].setItem(key, JSON.stringify(value));
  } catch {
    /* storage blocked */
  }
}

let settings = { ...defaults(), ...(readStore("localStorage", SETTINGS_KEY) || {}) };
const haveToken = () => settings.tokenId.trim() && settings.tokenSecret.trim();
const creds = () => ({ token_id: settings.tokenId.trim(), token_secret: settings.tokenSecret.trim() });

// ---------------------------------------------------------------------------------- relay

async function relay(path, body, timeoutMs = 60000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const base = (settings.relay || DEFAULT_RELAY).replace(/\/+$/, "");
  try {
    const r = await fetch(base + path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = data.detail;
      throw new Error(typeof d === "string" ? d : d ? JSON.stringify(d) : `The relay answered ${r.status}.`);
    }
    return data;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("The relay did not answer in time.");
    if (e instanceof TypeError) throw new Error(`Could not reach the relay (${base}).`);
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function sessionFetch(session, path, opts = {}, timeoutMs = 8000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(`${session.url}${path}?k=${encodeURIComponent(session.key)}`, {
      ...opts,
      signal: ctrl.signal,
      cache: "no-store",
    });
    if (!r.ok) throw new Error(`session answered ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------------- views

function showView(name) {
  $("home").hidden = name !== "home";
  $("launch").hidden = name !== "launch";
  $("player").hidden = name !== "player";
}

function homeMsg(text, kind = "") {
  const el = $("home-msg");
  el.textContent = text;
  el.className = `msg ${kind}`;
}

function renderSummary() {
  const chips = [
    settings.mode === "mobile" ? "Mobile controls" : "PC controls",
    `${settings.cpu} CPU cores`,
    `about $${hourlyCost(settings.cpu).toFixed(2)}/hour`,
  ];
  const el = $("summary");
  el.replaceChildren(
    ...chips.map((t) => Object.assign(document.createElement("span"), { className: "chip", textContent: t }))
  );
  if (!haveToken()) {
    el.append(Object.assign(document.createElement("span"), { className: "chip bad", textContent: "No Modal token yet: open Settings" }));
  }
}

async function refreshHome() {
  renderSummary();
  $("running-card").hidden = true;
  if (!haveToken()) return;
  try {
    const { sessions } = await relay("/api/sessions", creds(), 30000);
    if (sessions.length) {
      const s = sessions[0];
      const mins = Math.max(1, Math.round((Date.now() / 1000 - s.started) / 60));
      $("running-text").textContent =
        `${s.mode === "mobile" ? "Mobile" : "PC"} controls, ${s.cpu} cores, started ${mins} min ago. It is billing your Modal account.`;
      $("running-card").hidden = false;
      $("reconnect").onclick = () => enterPlayer(s);
    }
  } catch (e) {
    homeMsg(e.message, "error");
  }
}

// ---------------------------------------------------------------------------------- settings dialog

function openSettings(message = "") {
  const f = $("settings-form");
  f.mode.value = settings.mode;
  $("cpu").value = settings.cpu;
  $("token-id").value = settings.tokenId;
  $("token-secret").value = settings.tokenSecret;
  $("token-secret").type = "password";
  $("show-secret").textContent = "Show";
  $("idle").value = settings.idleMinutes;
  $("max-hours").value = settings.maxHours;
  $("quality").value = settings.quality;
  $("relay").value = settings.relay;
  updateCpu();
  const msg = $("settings-msg");
  msg.textContent = message;
  msg.className = message ? "msg error" : "msg";
  $("settings").showModal();
}

function updateCpu() {
  const cpu = Number($("cpu").value);
  $("cpu-out").textContent = `${cpu} cores`;
  $("cost").textContent =
    `About $${hourlyCost(cpu).toFixed(2)} per hour while running (${cpu} cores and ${memoryGiB(cpu)} GiB at Modal's list prices), ` +
    "plus a few cents per hour for the video stream.";
}

function saveSettingsFromForm(event) {
  event.preventDefault();
  const tokenId = $("token-id").value.trim();
  const tokenSecret = $("token-secret").value.trim();
  const msg = $("settings-msg");
  if (tokenId && !tokenId.startsWith("ak-")) return void ((msg.textContent = "The token ID starts with ak-."), (msg.className = "msg error"));
  if (tokenSecret && !tokenSecret.startsWith("as-")) return void ((msg.textContent = "The token secret starts with as-."), (msg.className = "msg error"));
  const num = (id, lo, hi, fallback) => {
    const v = Number($(id).value);
    return Number.isFinite(v) && v >= lo && v <= hi ? v : fallback;
  };
  settings = {
    mode: $("settings-form").mode.value === "mobile" ? "mobile" : "pc",
    cpu: num("cpu", 2, 32, 8),
    tokenId,
    tokenSecret,
    idleMinutes: num("idle", 2, 120, 10),
    maxHours: num("max-hours", 0.5, 24, 3),
    quality: Math.round(num("quality", 0, 9, 6)),
    relay: $("relay").value.trim() || DEFAULT_RELAY,
  };
  writeStore("localStorage", SETTINGS_KEY, settings);
  $("settings").close();
  homeMsg("Settings saved.", "ok");
  refreshHome();
}

// ---------------------------------------------------------------------------------- launch

let launchCtrl = null;

function sessionSize(mode) {
  if (mode !== "mobile") return [1280, 720];
  // Match the phone's landscape shape so the picture fills the screen.
  const long = Math.max(screen.width, screen.height);
  const short = Math.min(screen.width, screen.height);
  const h = Math.round((1280 * short) / long / 2) * 2;
  return [1280, Math.min(720, Math.max(480, h))];
}

async function launchSession() {
  if (!haveToken()) return openSettings("Enter your Modal token ID and secret first.");
  homeMsg("");
  showView("launch");
  $("launch-title").textContent = "Starting";
  $("launch-text").textContent = "Contacting the relay";
  $("launch-log").textContent = "";
  $("launch-log-wrap").hidden = true;
  const ctrl = { cancelled: false, job: null };
  launchCtrl = ctrl;
  const started = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    $("launch-time").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} elapsed`;
  }, 1000);
  try {
    const [width, height] = sessionSize(settings.mode);
    const { job } = await relay("/api/launch", {
      ...creds(),
      cpu: settings.cpu,
      mode: settings.mode,
      idle_minutes: settings.idleMinutes,
      max_hours: settings.maxHours,
      width,
      height,
    });
    ctrl.job = job;
    while (!ctrl.cancelled) {
      const s = await relay("/api/job", { job, token_id: creds().token_id });
      if (ctrl.cancelled) return;
      if (s.state === "running") $("launch-text").textContent = s.progress;
      else if (s.state === "error") throw new Error(s.error);
      else return void enterPlayer(s.session);
      await sleep(2500);
    }
  } catch (e) {
    if (!ctrl.cancelled) {
      showView("home");
      homeMsg(e.message, "error");
      refreshHome();
    }
  } finally {
    clearInterval(tick);
    if (launchCtrl === ctrl) launchCtrl = null;
  }
}

async function cancelLaunch() {
  const ctrl = launchCtrl;
  if (ctrl) ctrl.cancelled = true;
  showView("home");
  homeMsg("Stopping…");
  // A launch already handed to Modal finishes in the background; wait for it, then stop it.
  if (ctrl?.job) {
    for (let i = 0; i < 400; i++) {
      try {
        const s = await relay("/api/job", { job: ctrl.job, token_id: creds().token_id });
        if (s.state !== "running") break;
      } catch {
        break;
      }
      await sleep(3000);
    }
  }
  await stopEverything();
}

// ---------------------------------------------------------------------------------- player

let current = null;

function overlay(title, text, { spinner = true, restart = false } = {}) {
  $("player-overlay").hidden = false;
  $("overlay-title").textContent = title;
  $("overlay-text").textContent = text;
  $("overlay-spinner").hidden = !spinner;
  $("restart").hidden = !restart;
}
const hideOverlay = () => ($("player-overlay").hidden = true);

function enterPlayer(session) {
  leavePlayer({ keepView: true });
  writeStore("sessionStorage", SESSION_KEY, session);
  const mode = session.mode === "mobile" ? "mobile" : "pc";
  current = { session, mode, rfb: null, touch: null, failures: 0, alive: true, phase: "", connected: false, blankRetries: 0 };
  $("player").classList.toggle("mobile", mode === "mobile");
  $("touch-layer").hidden = mode !== "mobile";
  $("tb-items").hidden = true;
  showView("player");
  overlay("Starting Roblox", "Waiting for the session");
  loadViewer().catch(() => {}); // start fetching it now; statusLoop reports a failure
  statusLoop(current);
}

function leavePlayer({ keepView = false } = {}) {
  if (current) {
    current.alive = false;
    try {
      current.rfb?.disconnect();
    } catch {
      /* already closed */
    }
    current.touch?.close();
  }
  current = null;
  $("screen").replaceChildren();
  if (!keepView) showView("home");
}

async function statusLoop(c) {
  while (c.alive) {
    let st;
    try {
      st = await sessionFetch(c.session, "/status");
      c.failures = 0;
    } catch {
      if (++c.failures >= 4) {
        if (!c.alive) return;
        writeStore("sessionStorage", SESSION_KEY, null);
        leavePlayer();
        homeMsg("The session has ended.", "error");
        refreshHome();
        return;
      }
      await sleep(2000);
      continue;
    }
    if (!c.alive) return;
    const phase = st.phase;
    c.phase = phase;
    if (phase === "exited") {
      overlay("Roblox stopped", st.message, { spinner: false, restart: true });
    } else if (phase === "stopping") {
      overlay("Session ending", st.message, { spinner: false });
    } else if (phase === "loading") {
      overlay("Roblox is loading", "This takes 10-60 seconds, depending on the cores");
    } else if (phase === "ready") {
      // Only now: a viewer that connects before Roblox's first screen is drawn can be left
      // looking at the empty window until something else on screen changes.
      if (!c.rfb) {
        try {
          await loadViewer();
        } catch (e) {
          overlay("Could not load the viewer", `${e.message} Reload the page to try again.`, { spinner: false });
          await sleep(4000);
          continue;
        }
        if (c.alive && !c.rfb) connectVnc(c);
      } else if (c.connected) hideOverlay();
    } else {
      overlay(phase === "downloading" ? "Downloading Roblox" : "Starting Roblox", st.message);
    }
    await sleep(c.rfb ? 4000 : 1500);
  }
}

function connectVnc(c) {
  const url = `${c.session.url.replace(/^http/, "ws")}/vnc?k=${encodeURIComponent(c.session.key)}`;
  const rfb = new novnc.RFB($("screen"), url, { wsProtocols: ["binary"] });
  c.rfb = rfb;
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.qualityLevel = settings.quality;
  rfb.compressionLevel = 2;
  rfb.showDotCursor = c.mode === "pc";
  rfb.focusOnClick = c.mode === "pc";
  rfb.background = "#000";
  rfb.addEventListener("connect", () => {
    c.connected = true;
    hideOverlay();
    setTimeout(() => recheckBlank(c, rfb), 4000);
    if (c.mode === "pc") rfb.focus();
    if (c.mode === "mobile") connectTouch(c);
  });
  rfb.addEventListener("disconnect", () => {
    c.connected = false;
    if (c.rfb === rfb) c.rfb = null;
    if (!c.alive) return;
    overlay("Reconnecting", "The picture dropped; reconnecting");
    // statusLoop reconnects on its next pass while Roblox is up.
  });
}

// Roblox draws its first screen and then sits idle, and a viewer that connected around then can
// be left with the empty window it saw first. A flat picture a few seconds in means that: connect
// again (twice at most), which takes a fresh copy of the screen.
function recheckBlank(c, rfb) {
  if (!c.alive || c.rfb !== rfb || !c.connected || c.blankRetries >= 2) return;
  const canvas = $("screen").querySelector("canvas");
  if (!canvas || !canvas.width) return;
  let lo = 255;
  let hi = 0;
  try {
    const px = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    for (let i = 0; i < px.length; i += 4 * 997) {
      const v = (px[i] + px[i + 1] + px[i + 2]) / 3;
      lo = Math.min(lo, v);
      hi = Math.max(hi, v);
    }
  } catch {
    return;
  }
  if (hi - lo < 12) {
    c.blankRetries += 1;
    rfb.disconnect(); // statusLoop connects again
  }
}

// ---------------------------------------------------------------------------------- touch (mobile)

function connectTouch(c) {
  if (c.touch && c.touch.readyState <= 1) return;
  const ws = new WebSocket(`${c.session.url.replace(/^http/, "ws")}/touch?k=${encodeURIComponent(c.session.key)}`);
  c.touch = ws;
  ws.onclose = () => {
    if (c.touch === ws) c.touch = null;
  };
}

function screenPoint(e) {
  const canvas = $("screen").querySelector("canvas");
  if (!canvas || !current) return null;
  const r = canvas.getBoundingClientRect();
  const w = canvas.width || 1280;
  const h = canvas.height || 720;
  const x = ((e.clientX - r.left) / r.width) * w;
  const y = ((e.clientY - r.top) / r.height) * h;
  return [Math.max(0, Math.min(w - 1, x)).toFixed(1), Math.max(0, Math.min(h - 1, y)).toFixed(1)];
}

function sendTouch(verb, e) {
  const ws = current?.touch;
  const p = screenPoint(e);
  if (!ws || ws.readyState !== 1 || !p) return;
  ws.send(`${verb} ${e.pointerId} ${p[0]} ${p[1]}`);
}

function setupTouchLayer() {
  const layer = $("touch-layer");
  const pending = new Map();
  let frame = 0;
  layer.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    layer.setPointerCapture(e.pointerId);
    sendTouch("d", e);
  });
  layer.addEventListener("pointermove", (e) => {
    if (!layer.hasPointerCapture(e.pointerId)) return;
    pending.set(e.pointerId, e);
    if (!frame) {
      frame = requestAnimationFrame(() => {
        frame = 0;
        for (const ev of pending.values()) sendTouch("m", ev);
        pending.clear();
      });
    }
  });
  const up = (e) => {
    pending.delete(e.pointerId);
    sendTouch("u", e);
  };
  layer.addEventListener("pointerup", up);
  layer.addEventListener("pointercancel", up);
  layer.addEventListener("contextmenu", (e) => e.preventDefault());
}

// ---------------------------------------------------------------------------------- keyboard

// Phones have no physical keyboard; this hidden box raises the on-screen one and sends what is
// typed as key presses. A single space sits in it so a backspace has something to delete.
function setupKeyboard() {
  const box = $("kbd-input");
  const reset = () => {
    box.value = " ";
    box.setSelectionRange(1, 1);
  };
  const press = (keysym) => current?.rfb?.sendKey(keysym, null);
  const flush = () => {
    const v = box.value;
    if (!novnc) return reset();
    if (v === "") press(novnc.KeyTable.XK_BackSpace);
    else for (const ch of v.slice(1)) press(novnc.keysyms.lookup(ch.codePointAt(0)));
    reset();
  };
  box.addEventListener("input", (e) => {
    if (!e.isComposing) flush();
  });
  box.addEventListener("compositionend", flush);
  box.addEventListener("keydown", (e) => {
    if (!novnc) return;
    const K = novnc.KeyTable;
    const special = { Enter: K.XK_Return, Tab: K.XK_Tab, Escape: K.XK_Escape,
      ArrowLeft: K.XK_Left, ArrowRight: K.XK_Right, ArrowUp: K.XK_Up, ArrowDown: K.XK_Down };
    if (special[e.key]) {
      e.preventDefault();
      press(special[e.key]);
    }
  });
  $("tb-keyboard").addEventListener("click", () => {
    reset();
    box.focus({ preventScroll: true });
    $("tb-items").hidden = true;
  });
}

// ---------------------------------------------------------------------------------- stop

async function stopEverything() {
  const session = current?.session || readStore("sessionStorage", SESSION_KEY);
  leavePlayer();
  homeMsg("Stopping…");
  $("running-card").hidden = true;
  let shutDown = false;
  if (session) {
    try {
      await sessionFetch(session, "/shutdown", { method: "POST" }, 5000);
      shutDown = true;
    } catch {
      /* already gone, or unreachable: the relay stops it below */
    }
  }
  writeStore("sessionStorage", SESSION_KEY, null);
  if (!haveToken()) {
    homeMsg(shutDown ? "Stopped." : "Could not stop it: no Modal token in Settings.", shutDown ? "ok" : "error");
    return;
  }
  try {
    await relay("/api/stop", creds());
    homeMsg("Stopped. Nothing from this site is running on your Modal account now.", "ok");
  } catch (e) {
    homeMsg(
      shutDown
        ? "The session was told to stop, but the relay could not confirm it. Check modal.com/apps."
        : `Could not confirm the stop: ${e.message} Check modal.com/apps.`,
      "error"
    );
  }
  refreshHome();
}

// ---------------------------------------------------------------------------------- wiring

function toggleFullscreen() {
  if (document.fullscreenElement) return void document.exitFullscreen?.();
  const el = document.documentElement;
  (el.requestFullscreen?.() || el.webkitRequestFullscreen?.() || Promise.resolve())
    .then(() => current?.mode === "mobile" && screen.orientation?.lock?.("landscape"))
    .catch(() => {});
}

function init() {
  $("open-settings").addEventListener("click", () => openSettings());
  $("settings-form").addEventListener("submit", saveSettingsFromForm);
  $("settings-cancel").addEventListener("click", () => $("settings").close());
  $("cpu").addEventListener("input", updateCpu);
  $("show-secret").addEventListener("click", () => {
    const input = $("token-secret");
    input.type = input.type === "password" ? "text" : "password";
    $("show-secret").textContent = input.type === "password" ? "Show" : "Hide";
  });
  $("forget").addEventListener("click", () => {
    writeStore("localStorage", SETTINGS_KEY, null);
    settings = defaults();
    $("settings").close();
    homeMsg("Saved settings and token removed from this browser.", "ok");
    refreshHome();
  });

  $("play").addEventListener("click", launchSession);
  $("stop-home").addEventListener("click", stopEverything);
  $("stop-running").addEventListener("click", stopEverything);
  $("cancel-launch").addEventListener("click", cancelLaunch);
  $("overlay-stop").addEventListener("click", stopEverything);
  $("tb-stop").addEventListener("click", stopEverything);
  $("tb-toggle").addEventListener("click", () => ($("tb-items").hidden = !$("tb-items").hidden));
  $("tb-fullscreen").addEventListener("click", () => {
    toggleFullscreen();
    $("tb-items").hidden = true;
  });
  $("tb-leave").addEventListener("click", () => {
    leavePlayer();
    homeMsg("The session is still running. Reconnect, or stop it so it stops billing.");
    refreshHome();
  });
  const restart = async () => {
    if (!current) return;
    $("tb-items").hidden = true;
    overlay("Restarting Roblox", "");
    try {
      current.rfb?.disconnect();
      await sessionFetch(current.session, "/restart", { method: "POST" });
    } catch {
      /* statusLoop reports what happened */
    }
  };
  $("restart").addEventListener("click", restart);
  $("tb-restart").addEventListener("click", restart);

  setupTouchLayer();
  setupKeyboard();

  showView("home");
  const saved = readStore("sessionStorage", SESSION_KEY);
  if (saved?.url) {
    enterPlayer(saved); // a reload while playing: pick the same session back up
  } else {
    refreshHome();
  }
  if (!haveToken() && !saved) openSettings();
}

init();

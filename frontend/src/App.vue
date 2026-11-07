<script setup>
import { computed, ref, onMounted, onUnmounted } from "vue";
const state = ref(null),
  events = ref([]),
  error = ref(""),
  notice = ref(""),
  busy = ref(false);
const text = ref(""),
  autoPaste = ref(true),
  viewer = ref(null),
  generation = ref(0),
  connected = ref(false);
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get("token") || "",
  password = fragment.get("password") || "";
const authenticated = ref(false);
const now = ref(Date.now()), lastStatusAt = ref(0);
let timer,
  clockTimer,
  refreshing = false,
  lastInteraction = 0;
const viewerUrl = computed(
  () =>
    "/novnc/vnc.html#" +
    new URLSearchParams({
      autoconnect: "1",
      resize: "scale",
      password,
      path: "novnc/websockify",
      show_dot: "1",
    }),
);
const capture = computed(() => state.value?.capture);
const statusAge = computed(() => Math.max(0, (now.value - lastStatusAt.value) / 1000));
const fresh = computed(() => capture.value && capture.value.age + statusAge.value < 20);
const speechFresh = computed(() => state.value?.speech && state.value.speech.age + statusAge.value < 20);
const recording = computed(
  () =>
    fresh.value &&
    capture.value.audio?.running &&
    capture.value.screen?.running,
);
const elapsed = computed(() => {
  const seconds = (capture.value?.elapsed || 0) + (capture.value ? Math.min(statusAge.value, 20) : 0);
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
});
const disk = computed(() =>
  capture.value?.free_bytes == null
    ? "—"
    : (capture.value.free_bytes / 1073741824).toFixed(1) + " GB",
);
const control = computed(() => state.value?.control === true && statusAge.value < 12);
async function api(path, data, retry = true) {
  const r = await fetch("/api/" + path, {
    method: data === undefined ? "GET" : "POST",
    headers: data === undefined ? {} : { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: "no-store",
  });
  const result = await r.json();
  if (r.status === 401 && path !== "session" && token && retry) {
    await api("session", { token });
    reconnect();
    return api(path, data, false);
  }
  if (!r.ok) throw new Error(result.error || "Request failed");
  return result;
}
async function refresh() {
  if (refreshing || !authenticated.value) return;
  refreshing = true;
  try {
    state.value = await api("status");
    lastStatusAt.value = Date.now();
    now.value = Date.now();
    events.value = (await api("events")).events;
    error.value = "";
  } catch (e) {
    error.value = e.message;
  } finally {
    refreshing = false;
  }
}
async function action(op, values = {}) {
  if (busy.value) return;
  busy.value = true;
  notice.value = "";
  error.value = "";
  try {
    await api("action", { op, ...values });
    notice.value =
      op === "paste"
        ? "Paste sent to the focused VM field. No Enter key pressed."
        : "Key sent to the VM.";
    if (op === "paste") text.value = "";
    await refresh();
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
function pasted(event) {
  if (!autoPaste.value) return;
  const value = event.clipboardData?.getData("text/plain");
  if (value) {
    event.preventDefault();
    action("paste", { text: value });
  }
}
function interaction() {
  if (!control.value || Date.now() - lastInteraction < 10000) return;
  lastInteraction = Date.now();
  api("interaction", {}).catch(() => {});
}
function attachViewer() {
  const win = viewer.value?.contentWindow;
  const doc = win?.document;
  if (!doc) return;
  doc.addEventListener("pointerdown", interaction);
  doc.addEventListener("keydown", interaction);
  const observer = new MutationObserver(() => {
    connected.value = doc.documentElement.classList.contains("noVNC_connected");
  });
  observer.observe(doc.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });
}
function reconnect() {
  connected.value = false;
  generation.value++;
}
function fullscreen() {
  viewer.value?.requestFullscreen?.().catch(() => {
    error.value = "Fullscreen is unavailable in this browser.";
  });
}
async function connect() {
  try {
    await api("session", { token });
    authenticated.value = true;
    await refresh();
  } catch (e) {
    error.value = e.message;
  }
}
onMounted(async () => {
  clockTimer = setInterval(() => { now.value = Date.now(); }, 1000);
  await connect();
  timer = setInterval(refresh, 4000);
});
onUnmounted(() => { clearInterval(timer); clearInterval(clockTimer); });
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <a class="brand" href="#" @click.prevent
        ><span class="brand-icon">≋</span
        ><span>Live Transcript<small>MEETING CONSOLE</small></span></a
      >
      <div class="session-badges">
        <span class="badge" :class="{ good: connected }"
          ><i />{{ connected ? "Viewer connected" : "Viewer offline" }}</span
        ><span class="badge" :class="{ recording }"
          ><i />{{
            recording
              ? "Recording"
              : fresh
                ? "Capture needs attention"
                : "Capture unconfirmed"
          }}</span
        >
      </div>
      <span class="private-label">PRIVATE VM</span>
    </header>
    <main>
      <section class="workspace">
        <div class="section-heading">
          <div>
            <span class="eyebrow">BROWSER WORKSPACE</span>
            <h1>Your meeting, within reach.</h1>
          </div>
          <span class="mode" :class="{ active: control }">{{
            control ? "Manual control active" : "View only"
          }}</span>
        </div>
        <div v-if="error" role="alert" class="banner error">
          {{ error }}<button @click="authenticated ? refresh() : connect()">Retry</button>
        </div>
        <div class="viewer-frame">
          <iframe
            v-if="authenticated"
            :key="generation"
            ref="viewer"
            :src="viewerUrl"
            title="Isolated meeting browser"
            allow="fullscreen; clipboard-read; clipboard-write"
            @load="attachViewer"
          />
          <div v-else class="empty">
            <span class="brand-icon">≋</span>
            <h2>Opening your console…</h2>
            <p>Open the complete private console link to continue.</p>
          </div>
        </div>
        <div class="viewer-toolbar">
          <span
            ><i :class="{ live: connected }" />{{ state?.display || "VM" }}
            <span class="subtle"
              >·
              {{
                control ? "Click inside to use the browser" : "Observation mode"
              }}</span
            ></span
          >
          <div>
            <button @click="reconnect" :disabled="!authenticated">
              Reconnect viewer</button
            ><button @click="fullscreen" :disabled="!authenticated">
              ⤢ Fullscreen
            </button>
          </div>
        </div>
        <p class="workspace-note">
          Recording runs independently. Reconnecting this viewer does not
          restart the meeting.
        </p>
      </section>
      <aside>
        <section class="panel paste-panel">
          <div class="panel-title">
            <span class="eyebrow">QUICK INPUT</span
            ><span class="keycap">⌘ / Ctrl V</span>
          </div>
          <h2>Paste straight into the VM</h2>
          <p>Click the destination field in the browser, then paste here.</p>
          <label class="sr-only" for="paste">Text to paste into the VM</label
          ><textarea
            id="paste"
            v-model="text"
            @paste="pasted"
            placeholder="Paste a link or text here…"
            maxlength="16384"
            :disabled="!control || busy"
          /><label class="checkbox"
            ><input type="checkbox" v-model="autoPaste" /> Send immediately when
            I paste</label
          ><button
            class="primary"
            :disabled="!control || busy || !text"
            @click="action('paste', { text })"
          >
            {{ busy ? "Sending…" : "Paste into VM" }}<span>↗</span>
          </button>
          <p class="micro">Pastes text only. Never presses Enter.</p>
          <div v-if="notice" role="status" class="success-note">
            {{ notice }}
          </div>
          <div class="keys">
            <button
              :disabled="!control || busy"
              @click="action('key', { key: 'Escape' })"
            >
              Esc</button
            ><button
              :disabled="!control || busy"
              @click="action('key', { key: 'Tab' })"
            >
              Tab ↹</button
            ><button
              :disabled="!control || busy"
              @click="action('key', { key: 'ctrl+l' })"
            >
              Address bar
            </button>
          </div>
        </section>
        <section class="panel">
          <div class="panel-title">
            <span class="eyebrow">CAPTURE HEALTH</span
            ><span class="pulse" :class="{ good: recording }" />
          </div>
          <div class="capture-title">
            <h2>
              {{
                recording ? "Evidence is being saved" : "Check recording status"
              }}
            </h2>
            <span class="duration">{{ elapsed }}</span>
          </div>
          <div class="health-grid">
            <div>
              <span>Audio</span
              ><strong :class="{ good: fresh && capture?.audio?.running }">{{
                fresh && capture?.audio?.running ? "Saving" : "Unavailable"
              }}</strong>
            </div>
            <div>
              <span>Video</span
              ><strong :class="{ good: fresh && capture?.screen?.running }">{{
                fresh && capture?.screen?.running ? "Saving" : "Unavailable"
              }}</strong>
            </div>
            <div>
              <span>Snapshots</span
              ><strong>{{
                fresh && capture?.snapshots?.running ? "Saving" : "Unavailable"
              }}</strong>
            </div>
            <div>
              <span>Free storage</span><strong>{{ disk }}</strong>
            </div>
          </div>
          <div class="speech-strip" v-if="speechFresh">
            <span
              >STT queue
              <strong
                >{{
                  Number(state.speech.queued_audio_s || 0).toFixed(1)
                }}s</strong
              ></span
            ><span :class="{ warning: state.speech.decode_error }">{{
              state.speech.decode_error
                ? "Recognition delayed"
                : state.speech.signal_flags?.includes("digital_silence")
                  ? "Signal: silence"
                  : "Signal present"
            }}</span>
          </div>
          <p class="micro" v-if="capture?.speaker_source">
            {{ capture.speaker_source }}
          </p>
          <p class="micro" v-if="!fresh">
            No fresh capture heartbeat for this display.
          </p>
        </section>
        <section class="panel activity">
          <div class="panel-title">
            <span class="eyebrow">RECENT ACTIVITY</span
            ><span class="subtle">Hermes ready</span>
          </div>
          <ol v-if="events.length">
            <li v-for="event in events.slice(-6).reverse()" :key="event.id">
              <i :class="{ warning: event.level === 'warning' }" />
              <div>
                <span>{{ event.message }}</span
                ><time>{{
                  new Date(event.utc * 1000).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                  })
                }}</time>
              </div>
            </li>
          </ol>
          <p v-else class="micro">
            Status changes and manual actions appear here.
          </p>
          <p class="micro">
            Clipboard contents are never written to the activity log.
          </p>
        </section>
      </aside>
    </main>
    <footer>
      <span>Isolated browser. Independent capture.</span
      ><span>Live Transcript / Operator console</span>
    </footer>
  </div>
</template>

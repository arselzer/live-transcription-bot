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
const token = fragment.get("token") || "";
const password = ref(fragment.get("password") || "");
const authenticated = ref(false);
const consoles = ref([]),
  selected = ref("local"),
  vmReady = ref(false);
const apiBase = computed(
  () => consoles.value.find((item) => item.id === selected.value)?.prefix || "",
);
const now = ref(Date.now()),
  lastStatusAt = ref(0);
let timer,
  clockTimer,
  viewerObserver,
  refreshing = null,
  lastInteraction = 0;
const viewerUrl = computed(
  () =>
    apiBase.value +
    "/novnc/vnc.html#" +
    new URLSearchParams({
      autoconnect: "1",
      resize: "scale",
      password: password.value,
      path: (apiBase.value + "/novnc/websockify").replace(/^\//, ""),
      show_dot: "1",
    }),
);
const capture = computed(() => state.value?.capture);
const admission = computed(() => state.value?.admission);
const admissionText = computed(() => {
  if (!admission.value) return "";
  if (admission.value.stage === "admitted") return "Admission confirmed";
  if (admission.value.stage === "joining") return "Joining the meeting";
  if (admission.value.stage === "needs_operator") {
    const left = Math.max(
      0,
      Math.ceil((admission.value.deadline || 0) - now.value / 1000),
    );
    return left > 0
      ? `Join through the console · ${left}s remaining`
      : "Admission recovery window ended";
  }
  return "Admission was not confirmed";
});
const statusAge = computed(() =>
  Math.max(0, (now.value - lastStatusAt.value) / 1000),
);
const fresh = computed(
  () => capture.value && capture.value.age + statusAge.value < 20,
);
const speechFresh = computed(
  () => state.value?.speech && state.value.speech.age + statusAge.value < 20,
);
function saving(component) {
  const item = capture.value?.[component];
  return (
    fresh.value &&
    item?.running &&
    Number.isFinite(item.progress_age) &&
    item.progress_age + capture.value.age + statusAge.value < 20
  );
}
const recording = computed(() => saving("audio") && saving("screen"));
const elapsed = computed(() => {
  const seconds =
    (capture.value?.elapsed || 0) +
    (capture.value ? Math.min(statusAge.value, 20) : 0);
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
});
const disk = computed(() =>
  capture.value?.free_bytes == null
    ? "—"
    : (capture.value.free_bytes / 1073741824).toFixed(1) + " GB",
);
const control = computed(
  () => state.value?.control === true && statusAge.value < 12,
);
async function api(path, data, retry = true, base = apiBase.value) {
  const r = await fetch(base + "/api/" + path, {
    method: data === undefined ? "GET" : "POST",
    headers: data === undefined ? {} : { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: "no-store",
  });
  const result = await r.json();
  if (r.status === 401 && path !== "session" && token && retry) {
    await api("session", { token }, false, "");
    return api(path, data, false, base);
  }
  if (!r.ok) throw new Error(result.error || "Request failed");
  return result;
}
async function refresh() {
  const base = apiBase.value;
  if (refreshing === base || !authenticated.value || !vmReady.value) return;
  refreshing = base;
  try {
    const [status, activity] = await Promise.all([
      api("status", undefined, true, base),
      api("events", undefined, true, base),
    ]);
    if (base !== apiBase.value) return;
    state.value = status;
    lastStatusAt.value = Date.now();
    now.value = Date.now();
    events.value = activity.events;
    error.value = "";
  } catch (e) {
    if (base === apiBase.value) error.value = e.message;
  } finally {
    if (refreshing === base) refreshing = null;
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
  viewerObserver?.disconnect();
  const win = viewer.value?.contentWindow;
  const doc = win?.document;
  if (!doc) return;
  doc.addEventListener("pointerdown", interaction);
  doc.addEventListener("keydown", interaction);
  viewerObserver = new MutationObserver(() => {
    connected.value = doc.documentElement.classList.contains("noVNC_connected");
  });
  viewerObserver.observe(doc.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });
}
async function selectConsole(id) {
  if (busy.value || !consoles.value.some((item) => item.id === id)) return;
  selected.value = id;
  state.value = null;
  events.value = [];
  notice.value = "";
  text.value = "";
  error.value = "";
  lastStatusAt.value = 0;
  await reconnect();
}
async function reconnect() {
  const base = apiBase.value;
  viewerObserver?.disconnect();
  vmReady.value = false;
  connected.value = false;
  try {
    let session = await api("session", undefined, true, base);
    if (!session.authenticated && token) {
      await api("session", { token }, false, "");
      session = await api("session", undefined, false, base);
    }
    if (base !== apiBase.value) return;
    if (!session.authenticated)
      throw new Error("Open a private console login link first.");
    password.value = session.viewer_password || "";
    generation.value++;
    vmReady.value = true;
    await refresh();
  } catch (e) {
    if (base === apiBase.value) error.value = e.message;
  }
}
function fullscreen() {
  viewer.value?.requestFullscreen?.().catch(() => {
    error.value = "Fullscreen is unavailable in this browser.";
  });
}
async function connect() {
  try {
    let session = await api("session", undefined, true, "");
    if (!session.authenticated) {
      if (!token) throw new Error("Open a private console login link first.");
      session = await api("session", { token }, false, "");
    }
    password.value = session.viewer_password || password.value;
    authenticated.value = true;
    consoles.value = session.consoles || [
      { id: "local", label: "Current VM", prefix: "" },
    ];
    await selectConsole(consoles.value[0].id);
  } catch (e) {
    error.value = e.message;
  }
}
onMounted(async () => {
  clockTimer = setInterval(() => {
    now.value = Date.now();
  }, 1000);
  await connect();
  timer = setInterval(refresh, 4000);
});
onUnmounted(() => {
  viewerObserver?.disconnect();
  clearInterval(timer);
  clearInterval(clockTimer);
});
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">Live Transcript <span>VM console</span></div>
      <label v-if="consoles.length > 1" class="vm-selector">
        <span>VM</span>
        <select
          :value="selected"
          :disabled="busy"
          @change="selectConsole($event.target.value)"
        >
          <option v-for="item in consoles" :key="item.id" :value="item.id">
            {{ item.label }}
          </option>
        </select>
      </label>
      <div class="session-badges">
        <span class="badge" :class="{ good: connected }"
          ><i />{{ connected ? "Connected" : "Disconnected" }}</span
        ><span class="badge" :class="{ recording }"
          ><i />{{
            recording
              ? "Recording"
              : fresh
                ? "Recording needs attention"
                : "Recording status unavailable"
          }}</span
        >
      </div>
    </header>
    <main>
      <section class="workspace">
        <div class="section-heading">
          <h1>Meeting browser</h1>
          <span class="mode" :class="{ active: control }">{{
            control ? "Keyboard and mouse enabled" : "View only"
          }}</span>
        </div>
        <div v-if="error" role="alert" class="banner error">
          {{ error
          }}<button @click="authenticated ? refresh() : connect()">
            Retry
          </button>
        </div>
        <div class="viewer-frame">
          <iframe
            v-if="authenticated && vmReady"
            :key="generation"
            ref="viewer"
            :src="viewerUrl"
            title="Isolated meeting browser"
            allow="fullscreen; clipboard-read; clipboard-write"
            @load="attachViewer"
          />
          <div v-else class="empty">
            <h2>Connecting…</h2>
            <p>Use the full console link to connect.</p>
          </div>
        </div>
        <div class="viewer-toolbar">
          <span
            ><i :class="{ live: connected }" />{{ state?.display || "VM" }}
            <span class="subtle"
              >·
              {{
                control ? "Click inside to control the browser" : "View only"
              }}</span
            ></span
          >
          <div>
            <button @click="reconnect" :disabled="!authenticated">
              Reconnect</button
            ><button @click="fullscreen" :disabled="!authenticated">
              Fullscreen
            </button>
          </div>
        </div>
        <p class="workspace-note">
          You can reconnect this view while recording continues.
        </p>
      </section>
      <aside>
        <section class="panel paste-panel">
          <div class="panel-title">
            <h2>Paste into the VM</h2>
            <span class="keycap">⌘ / Ctrl V</span>
          </div>
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
            {{ busy ? "Pasting…" : "Paste into VM" }}
          </button>
          <p class="micro">Pastes text without pressing Enter.</p>
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
              Tab</button
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
            <h2>Recording</h2>
            <span class="pulse" :class="{ good: recording }" />
          </div>
          <div class="capture-title">
            <h2>
              {{
                recording
                  ? "Audio and video are being saved"
                  : "Check recording status"
              }}
            </h2>
            <span class="duration">{{ elapsed }}</span>
          </div>
          <p
            v-if="admissionText"
            class="micro"
            :class="{
              warning:
                admission?.stage === 'needs_operator' ||
                admission?.stage === 'rejected',
            }"
            role="status"
          >
            {{ admissionText }}
          </p>
          <p v-if="admission && admission.stage !== 'admitted'" class="micro">
            Join evidence is being saved. Meeting admission is still
            unconfirmed.
          </p>
          <div class="health-grid">
            <div>
              <span>Audio</span
              ><strong :class="{ good: saving('audio') }">{{
                saving("audio") ? "Saving" : "Unavailable"
              }}</strong>
            </div>
            <div>
              <span>Video</span
              ><strong :class="{ good: saving('screen') }">{{
                saving("screen") ? "Saving" : "Unavailable"
              }}</strong>
            </div>
            <div>
              <span>Snapshots</span
              ><strong>{{
                saving("snapshots") ? "Saving" : "Unavailable"
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
                  ? "No audio detected"
                  : "Audio detected"
            }}</span>
          </div>
          <p class="micro" v-if="capture?.speaker_source">
            {{ capture.speaker_source }}
          </p>
          <p class="micro" v-if="!fresh">
            Recording status has not updated recently.
          </p>
        </section>
        <section class="panel activity">
          <div class="panel-title">
            <h2>Activity</h2>
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
          <p class="micro">Pasted text is not included in this log.</p>
        </section>
      </aside>
    </main>
  </div>
</template>

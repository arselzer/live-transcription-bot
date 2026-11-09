<script setup>
import { computed, ref, onMounted, onUnmounted } from "vue";
import MeetingTranscript from "./MeetingTranscript.vue";
import RecordingLibrary from "./RecordingLibrary.vue";
const persistent = ref(false),
  release = ref(""),
  manualControl = ref(false),
  workspaceControl = ref(false);
const invitation = ref(""),
  duration = ref(90),
  meetingBusy = ref(false),
  stopPending = ref(false),
  jobs = ref([]);
let workspaceGeneration = null;
const run = computed(() => state.value?.workspace?.run || {});
const activeRun = computed(
  () =>
    ["active", "control_lost"].includes(run.value.status) && run.value.run_id,
);
async function takeControl() {
  const enabled = !manualControl.value;
  try {
    await api("control", {
      enabled,
      target_generation: state.value?.workspace?.target_generation,
    });
    manualControl.value = enabled;
    if (enabled) tab.value = "console";
  } catch (e) {
    error.value = e.message;
  }
}
async function startMeeting() {
  if (meetingBusy.value) return;
  meetingBusy.value = true;
  error.value = "";
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  const request_id = Array.from(bytes, (x) =>
    x.toString(16).padStart(2, "0"),
  ).join("");
  try {
    await api("meeting/start", {
      url: invitation.value,
      minutes: Number(duration.value),
      request_id,
    });
    invitation.value = "";
    notice.value = "Meeting start queued.";
    await refresh();
  } catch (e) {
    error.value = e.message;
  } finally {
    meetingBusy.value = false;
  }
}
async function stopMeeting() {
  if (!stopPending.value) {
    stopPending.value = true;
    return;
  }
  const run_id = run.value.run_id;
  meetingBusy.value = true;
  try {
    await api("meeting/stop", { run_id });
    notice.value =
      "Meeting stop requested. Retrieval and processing continue independently.";
  } catch (e) {
    error.value = e.message;
  } finally {
    meetingBusy.value = false;
    stopPending.value = false;
  }
}
async function cancelJob(id) {
  try {
    await api("jobs/cancel", { id });
    notice.value = "Processing cancellation requested; originals are retained.";
  } catch (e) {
    error.value = e.message;
  }
}
const audio = ref(null),
  listening = ref(false),
  audioError = ref("");
let audioAttempt = 0;
function stopListening() {
  audioAttempt++;
  listening.value = false;
  if (audio.value) {
    audio.value.pause();
    audio.value.removeAttribute("src");
    audio.value.load();
  }
}
async function listen() {
  if (listening.value) {
    stopListening();
    return;
  }
  const attempt = ++audioAttempt;
  audioError.value = "";
  listening.value = true;
  audio.value.src = apiBase.value + "/api/audio";
  try {
    await audio.value.play();
  } catch (_) {
    if (attempt === audioAttempt) {
      stopListening();
      audioError.value =
        "Audio playback is unavailable. Recording is independent.";
    }
  }
}
function audioFailed() {
  if (!listening.value) return;
  stopListening();
  audioError.value = "Audio playback stopped. You can try listening again.";
}
const tab = ref("transcript"),
  meeting = ref(null),
  meetingError = ref(""),
  lastMeetingAt = ref(0);
let recoveryIncident = "";
const state = ref(null),
  events = ref([]),
  error = ref(""),
  statusError = ref(""),
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
  connectionAttempt = 0,
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
const statusFresh = computed(() => state.value && statusAge.value < 12);
const textBytes = computed(() => new TextEncoder().encode(text.value).length);
const pasteTooLarge = computed(() => textBytes.value > 16384);
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
const recordingLabel = computed(() =>
  recording.value
    ? admission.value && admission.value.stage !== "admitted"
      ? "Saving join evidence"
      : "Recording"
    : fresh.value
      ? "Recording needs attention"
      : statusFresh.value && !capture.value
        ? "No active recording reported"
        : "Recording status unknown",
);
const captureTitle = computed(() =>
  recording.value
    ? admission.value && admission.value.stage !== "admitted"
      ? "Join evidence is being saved"
      : "Audio and video are being saved"
    : statusFresh.value && !capture.value
      ? "No active recording reported"
      : "Check recording status",
);
const elapsed = computed(() => {
  const seconds =
    (capture.value?.elapsed || 0) +
    (capture.value ? Math.min(statusAge.value, 20) : 0);
  const minutes = Math.floor(seconds / 60);
  const tail = `${String(minutes % 60).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${tail}`
    : `${minutes}:${tail.slice(-2)}`;
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
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  let r, result;
  try {
    r = await fetch(base + "/api/" + path, {
      method: data === undefined ? "GET" : "POST",
      headers: data === undefined ? {} : { "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
      cache: "no-store",
      signal: controller.signal,
    });
    result = await r.json();
  } catch (e) {
    if (e.name === "AbortError")
      throw new Error(
        path === "action"
          ? "No reply from the VM. The action may have completed; check the browser before trying again."
          : "The VM did not respond in time. Try reconnecting.",
      );
    throw e;
  } finally {
    clearTimeout(timeout);
  }
  if (r.status === 401 && path !== "session" && token && retry) {
    await api("session", { token }, false, "");
    return api(path, data, false, base);
  }
  if (!r.ok) throw new Error(result.error || "Request failed");
  return result;
}
async function refresh() {
  const base = apiBase.value;
  const attempt = connectionAttempt;
  if (
    (refreshing?.base === base && refreshing?.attempt === attempt) ||
    !authenticated.value ||
    !vmReady.value
  )
    return;
  const request = { base, attempt };
  refreshing = request;
  try {
    const current = () =>
      base === apiBase.value && attempt === connectionAttempt;
    const status = api("status", undefined, true, base)
      .then((value) => {
        if (!current()) return false;
        state.value = value;
        if (value.workspace) {
          persistent.value = true;
          workspaceControl.value = value.workspace.control_enabled;
          if (manualControl.value && !value.workspace.control_lease_active)
            manualControl.value = false;
          const next = `${base}:${value.workspace.generation}`;
          if (workspaceGeneration !== next) {
            stopListening();
            workspaceGeneration = next;
            password.value = value.workspace.viewer_password || "";
            generation.value++;
          }
          if (manualControl.value && document.visibilityState === "visible")
            api("control", {
              enabled: true,
              target_generation: value.workspace.target_generation,
            }).catch(() => {
              manualControl.value = false;
            });
          else if (manualControl.value) {
            manualControl.value = false;
            api("control", { enabled: false }).catch(() => {});
          }
        }

        lastStatusAt.value = Date.now();
        now.value = Date.now();
        statusError.value = "";
        if (meeting.value?.session?.id !== value.session?.id)
          meeting.value = null;
        const admission = value.admission;
        const incident =
          admission?.stage === "needs_operator"
            ? `${value.session?.id}:${admission.utc}`
            : "";
        if (incident && incident !== recoveryIncident) {
          tab.value = "console";
          recoveryIncident = incident;
        }
        return true;
      })
      .catch((e) => {
        if (current()) statusError.value = e.message;
        return false;
      });
    const activity = api("events", undefined, true, base).then((value) => {
      if (current()) events.value = value.events;
    });
    const captions = api("meeting", undefined, true, base)
      .then(async (value) => {
        const statusOK = await status;
        if (!current()) return;
        if (statusOK && value.session?.id === state.value?.session?.id) {
          if (value.status === "unavailable") {
            meetingError.value =
              "Caption journal is unavailable; recording is independent";
            return;
          }
          meeting.value = value;
          lastMeetingAt.value = Date.now();
          meetingError.value = "";
        } else meetingError.value = "Waiting for current meeting status";
      })
      .catch((e) => {
        if (current()) meetingError.value = e.message;
      });
    const work = persistent.value
      ? api("jobs", undefined, true, base)
          .then((value) => {
            if (current()) jobs.value = value.jobs || [];
          })
          .catch(() => {})
      : Promise.resolve();
    await Promise.allSettled([status, activity, captions, work]);
  } finally {
    if (refreshing === request) refreshing = null;
  }
}
async function action(op, values = {}) {
  if (busy.value) return;
  const attempt = connectionAttempt;
  if (
    op === "paste" &&
    new TextEncoder().encode(values.text || "").length > 16384
  ) {
    error.value = "Paste up to 16 KiB at a time. Your text is still here.";
    return;
  }
  busy.value = true;
  notice.value = "";
  error.value = "";
  try {
    await api("action", { op, ...values });
    if (attempt !== connectionAttempt) return;
    notice.value =
      op === "paste"
        ? "Paste sent to the focused VM field. No Enter key pressed."
        : "Key sent to the VM.";
    if (op === "paste" && text.value === values.text) text.value = "";
    await refresh();
  } catch (e) {
    if (attempt === connectionAttempt) error.value = e.message;
  } finally {
    busy.value = false;
  }
}
function pasted(event) {
  if (!autoPaste.value) return;
  const value = event.clipboardData?.getData("text/plain");
  if (value) {
    event.preventDefault();
    text.value = value;
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
  connected.value = doc.documentElement.classList.contains("noVNC_connected");
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
  if (manualControl.value) {
    await api("control", { enabled: false }).catch(() => {});
    manualControl.value = false;
  }
  persistent.value = false;
  workspaceGeneration = null;
  jobs.value = [];
  stopPending.value = false;
  selected.value = id;
  meeting.value = null;
  meetingError.value = "";
  lastMeetingAt.value = 0;
  recoveryIncident = "";
  tab.value = "transcript";
  state.value = null;
  events.value = [];
  notice.value = "";
  text.value = "";
  error.value = "";
  statusError.value = "";
  lastStatusAt.value = 0;
  await reconnect();
}
async function reconnect() {
  stopListening();
  audioError.value = "";
  const base = apiBase.value;
  const attempt = ++connectionAttempt;
  error.value = "";
  statusError.value = "";
  viewerObserver?.disconnect();
  vmReady.value = false;
  connected.value = false;
  try {
    let session = await api("session", undefined, true, base);
    if (!session.authenticated && token) {
      await api("session", { token }, false, "");
      session = await api("session", undefined, false, base);
    }
    if (base !== apiBase.value || attempt !== connectionAttempt) return;
    if (!session.authenticated)
      throw new Error("Open a private console login link first.");
    persistent.value = !!session.persistent;
    release.value = session.release || "";
    workspaceControl.value = !!session.control_enabled;
    password.value = session.viewer_password || "";
    generation.value++;
    vmReady.value = true;
    await refresh();
  } catch (e) {
    if (base === apiBase.value && attempt === connectionAttempt)
      error.value = e.message;
  }
}
async function copyStatus() {
  const report = JSON.stringify(
    {
      captured_at: new Date().toISOString(),
      vm:
        consoles.value.find((item) => item.id === selected.value)?.label ||
        "Current VM",
      status_age_seconds: Math.round(statusAge.value),
      display: state.value?.display,
      control: control.value,
      admission: admission.value,
      capture: capture.value,
      speech: state.value?.speech,
    },
    null,
    2,
  );
  try {
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(report);
    } else {
      const field = document.createElement("textarea");
      field.value = report;
      field.className = "sr-only";
      document.body.appendChild(field);
      try {
        field.select();
        if (!document.execCommand("copy")) throw new Error("Copy unavailable");
      } finally {
        field.remove();
      }
    }
    notice.value =
      "Status copied. It contains no console login link or pasted text.";
  } catch (_) {
    error.value =
      "Copy is unavailable in this browser. Status remains visible below.";
  }
}
function fullscreen() {
  viewer.value?.requestFullscreen?.().catch(() => {
    error.value = "Fullscreen is unavailable in this browser.";
  });
}
async function connect() {
  error.value = "";
  statusError.value = "";
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
  if (manualControl.value)
    fetch(apiBase.value + "/api/control", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: false }),
      keepalive: true,
    }).catch(() => {});
  stopListening();
  viewerObserver?.disconnect();
  clearInterval(timer);
  clearInterval(clockTimer);
});
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">Live Transcript <span>Meeting workspace</span></div>
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
          ><i />{{
            connected ? "Viewer connected" : "Viewer disconnected"
          }}</span
        ><span class="badge" :class="{ recording }"
          ><i />{{ recordingLabel }}</span
        >
      </div>
    </header>
    <main :class="{ reviewing: tab === 'library' }">
      <section class="workspace">
        <div class="section-heading">
          <div>
            <h1>
              {{
                state?.session?.provider
                  ? {
                      google: "Google Meet",
                      zoom: "Zoom",
                      proton: "Proton Meet",
                      jitsi: "Jitsi Meet",
                    }[state.session.provider]
                  : "Meeting"
              }}
            </h1>
            <p class="meeting-subtitle">
              {{ admissionText || "Admission status not reported" }}
            </p>
          </div>
          <span class="mode" :class="{ active: control }">{{
            control ? "Keyboard and mouse enabled" : "View only"
          }}</span>
        </div>
        <div v-if="error || statusError" role="alert" class="banner error">
          {{ error || statusError
          }}<button @click="authenticated ? reconnect() : connect()">
            Retry
          </button>
        </div>
        <form
          v-if="persistent && workspaceControl"
          class="meeting-controls"
          @submit.prevent="startMeeting"
        >
          <label class="meeting-link"
            ><span>Meeting invitation</span
            ><input
              type="url"
              v-model="invitation"
              maxlength="4096"
              placeholder="Paste a meeting link"
              required
              :disabled="!!activeRun || meetingBusy"
          /></label>
          <label class="meeting-duration"
            ><span>Minutes</span
            ><input
              type="number"
              v-model="duration"
              min="1"
              max="1440"
              required
              :disabled="!!activeRun || meetingBusy"
          /></label>
          <button
            v-if="!activeRun"
            type="submit"
            :disabled="
              meetingBusy ||
              !authenticated ||
              !invitation ||
              state?.workspace?.unreachable ||
              !!state?.display ||
              state?.capacity?.status === 'blocked'
            "
          >
            {{ meetingBusy ? "Queuing…" : "Join and record" }}
          </button>
          <button
            v-else
            type="button"
            @click="stopMeeting"
            :disabled="meetingBusy"
            :class="{ danger: stopPending }"
          >
            {{ stopPending ? "Confirm stop" : "Stop recording" }}
          </button>
          <button v-if="stopPending" type="button" @click="stopPending = false">
            Keep recording
          </button>
        </form>
        <p
          v-if="persistent && notice && tab !== 'console'"
          class="success-note"
          role="status"
        >
          {{ notice }}
        </p>
        <p
          v-if="persistent && state?.workspace?.unreachable"
          class="banner recovery"
          role="status"
        >
          VM status is unavailable. A recording may still be running.
        </p>
        <p
          v-if="persistent && state?.workspace?.application_ready === false"
          class="banner recovery"
          role="status"
        >
          Application control is unavailable. Queued requests wait for recovery;
          existing VM capture may continue.
        </p>
        <nav class="workspace-tabs" aria-label="Meeting views">
          <button
            :aria-pressed="tab === 'transcript'"
            @click="tab = 'transcript'"
          >
            Transcript
          </button>
          <button :aria-pressed="tab === 'console'" @click="tab = 'console'">
            VM console
            <span v-if="admission?.stage === 'needs_operator'" class="warning"
              >· Needs you</span
            >
          </button>
          <button
            v-if="persistent"
            :aria-pressed="tab === 'library'"
            @click="
              tab = 'library';
              stopListening();
            "
          >
            Recordings
          </button>
        </nav>
        <div
          v-if="admission?.stage === 'needs_operator'"
          class="banner recovery"
          role="status"
        >
          <span
            >{{ admissionText }}.
            {{
              control
                ? "Use the VM browser to finish joining."
                : "Open a console with --control to finish joining."
            }}</span
          >
          <button @click="tab = 'console'">Open console</button>
        </div>
        <RecordingLibrary
          v-if="persistent"
          v-show="tab === 'library'"
          :base="apiBase"
          :active="tab === 'library'"
          @playback="stopListening"
        />
        <MeetingTranscript
          v-show="tab === 'transcript'"
          :active="tab === 'transcript'"
          :meeting="meeting"
          :error="meetingError"
          :stale="!!lastMeetingAt && now - lastMeetingAt > 12000"
        />
        <div v-show="tab === 'console'">
          <div class="viewer-frame">
            <iframe
              v-if="
                authenticated &&
                vmReady &&
                (!persistent || state?.workspace?.viewer_ready)
              "
              :key="generation"
              ref="viewer"
              :src="viewerUrl"
              title="Isolated meeting browser"
              allow="fullscreen; clipboard-read; clipboard-write"
              @load="attachViewer"
            />
            <div v-else class="empty">
              <h2>{{ persistent ? "No viewer connected" : "Connecting…" }}</h2>
              <p>
                {{
                  persistent
                    ? "The viewer opens when the meeting browser is available."
                    : "Use the full console link to connect."
                }}
              </p>
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
              <button
                v-if="persistent && workspaceControl"
                @click="takeControl"
                :disabled="!state?.workspace?.viewer_ready"
              >
                {{ manualControl ? "Release control" : "Take control" }}
              </button>
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
        </div>
      </section>
      <aside v-show="tab !== 'library'">
        <section v-show="tab !== 'library'" class="panel audio-panel">
          <div class="panel-title">
            <h2>Meeting audio</h2>
            <button :disabled="!authenticated || !vmReady" @click="listen">
              {{ listening ? "Stop listening" : "Listen" }}
            </button>
          </div>
          <p class="micro">
            Listen to the selected VM. Your microphone stays off.
          </p>
          <audio
            ref="audio"
            v-show="listening"
            controls
            preload="none"
            controlslist="nodownload noplaybackrate"
            aria-label="Meeting audio playback"
            @error="audioFailed"
            @ended="stopListening"
            @pause="listening && stopListening()"
          />
          <p v-if="audioError" role="status" class="micro warning">
            {{ audioError }}
          </p>
        </section>
        <section v-show="tab === 'console'" class="panel paste-panel">
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
            :disabled="!control || busy"
          />
          <div class="input-meta">
            <span :class="{ warning: pasteTooLarge }"
              >{{ textBytes.toLocaleString() }} / 16,384 bytes</span
            >
            <button
              class="text-button"
              :disabled="!text || busy"
              @click="text = ''"
            >
              Clear
            </button>
          </div>
          <label class="checkbox"
            ><input type="checkbox" v-model="autoPaste" /> Send immediately when
            I paste</label
          ><button
            class="primary"
            :disabled="!control || busy || !text || pasteTooLarge"
            @click="action('paste', { text })"
          >
            {{ busy ? "Sending…" : "Paste into VM" }}
          </button>
          <p class="micro">
            {{
              control
                ? "Pastes text without pressing Enter."
                : "Open this console with manual control to paste or type."
            }}
          </p>
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
              {{ captureTitle }}
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
            Meeting admission is still unconfirmed.
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
          <p class="micro" v-if="capture && !fresh">
            Recording status has not updated recently.
          </p>
          <div class="status-footer">
            <span>{{
              lastStatusAt
                ? `Updated ${Math.floor(statusAge)}s ago`
                : "Waiting for VM status"
            }}</span>
            <button class="text-button" @click="copyStatus" :disabled="!state">
              Copy status
            </button>
          </div>
        </section>
        <section v-if="state?.capacity" class="panel capacity-panel">
          <div class="panel-title">
            <h2>Recording capacity</h2>
            <span :class="{ warning: state.capacity.status !== 'ready' }">{{
              state.capacity.status
            }}</span>
          </div>
          <p class="micro">
            About
            {{ Math.floor(state.capacity.estimated_minutes_remaining) }} minutes
            available at the configured recording estimate.
          </p>
          <div class="health-grid">
            <div>
              <span>Application storage</span
              ><strong
                >{{
                  (state.capacity.host_free_bytes / 1024 ** 3).toFixed(1)
                }}
                GiB</strong
              >
            </div>
            <div>
              <span>VM disk storage</span
              ><strong>{{
                state.capacity.vm_host_free_bytes === null
                  ? "Unknown"
                  : (state.capacity.vm_host_free_bytes / 1024 ** 3).toFixed(1) +
                    " GiB"
              }}</strong>
            </div>
          </div>
          <p
            v-for="message in [
              ...state.capacity.reasons,
              ...state.capacity.warnings,
            ]"
            :key="message"
            class="micro warning"
          >
            {{ message }}
          </p>
        </section>
        <section
          v-if="
            persistent &&
            jobs.some(
              (job) =>
                ['running', 'starting', 'queued'].includes(job.status) &&
                !['watchdog', 'calendar'].includes(job.kind),
            )
          "
          class="panel"
        >
          <div class="panel-title"><h2>Background work</h2></div>
          <div
            v-for="job in jobs
              .filter(
                (job) =>
                  ['running', 'starting', 'queued'].includes(job.status) &&
                  !['watchdog', 'calendar'].includes(job.kind),
              )
              .slice(0, 6)"
            :key="job.id"
            class="job-row"
          >
            <span
              >{{ job.kind }} ·
              {{ job.cancel_requested ? "Cancelling" : job.status }}</span
            ><button
              v-if="job.kind === 'process'"
              :disabled="job.cancel_requested || !workspaceControl"
              @click="cancelJob(job.id)"
            >
              Cancel
            </button>
          </div>
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
    <footer v-if="persistent && release" class="release-note">
      Application {{ release }}
    </footer>
  </div>
</template>

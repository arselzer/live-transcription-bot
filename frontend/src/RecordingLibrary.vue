<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
const props = defineProps({ base: String, active: Boolean });
const emit = defineEmits(["playback"]);
const query = ref(""),
  recordings = ref([]),
  selected = ref(null),
  detail = ref(null);
const error = ref(""),
  loading = ref(false),
  scan = ref(null),
  nextOffset = ref(null);
const passageQuery = ref(""),
  media = ref(null),
  position = ref(0),
  pendingSeek = ref(null);
let generation = 0,
  searchTimer,
  abort;
const lines = computed(() =>
  (detail.value?.lines || []).filter(
    (line) =>
      !passageQuery.value ||
      `${line.speaker} ${line.text}`
        .toLocaleLowerCase()
        .includes(passageQuery.value.toLocaleLowerCase()),
  ),
);
const audioUrl = computed(() =>
  detail.value?.artifacts?.audio
    ? `${props.base}/api/recordings/${detail.value.id}/audio`
    : "",
);
function stamp(t) {
  return `${Math.floor((t || 0) / 60)}:${String(Math.floor((t || 0) % 60)).padStart(2, "0")}`;
}
async function read(path) {
  const response = await fetch(props.base + path, {
    credentials: "same-origin",
    cache: "no-store",
    signal: abort.signal,
  });
  if (!response.ok)
    throw new Error(
      response.status === 401
        ? "Open the private workspace link again."
        : "Recording information is unavailable.",
    );
  return response.json();
}
async function refresh(more = false) {
  const request = ++generation;
  abort?.abort();
  abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 10000);
  loading.value = true;
  error.value = "";
  try {
    const value = await read(
      `/api/recordings?q=${encodeURIComponent(query.value)}&offset=${more ? nextOffset.value || 0 : 0}`,
    );
    if (request !== generation) return;
    recordings.value = more
      ? [...recordings.value, ...value.recordings]
      : value.recordings;
    scan.value = value.scan;
    nextOffset.value = value.next_offset;
  } catch (e) {
    if (request === generation)
      error.value =
        e.name === "AbortError"
          ? "The library did not respond in time. Live recording is independent."
          : e.message;
  } finally {
    clearTimeout(timeout);
    if (request === generation) loading.value = false;
  }
}
async function choose(item) {
  clearTimeout(searchTimer);
  const request = ++generation;
  abort?.abort();
  abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 10000);
  media.value?.pause();
  selected.value = item.id;
  detail.value = null;
  passageQuery.value = "";
  position.value = 0;
  error.value = "";
  loading.value = true;
  try {
    const value = await read(`/api/recordings/${item.id}`);
    if (request === generation) detail.value = value;
  } catch (e) {
    if (request === generation)
      error.value =
        "This recording is unavailable. Try refreshing the library.";
  } finally {
    clearTimeout(timeout);
    if (request === generation) loading.value = false;
  }
}
function search() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => refresh(), 300);
}
function seek(t) {
  if (
    !media.value ||
    !audioUrl.value ||
    detail.value?.audio_matches_transcript === false
  )
    return;
  pendingSeek.value = t;
  if (media.value.readyState > 0) playAt();
  else media.value.load();
}
function playAt() {
  if (pendingSeek.value === null || !media.value) return;
  media.value.currentTime = pendingSeek.value;
  pendingSeek.value = null;
  emit("playback");
  media.value.play().catch(() => {
    error.value =
      "Playback could not start. Use the audio controls to try again.";
  });
}
function mediaError() {
  error.value =
    "Audio playback is unavailable. The retained original has not been changed.";
}
watch(
  () => props.base,
  () => {
    abort?.abort();
    generation++;
    media.value?.pause();
    recordings.value = [];
    detail.value = null;
    selected.value = null;
    if (props.active) refresh();
  },
);
watch(
  () => props.active,
  (active) => {
    if (active && !recordings.value.length) refresh();
    if (!active) media.value?.pause();
  },
  { immediate: true },
);
onUnmounted(() => {
  abort?.abort();
  clearTimeout(searchTimer);
  media.value?.pause();
});
</script>

<template>
  <section class="library" aria-label="Recording library">
    <div class="library-tools">
      <label
        ><span class="sr-only">Search recordings</span
        ><input
          type="search"
          v-model="query"
          @input="search"
          placeholder="Search recordings"
      /></label>
      <button @click="refresh()" :disabled="loading">
        {{ loading ? "Loading…" : "Refresh" }}
      </button>
    </div>
    <p v-if="error" role="status" class="banner error">{{ error }}</p>
    <div class="library-layout">
      <div class="recording-list">
        <p v-if="!recordings.length && !loading" class="micro">
          {{
            scan
              ? "No matching recordings."
              : "The library is indexing. Recordings appear after retrieval."
          }}
        </p>
        <button
          v-for="item in recordings"
          :key="item.id"
          class="recording-item"
          :aria-pressed="selected === item.id"
          @click="choose(item)"
        >
          <strong>{{ item.title }}</strong>
          <span
            >{{
              item.updated_at
                ? new Date(item.updated_at * 1000).toLocaleDateString()
                : "Date unavailable"
            }}
            · {{ item.duration ? stamp(item.duration) : item.status }}</span
          >
          <span>{{ item.provider || "Recording" }} · {{ item.status }}</span>
        </button>
        <button
          v-if="nextOffset !== null"
          @click="refresh(true)"
          :disabled="loading"
        >
          Load more
        </button>
        <p v-if="scan && !scan.complete" class="micro warning">
          Some recording storage could not be indexed. Showing the saved
          catalogue.
        </p>
      </div>
      <div v-if="detail" class="recording-detail panel">
        <h2>{{ detail.title }}</h2>
        <p class="micro">
          {{ detail.status }} · Transcript: {{ detail.transcript_source }}.
          Speaker labels are observations and may be uncertain.
        </p>
        <p
          v-if="detail.audio_matches_transcript === false"
          class="micro warning"
        >
          The audio changed after this transcript was generated. Timestamp
          playback is disabled until it is processed again.
        </p>
        <audio
          v-if="audioUrl"
          ref="media"
          :src="audioUrl"
          controls
          preload="metadata"
          aria-label="Recorded meeting audio"
          @loadedmetadata="playAt"
          @timeupdate="position = $event.target.currentTime"
          @play="emit('playback')"
          @error="mediaError"
        />
        <p v-else class="micro">
          A playback WAV is not available yet. Original archive evidence may
          still be retained.
        </p>
        <div class="recording-downloads">
          <a
            v-for="(info, kind) in detail.artifacts"
            :key="kind"
            :href="`${base}/api/recordings/${detail.id}/${kind}`"
            :download="info.filename || `${detail.title}-${kind}`"
            >Download {{ kind }}</a
          >
        </div>
        <details v-if="detail.events.length" class="recording-events">
          <summary>
            {{ detail.events.length }} capture and recovery events
          </summary>
          <ul>
            <li v-for="(event, index) in detail.events" :key="index">
              <button
                v-if="event.t !== null && audioUrl"
                @click="seek(event.t)"
              >
                {{ stamp(event.t) }}
              </button>
              <time v-else>{{
                new Date(event.utc * 1000).toLocaleTimeString()
              }}</time>
              {{ event.kind.replaceAll("_", " ") }} · {{ event.component }} ·
              estimated timing
            </li>
          </ul>
        </details>
        <label class="library-passage-search"
          ><span class="sr-only">Search saved transcript</span
          ><input
            type="search"
            v-model="passageQuery"
            placeholder="Search this transcript"
        /></label>
        <div class="recorded-passages">
          <p v-if="!lines.length" class="micro">
            No matching transcript passages. Processing may still be running.
          </p>
          <article
            v-for="(line, index) in lines"
            :key="`${line.t}-${index}`"
            :class="{ playing: position >= line.t && position < line.end }"
          >
            <div>
              <button
                :disabled="
                  !audioUrl || detail.audio_matches_transcript === false
                "
                @click="seek(line.t)"
                :aria-label="`Play from ${stamp(line.t)}`"
              >
                {{ stamp(line.t) }}</button
              ><strong>{{ line.speaker }}</strong>
            </div>
            <p>{{ line.text }}</p>
          </article>
        </div>
        <details v-if="detail.notes" class="recording-notes">
          <summary>Meeting notes</summary>
          <pre>{{ detail.notes }}</pre>
        </details>
      </div>
      <div v-else class="panel library-empty">
        <h2>Select a recording</h2>
        <p>
          Review the transcript and play its original audio from a timestamp.
        </p>
      </div>
    </div>
  </section>
</template>

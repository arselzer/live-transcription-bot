<script setup>
import { computed, nextTick, ref, watch } from "vue";
const props = defineProps({
  meeting: Object,
  error: String,
  stale: Boolean,
  active: Boolean,
});
const query = ref(""),
  follow = ref(true),
  fontSize = ref(17),
  scroller = ref(null);
const lines = computed(() => props.meeting?.lines || []);
const filtered = computed(() => {
  const term = query.value.trim().toLocaleLowerCase();
  return lines.value.filter(
    (line) =>
      !term ||
      `${line.speaker} ${line.text}`.toLocaleLowerCase().includes(term),
  );
});
function stamp(t) {
  return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
}
function color(name) {
  let hash = 0;
  for (const char of name) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return `speaker-${Math.abs(hash % 5)}`;
}
async function scrollLive() {
  await nextTick();
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}
function resume() {
  follow.value = true;
  query.value = "";
  scrollLive();
}
function scrolled() {
  const el = scroller.value;
  if (el && el.scrollHeight - el.scrollTop - el.clientHeight > 60)
    follow.value = false;
}
watch(
  () => props.meeting?.session?.id,
  () => {
    query.value = "";
    follow.value = true;
  },
);
watch(lines, () => {
  if (follow.value && !query.value) scrollLive();
});
watch(
  () => props.active,
  (value) => {
    if (value && follow.value && !query.value) scrollLive();
  },
);
function download() {
  const content = lines.value
    .map((line) => `[${stamp(line.t)}] ${line.speaker}: ${line.text}`)
    .join("\n\n");
  const url = URL.createObjectURL(
    new Blob([content], { type: "text/plain;charset=utf-8" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = "live-transcript.txt";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>

<template>
  <section
    class="transcript-panel"
    :class="{ 'has-notes': meeting?.board?.bullets?.length }"
  >
    <div class="transcript-tools">
      <label class="search-field"
        ><span class="sr-only">Search transcript</span
        ><input
          type="search"
          v-model="query"
          placeholder="Search transcript"
          @input="follow = false"
      /></label>
      <label class="font-control"
        >Text
        <input
          aria-label="Transcript text size"
          type="range"
          min="14"
          max="24"
          v-model="fontSize"
      /></label>
      <button @click="download" :disabled="!lines.length">Save text</button>
    </div>
    <div class="transcript-meta">
      <span>{{
        query
          ? `${filtered.length} matching passages`
          : meeting?.truncated
            ? `Latest ${lines.length} passages · full transcript retained in journal`
            : "Live transcript"
      }}</span>
      <span :class="{ warning: stale || error }">{{
        error ||
        (stale
          ? "Updates delayed"
          : meeting?.status === "available"
            ? "Connected to caption journal"
            : "Waiting for captions")
      }}</span>
    </div>
    <div
      ref="scroller"
      class="transcript-scroll"
      :style="{ fontSize: fontSize + 'px' }"
      @scroll="scrolled"
      tabindex="0"
      aria-label="Meeting transcript"
    >
      <article
        v-for="(line, index) in filtered"
        :key="`${line.t}-${index}`"
        class="transcript-line"
      >
        <div class="line-label">
          <strong :class="color(line.speaker)">{{
            line.speaker || "Speaker unknown"
          }}</strong
          ><time>{{ stamp(line.t) }}</time>
        </div>
        <p>{{ line.text }}</p>
      </article>
      <div v-if="!filtered.length" class="transcript-empty">
        <h2>
          {{
            query
              ? "No matching passages"
              : error || meeting?.status === "unavailable"
                ? "Captions are unavailable"
                : "Waiting for speech"
          }}
        </h2>
        <p>
          {{
            query
              ? "Try another word or speaker name."
              : "Captions appear here after admission and recognition. Check original recording status separately."
          }}
        </p>
      </div>
    </div>
    <div class="transcript-footer">
      <span
        >Live recognition may revise recent words. Speaker labels can be
        uncertain.</span
      >
      <button @click="resume" :class="{ following: follow && !query }">
        {{ follow && !query ? "Following live" : "Follow live ↓" }}
      </button>
    </div>
    <section v-if="meeting?.board?.bullets?.length" class="meeting-notes">
      <div class="panel-title">
        <h2>Meeting notes</h2>
        <span>AI draft · verify against transcript</span>
      </div>
      <ul>
        <li v-for="bullet in meeting.board.bullets" :key="bullet">
          {{ bullet }}
        </li>
      </ul>
    </section>
  </section>
</template>

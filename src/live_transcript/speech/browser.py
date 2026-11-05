"""Browser-native virtual microphone. No PulseAudio monitor or ALSA loopback.
The shared silent track keeps the media clock running; explicit playback adds
locally synthesized speech. Install before the meeting requests its microphone.
"""

import base64

MIC_SCRIPT = r"""(() => {
  if (window.__liveTranscriptVoice) return;
  const native = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  let context, destination, silence;
  const ready = () => {
    if (!context) {
      context = new AudioContext({sampleRate:48000});
      destination = context.createMediaStreamDestination();
      silence = context.createConstantSource(); silence.offset.value = 0;
      silence.connect(destination); silence.start();
    }
    return context.resume();
  };
  const api = window.__liveTranscriptVoice = {
    requests: [], tracks: [],
    async play(encoded, monitor=false) {
      await ready();
      const raw = atob(encoded), bytes = new Uint8Array(raw.length);
      for(let i=0;i<raw.length;i++) bytes[i]=raw.charCodeAt(i);
      const buffer = await context.decodeAudioData(bytes.buffer);
      const source = context.createBufferSource(); source.buffer = buffer;
      source.connect(destination);
      if (monitor) source.connect(context.destination);
      const started = Date.now()/1000;
      return await new Promise(resolve => {
        source.onended=()=>{source.disconnect();resolve({started,duration:buffer.duration,ended:Date.now()/1000});};
        source.start();
      });
    }
  };
  navigator.mediaDevices.getUserMedia = async options => {
    const stream = await native(options);
    if (options && options.audio) {
      await ready();
      const original=stream.getAudioTracks()[0];
      const settings=original?.getSettings()||{};
      const label=original?.label||'Live Transcript microphone';
      for(const track of stream.getAudioTracks()){stream.removeTrack(track);track.stop();}
      const virtual=destination.stream.getAudioTracks()[0].clone();
      const virtualSettings=virtual.getSettings.bind(virtual);
      // Zoom correlates opened microphone tracks with enumerateDevices().
      // A WebAudio track alone has no hardware device ID and may be discarded.
      Object.defineProperty(virtual,'getSettings',{value:()=>({...settings,...virtualSettings(),
          ...(settings.deviceId?{deviceId:settings.deviceId}:{}),...(settings.groupId?{groupId:settings.groupId}:{})})});
      Object.defineProperty(virtual,'label',{get:()=>label});
      stream.addTrack(virtual);
      api.tracks = api.tracks.filter(t=>t.readyState==='live');
      api.tracks.push(virtual);
      api.requests.push(Date.now()/1000);
    }
    return stream;
  };
})()"""


def play_wav(page, wav, monitor=False):
    encoded = base64.b64encode(wav).decode("ascii")
    # Meeting media may live in a child frame. Playing into a newly initialized
    # top-level WebAudio destination reports success but sends no microphone data.
    candidates = []
    for frame in getattr(page, "frames", [page]):
        try:
            info = frame.evaluate("""() => {
              const api=window.__liveTranscriptVoice;if(!api)return null;
              return {live:api.tracks ? api.tracks.filter(t=>t.readyState==='live' && t.enabled).length :
                      (api.requests.length?1:0),last:api.requests.at(-1)||0,requests:api.requests.length};
            }""")
            if info and info["live"]:
                candidates.append((info["last"], frame, info))
        except Exception:
            continue
    if not candidates:
        raise RuntimeError("No live meeting microphone consumes the virtual speech track")
    _, target, channel = max(candidates, key=lambda item: item[0])
    result = target.evaluate(
        """async ({encoded,monitor}) => {
        if(!window.__liveTranscriptVoice) throw new Error('Voice microphone requires a new bot join');
        return await window.__liveTranscriptVoice.play(encoded,monitor);
    }""",
        dict(encoded=encoded, monitor=monitor),
    )
    result["microphone_requests"] = channel["requests"]
    result["live_microphone_tracks"] = channel["live"]
    return result

"""Browser-native virtual microphone. No PulseAudio monitor or ALSA loopback.
The shared silent track keeps the media clock running; explicit playback adds
locally synthesized speech. Install before the meeting requests its microphone.
"""

import base64

MIC_SCRIPT = r"""(() => {
  if (window.__liveTranscriptVoice) return;
  const native = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  let context, destination, silence, setup;
  const ready = async () => {
    if (!setup) setup = (async () => {
      context = new AudioContext({sampleRate:48000});
      // This graph supplies WebRTC's microphone clock, not a speaker. Keeping
      // it independent of the hardware output avoids losing its clock when
      // PulseAudio restarts. Older engines retain the existing fallback.
      if (typeof context.setSinkId === 'function') {
        try { await context.setSinkId({type:'none'}); } catch (_) {}
      }
      destination = context.createMediaStreamDestination();
      silence = context.createConstantSource(); silence.offset.value = 0;
      silence.connect(destination); silence.start();
    })();
    await setup;
    return context.resume();
  };
  const api = window.__liveTranscriptVoice = {
    requests: [], tracks: [], outputs: new Map(),
    async recoverOutput() {
      if(context && context.state!=='closed') await context.resume();
      let reset=0;
      for(const [element,stream] of this.outputs) {
        if(!element.isConnected || element.srcObject!==stream) {this.outputs.delete(element);continue;}
        if(!stream.getAudioTracks().some(t=>t.readyState==='live'))continue;
        // Chromium may pause an active remote MediaStream when its audio
        // server disappears. Reattach the same stream; never change mute,
        // volume, track.enabled, the meeting microphone, or call admission.
        element.srcObject=null; element.srcObject=stream;
        try {await element.play();reset++;}catch(_){}
      }
      return {reset,clock:context?.state||'unused'};
    },
    health() { return {state:context?.state||'unused', time:context?.currentTime||0,
      independentClock:context?.sinkId?.type==='none',
      live:this.tracks.filter(t=>t.readyState==='live').length}; },
    async play(encoded, monitor=false) {
      await ready();
      const raw = atob(encoded), bytes = new Uint8Array(raw.length);
      for(let i=0;i<raw.length;i++) bytes[i]=raw.charCodeAt(i);
      const buffer = await context.decodeAudioData(bytes.buffer);
      const source = context.createBufferSource(); source.buffer = buffer;
      source.connect(destination);
      let monitorContext, monitorSource;
      if (monitor) {
        monitorContext = new AudioContext({sampleRate:48000});
        await monitorContext.resume();
        monitorSource = monitorContext.createBufferSource(); monitorSource.buffer=buffer;
        monitorSource.connect(monitorContext.destination);
      }
      const started = Date.now()/1000;
      return await new Promise(resolve => {
        source.onended=()=>{source.disconnect();
          if(monitorSource){try{monitorSource.stop();}catch(_){}monitorSource.disconnect();monitorContext.close().catch(()=>{});}
          resolve({started,duration:buffer.duration,ended:Date.now()/1000});};
        source.start(); if(monitorSource)monitorSource.start();
      });
    }
  };
  const remember = element => {
    const stream=element?.srcObject;
    if(stream && typeof stream.getAudioTracks==='function' && stream.getAudioTracks().length) {
      api.outputs.set(element,stream);
      for(const node of api.outputs.keys()) if(!node.isConnected) api.outputs.delete(node);
      if(api.outputs.size>32)api.outputs.delete(api.outputs.keys().next().value);
    }
  };
  document.addEventListener('playing',event=>remember(event.target),true);
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


def recover_output(page):
    """Reattach previously playing remote streams, without changing mute state."""
    reset = 0
    for frame in getattr(page, "frames", [page])[:8]:
        try:
            result = frame.evaluate("""async () => {
              const api=window.__liveTranscriptVoice;
              if(!api?.recoverOutput)return null;
              return await Promise.race([api.recoverOutput(),new Promise(resolve=>setTimeout(()=>resolve(null),1500))]);
            }""")
            if isinstance(result, dict) and type(result.get("reset")) is int:
                reset += max(0, min(32, result["reset"]))
        except Exception:
            continue
    return reset

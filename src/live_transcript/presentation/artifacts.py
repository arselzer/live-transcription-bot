"""Create derived caption artifacts without replacing a good file on failure."""

from pathlib import Path
import subprocess
from live_transcript.core.files import atomic_destination


def subtitle_time(value):
    milliseconds = max(0, round(float(value) * 1000))
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d},{fraction:03d}"


def subtitles(utterances, speaker):
    parts = []
    for index, row in enumerate(utterances):
        end = row.get(
            "end", utterances[index + 1]["t"] if index + 1 < len(utterances) else row["t"] + 5
        )
        name = row.get("voice_name") or speaker(row["t"])
        text = (name + ": " if name else "") + row["text"]
        parts.append(f"{index + 1}\n{subtitle_time(row['t'])} --> {subtitle_time(end)}\n{text}\n\n")
    return "".join(parts)


def render_video(wav, srt, runner=None):
    runner = runner or subprocess.run
    stem = Path(wav).with_suffix("")
    screen = Path(str(stem) + "-screen.mp4")
    if not screen.is_file() or screen.stat().st_size <= 10000:
        return dict(status="not_available")
    mkv = Path(str(stem) + ".mkv")
    with atomic_destination(mkv) as temporary:
        runner(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(screen),
                "-i",
                str(wav),
                "-i",
                str(srt),
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:s",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-c:s",
                "srt",
                str(temporary),
            ],
            check=True,
            timeout=600,
        )
        if not temporary.stat().st_size:
            raise ValueError("Empty video output")
    burned = Path(str(stem) + "-burned.mp4")
    # Use a constant relative subtitle filename in a private directory. FFmpeg
    # filter syntax must not interpret punctuation from arbitrary source paths.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="subtitle-render-") as directory:
        subtitle_copy = Path(directory) / "captions.srt"
        subtitle_copy.write_bytes(Path(srt).read_bytes())
        with atomic_destination(burned) as temporary:
            runner(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(mkv.resolve()),
                    "-vf",
                    "subtitles=captions.srt:force_style='FontSize=20,MarginV=40,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2'",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "26",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    str(temporary.resolve()),
                ],
                cwd=directory,
                check=True,
                timeout=1800,
            )
            if not temporary.stat().st_size:
                raise ValueError("Empty burned video output")
    return dict(status="complete", mkv=str(mkv), burned=str(burned))

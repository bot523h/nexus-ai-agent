#!/usr/bin/env python3
"""
build_episode.py — ساخت فیلم (animatic سینمایی) از روی یک فایل «مانیفست» JSON.

هر صحنه = یک تصویر ثابت + حرکت آرام دوربین (Ken Burns) + یک یا چند کلیپ صوتی
(نریشن/دیالوگ) که در زمان مشخصی از صحنه پخش می‌شوند. صحنه‌ها با crossfade
به هم وصل می‌شوند و در پایان موسیقی متن + میکس صدا + خروجی MP4 (H.264/AAC).

استفاده:
    python3 build_episode.py manifest.json --out out.mp4 [--keep-temp]

ساختار مانیفست: film_studio/README.md را ببینید.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe")


def die(msg: str) -> None:
    print(f"✖ {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"✖ ffmpeg failed at: {what}", file=sys.stderr)
        print(" ".join(cmd), file=sys.stderr)
        print(proc.stderr[-4000:], file=sys.stderr)
        raise SystemExit(1)


MOTIONS = {
    # z, x, y به صورت تابعی از on (شماره فریم خروجی) و N (تعداد فریم صحنه)
    "zoom_in": ("1+{rise}*on/{last}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
    "zoom_out": ("{top}-{rise}*on/{last}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
    "pan_left": ("{flat}", "(iw-iw/zoom)*(1-on/{last})", "(ih-ih/zoom)/2"),
    "pan_right": ("{flat}", "(iw-iw/zoom)*on/{last}", "(ih-ih/zoom)/2"),
    "pan_up": ("{flat}", "(iw-iw/zoom)/2", "(ih-ih/zoom)*(1-on/{last})"),
    "pan_down": ("{flat}", "(iw-iw/zoom)/2", "(ih-ih/zoom)*on/{last}"),
    "still": ("{flat}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
}


def motion_expr(name: str, frames: int) -> tuple[str, str, str]:
    if name not in MOTIONS:
        die(f"motion ناشناخته: {name} (مجاز: {', '.join(MOTIONS)})")
    rise, flat, top = 0.10, 1.06, 1.16
    z, x, y = MOTIONS[name]
    return (
        z.format(rise=rise, flat=flat, top=top, last=max(frames - 1, 1)),
        x.format(last=max(frames - 1, 1)),
        y.format(last=max(frames - 1, 1)),
    )


def probe_duration(path: Path) -> float:
    """مدت فایل را با ffprobe (و اگر نبود با خروجی ffmpeg -i) برمی‌گرداند."""
    if FFPROBE:
        cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip())
    proc = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr)
    if not m:
        die(f"مدت {path} قابل خواندن نبود (ffprobe نصب نیست؟)")
    h, mi, se = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(se)


def build_scene_clip(scene: dict, idx: int, W: int, H: int, FPS: int, tmp: Path) -> tuple[Path, float]:
    img = Path(scene["image"])
    if not img.is_absolute():
        img = Path(scene.get("_base", ".")) / img
    if not img.exists():
        die(f"تصویر صحنه {idx} پیدا نشد: {img}")

    duration = float(scene["duration"])
    frames = max(int(round(duration * FPS)), 2)
    z, x, y = motion_expr(scene.get("motion", "zoom_in"), frames)

    # تصویر را ۲ برابر بزرگ می‌کنیم تا پن/زوم نرم و بدون لرزش باشد.
    # از lanczos برای بزرگ‌نمایی و unsharp ملایم استفاده می‌کنیم تا فریم ۱۰۸۰p تیزتر شود.
    sharpen = float(scene.get("sharpen", 0.55))
    grain = float(scene.get("grain", 0.0))
    vf = (
        f"scale={W*2}:{H*2}:flags=lanczos:force_original_aspect_ratio=increase,"
        f"crop={W*2}:{H*2},setsar=1,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}"
    )
    if sharpen > 0:
        vf += f",unsharp=5:5:{sharpen}:5:5:0.0"
    if grain > 0:
        vf += f",noise=alls={grain}:allf=t+u"
    vf += ",format=yuv420p"
    out = tmp / f"scene_{idx:03d}.mp4"
    run(
        [
            FFMPEG, "-y", "-loglevel", "error",
            "-loop", "1", "-i", str(img),
            "-t", f"{duration:.3f}", "-vf", vf,
            "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", str(out),
        ],
        f"رندر صحنه {idx}",
    )
    return out, duration


def build_audio(manifest: dict, tmp: Path, total: float, base: Path, W: int, H: int) -> Path | None:
    """میکس همه کلیپ‌های صوتی صحنه‌ها روی هم + موسیقی متن."""
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []

    t0 = 0.0
    scene_starts: list[float] = []
    for scene in manifest["scenes"]:
        scene_starts.append(t0)
        t0 += float(scene["duration"]) - float(manifest.get("crossfade", 0.6))
    # شروع صحنه i = sum(d_0..d_{i-1}) - i*crossfade  (با crossfade صحنه‌ها روی هم می‌افتند)

    idx = 0
    for si, scene in enumerate(manifest["scenes"]):
        for clip in scene.get("audio", []) or []:
            p = Path(clip["file"])
            if not p.is_absolute():
                p = base / p
            if not p.exists():
                die(f"فایل صوتی پیدا نشد: {p}")
            at = scene_starts[si] + float(clip.get("offset", scene.get("audio_offset", 0.4)))
            gain = float(clip.get("gain_db", 0.0))
            inputs += ["-i", str(p)]
            filters.append(
                f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={gain}dB,adelay={int(at*1000)}|{int(at*1000)}[a{idx}]"
            )
            labels.append(f"[a{idx}]")
            idx += 1

    music = manifest.get("music")
    music_label = None
    if music:
        mp = Path(music)
        if not mp.is_absolute():
            mp = base / mp
        if mp.exists():
            mgain = float(manifest.get("music_gain_db", -22.0))
            inputs += ["-stream_loop", "-1", "-i", str(mp)]
            filters.append(
                f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={mgain}dB,atrim=0:{total:.3f},afade=t=in:st=0:d=1.2,"
                f"afade=t=out:st={max(total-2.0,0):.3f}:d=2.0[amusic]"
            )
            music_label = "[amusic]"
            idx += 1
        else:
            print(f"⚠ موسیقی پیدا نشد، بدون موسیقی ادامه می‌دهیم: {mp}")

    out = tmp / "mix.m4a"
    if not inputs:
        # بی‌صدا: یک ترک سکوت می‌سازیم تا خروجی همیشه صدا داشته باشد
        run(
            [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"anullsrc=r=48000:cl=stereo", "-t", f"{total:.3f}",
             "-c:a", "aac", "-b:a", "192k", str(out)],
            "ساخت ترک سکوت",
        )
        return out

    if music_label:
        labels.append(music_label)
    if len(labels) == 1:
        mix = f"{labels[0]}anull[aout]"
    else:
        mix = (
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
              f"alimiter=limit=0.95,aresample=48000[aout]"
        )
    filters.append(mix)
    run(
        [FFMPEG, "-y", "-loglevel", "error", *inputs,
         "-filter_complex", ";".join(filters),
         "-map", "[aout]", "-t", f"{total:.3f}",
         "-c:a", "aac", "-b:a", "192k", str(out)],
        "میکس صدا",
    )
    return out


def write_srt(manifest: dict, out_path: Path) -> Path | None:
    """اگر مانیفست «subtitles» داشته باشد، فایل .srt کنار ویدیو می‌نویسد."""
    subs = manifest.get("subtitles")
    if not subs:
        return None

    def ts(t: float) -> str:
        h, rem = divmod(max(t, 0.0), 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"

    lines = []
    for i, item in enumerate(subs, 1):
        lines += [str(i), f"{ts(float(item['start']))} --> {ts(float(item['end']))}",
                  str(item["text"]).strip(), ""]
    path = out_path.with_suffix(".srt")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()
    if not FFMPEG:
        die("ffmpeg پیدا نشد. می‌توانی FFMPEG_BIN را ست کنی یا imageio-ffmpeg نصب کنی.")

    mpath = Path(args.manifest).resolve()
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    base = mpath.parent
    for sc in manifest["scenes"]:
        sc["_base"] = str(base)

    W = int(manifest.get("width", 1920))
    H = int(manifest.get("height", 1080))
    FPS = int(manifest.get("fps", 30))
    xfade = float(manifest.get("crossfade", 0.6))
    out_path = Path(args.out or manifest.get("output") or (base / "episode.mp4")).resolve()

    tmp = Path(tempfile.mkdtemp(prefix="filmstudio_"))
    try:
        clips, durs = [], []
        for i, scene in enumerate(manifest["scenes"]):
            c, d = build_scene_clip(scene, i, W, H, FPS, tmp)
            clips.append(c)
            durs.append(d)
            print(f"  ✓ صحنه {i+1}/{len(manifest['scenes'])} آماده شد ({d:.1f}s)")

        total = sum(durs) - (len(durs) - 1) * xfade

        # زنجیره crossfade
        merged = tmp / "video.mp4"
        if len(clips) == 1:
            shutil.copy(clips[0], merged)
        else:
            inputs = []
            for c in clips:
                inputs += ["-i", str(c)]
            fc, offset, prev = [], durs[0] - xfade, "[0:v]"
            for i in range(1, len(clips)):
                lbl = f"[v{i}]"
                fc.append(
                    f"{prev}[{i}:v]xfade=transition=fade:duration={xfade}:offset={offset:.3f}{lbl}"
                )
                prev = lbl
                offset += durs[i] - xfade
            fc.append(f"{prev}format=yuv420p,fade=t=in:st=0:d={float(manifest.get('fade_in',0.8))},"
                      f"fade=t=out:st={max(total-float(manifest.get('fade_out',1.2)),0):.3f}:"
                      f"d={float(manifest.get('fade_out',1.2))}[vout]")
            run(
                [FFMPEG, "-y", "-loglevel", "error", *inputs,
                 "-filter_complex", ";".join(fc), "-map", "[vout]",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                 "-pix_fmt", "yuv420p", "-r", str(FPS), str(merged)],
                "اتصال صحنه‌ها با crossfade",
            )

        audio = build_audio(manifest, tmp, total, base, W, H)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        title = manifest.get("title", "")
        run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", str(merged), "-i", str(audio),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", "-shortest",
             "-metadata", f"title={title}", str(out_path)],
            "ترکیب نهایی تصویر و صدا",
        )
        srt = write_srt(manifest, out_path)
        print(f"\n✔ فیلم آماده شد: {out_path}")
        if srt:
            print(f"  زیرنویس: {srt}")
        print(f"  مدت: {total:.1f} ثانیه | {W}x{H} @ {FPS}fps | {len(clips)} صحنه")
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"  (فایل‌های موقت در {tmp})")


if __name__ == "__main__":
    main()

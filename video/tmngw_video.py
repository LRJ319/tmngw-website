#!/usr/bin/env python3
"""TMNGW video pipeline: folder of clips -> TikTok, IG Reels, faceless YouTube + thumbnails.

Usage:
  python3 video/tmngw_video.py init   <project>   # write brief.yml listing the clips
  python3 video/tmngw_video.py review <project>   # contact sheets + clip stats for picking order/trims
  python3 video/tmngw_video.py build  <project> [--only tiktok,reels,youtube] [--transcribe]

Requires: ffmpeg/ffprobe on PATH, Python 3.9+, Pillow, numpy, PyYAML.
Optional: faster-whisper (for --transcribe auto-captions).
See docs/video-creation-protocol.md for the full protocol.
"""
import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

HERE = Path(__file__).resolve().parent
FONTS = HERE / "assets" / "fonts"
TITLE_FONT = FONTS / "BebasNeue-Regular.ttf"
BODY_FONT = FONTS / "Inter-Bold.ttf"

GOLD = (200, 146, 30)
BLUE = (29, 120, 196)
INK = (8, 8, 8)
WHITE = (240, 240, 240)

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
FPS = 30

# Each platform: canvas, default length cap, caption placement, safe zones (px from edge).
PLATFORMS = {
    "tiktok": dict(w=1080, h=1920, max_seconds=60, safe_top=160, safe_bottom=520, cap_margin=600),
    "reels": dict(w=1080, h=1920, max_seconds=90, safe_top=220, safe_bottom=440, cap_margin=560),
    "youtube": dict(w=1920, h=1080, max_seconds=None, safe_top=80, safe_bottom=80, cap_margin=90),
}

DEFAULT_BRIEF = {
    "title": "",
    "hook": "",
    "thumbnail_text": "",
    "thumbnail_label": "",
    "thumbnail_frame": None,
    "clips": None,
    "music": {"file": None, "volume": 0.18},
    "captions": True,
    "platforms": {
        "tiktok": {"enabled": True, "max_seconds": 60, "clips": None},
        "reels": {"enabled": True, "max_seconds": 90, "clips": None},
        "youtube": {
            "enabled": True,
            "max_seconds": None,
            "clips": None,
            "voiceover": None,
            "ambient_volume": 0.12,
            "intro": None,
            "outro": None,
        },
    },
    "post": {"description": "", "hashtags": [], "tiktok": "", "reels": "", "youtube": ""},
}


# ---------------------------------------------------------------- helpers

def log(msg):
    print(f"[tmngw] {msg}", flush=True)


def run(cmd, quiet=True):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr[-4000:])
        raise SystemExit(f"command failed: {' '.join(map(str, cmd[:6]))} ...")
    return res


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    dur = float(data["format"].get("duration", 0) or 0)
    info = {"duration": dur, "has_audio": a is not None, "has_video": v is not None}
    if v:
        w, h = int(v["width"]), int(v["height"])
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        rot = int(v.get("tags", {}).get("rotate", rot))
        if abs(rot) in (90, 270):
            w, h = h, w
        num, den = (v.get("avg_frame_rate") or "0/1").split("/")
        info.update(width=w, height=h, fps=round(float(num) / float(den), 2) if float(den) else 0)
    return info


def natural_key(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(p))]


def find_clips(project):
    base = project / "clips" if (project / "clips").is_dir() else project
    return sorted([p for p in base.iterdir() if p.suffix.lower() in VIDEO_EXT], key=natural_key)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_brief(project):
    f = project / "brief.yml"
    data = yaml.safe_load(f.read_text()) if f.exists() else {}
    return deep_merge(DEFAULT_BRIEF, data or {})


def resolve(project, name):
    if not name:
        return None
    for cand in (project / name, project / "clips" / name, project / "music" / name):
        if cand.exists():
            return cand
    raise SystemExit(f"file not found in project: {name}")


def esc(path):
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def loudness(path):
    res = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true",
                          "-f", "null", "-"], capture_output=True, text=True)
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", res.stderr)
    tp = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", res.stderr)
    return (float(i[-1]) if i else None, float(tp[-1]) if tp else None)


# ---------------------------------------------------------------- captions

def parse_srt(path):
    def ts(s):
        h, m, rest = s.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)

    events = []
    for block in re.split(r"\n\s*\n", Path(path).read_text(encoding="utf-8-sig").strip()):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->")
        text = " ".join(lines[idx + 1:]).strip()
        if text:
            events.append((ts(a), ts(b), re.sub(r"<[^>]+>", "", text)))
    return events


def chunk_events(events, max_words):
    """Split long caption lines into short punchy chunks, timed by word share."""
    out = []
    for start, end, text in events:
        words = text.split()
        if len(words) <= max_words:
            out.append((start, end, text))
            continue
        n = math.ceil(len(words) / max_words)
        per = (end - start) / len(words)
        for i in range(n):
            part = words[i * max_words:(i + 1) * max_words]
            s = start + i * max_words * per
            out.append((s, s + len(part) * per, " ".join(part)))
    return out


def ass_time(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def write_ass(events, spec, path):
    vertical = spec["h"] > spec["w"]
    size = 78 if vertical else 58
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {spec['w']}
PlayResY: {spec['h']}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Inter,{size},&H00F0F0F0,&H00F0F0F0,&H00080808,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,90,90,{spec['cap_margin']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{t.upper() if vertical else t}"
             for s, e, t in events if e > s]
    Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def transcribe_missing(project, clips):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper not installed; skipping --transcribe (pip install faster-whisper)")
        return
    model = WhisperModel("small", compute_type="int8")
    capdir = project / "captions"
    capdir.mkdir(exist_ok=True)
    for src in clips:
        dest = capdir / f"{src.stem}.srt"
        if dest.exists():
            continue
        log(f"transcribing {src.name}")
        segs, _ = model.transcribe(str(src), vad_filter=True)
        blocks = []
        for i, s in enumerate(segs, 1):
            f = lambda t: f"{int(t//3600):02d}:{int(t%3600//60):02d}:{t%60:06.3f}".replace(".", ",")
            blocks.append(f"{i}\n{f(s.start)} --> {f(s.end)}\n{s.text.strip()}\n")
        dest.write_text("\n".join(blocks), encoding="utf-8")


# ---------------------------------------------------------------- segments

def clip_entries(project, brief, platform):
    pconf = brief["platforms"][platform]
    raw = pconf.get("clips") or brief.get("clips")
    if not raw:
        raw = [{"file": p.name} for p in find_clips(project)]
    entries = []
    for item in raw:
        e = {"file": item} if isinstance(item, str) else dict(item)
        if platform == "youtube" and e.get("faceless") is False:
            continue
        e["path"] = resolve(project, e["file"])
        entries.append(e)
    return entries


def video_filter(info, spec, entry):
    W, H = spec["w"], spec["h"]
    fit = entry.get("fit", "auto")
    cx = float(entry.get("crop_x", 0.5))
    if fit == "auto":
        src_ar = info["width"] / info["height"]
        fit = "crop" if abs(src_ar - W / H) / (W / H) < 0.2 else "blur"
    tail = f"fps={FPS},setsar=1,format=yuv420p"
    if fit == "crop":
        return (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H}:(iw-{W})*{cx}:(ih-{H})/2,{tail}[v]")
    return (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=24:2,eq=brightness=-0.10:saturation=0.8[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{tail}[v]")


def render_segment(entry, spec, dest):
    info = probe(entry["path"])
    start = float(entry.get("in", 0) or 0)
    end = float(entry.get("out") or info["duration"])
    dur = max(0.1, end - start)
    vol = float(entry.get("volume", 1.0))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", f"{start}", "-t", f"{dur}", "-i", str(entry["path"])]
    if info["has_audio"]:
        a = f"[0:a]aresample=48000,aformat=channel_layouts=stereo,volume={vol},"
    else:
        cmd += ["-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=48000:cl=stereo"]
        a = "[1:a]"
    fo = max(0.0, dur - 0.04)
    a += f"afade=t=in:d=0.03,afade=t=out:st={fo:.3f}:d=0.04,apad[a]"
    fc = video_filter(info, spec, entry) + ";" + a
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{dur}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
            "-ar", "48000", str(dest)]
    run(cmd)
    return dur, start


def build_sequence(project, brief, platform, work):
    spec = PLATFORMS[platform]
    pconf = brief["platforms"][platform]
    entries = clip_entries(project, brief, platform)
    if platform == "youtube":
        if pconf.get("intro"):
            entries.insert(0, {"file": pconf["intro"], "path": resolve(project, pconf["intro"]), "_brand": True})
        if pconf.get("outro"):
            entries.append({"file": pconf["outro"], "path": resolve(project, pconf["outro"]), "_brand": True})
    if not entries:
        raise SystemExit(f"{platform}: no clips to use")
    segs, events, t = [], [], 0.0
    for i, e in enumerate(entries):
        dest = work / f"{platform}_{i:03d}.mp4"
        log(f"{platform}: segment {i + 1}/{len(entries)} {e['file']}")
        dur, start = render_segment(e, spec, dest)
        segs.append(dest)
        srt = project / "captions" / f"{Path(e['file']).stem}.srt"
        if srt.exists() and not e.get("_brand"):
            for s, en, txt in parse_srt(srt):
                s2, e2 = max(s, start), min(en, start + dur)
                if e2 > s2:
                    events.append((t + s2 - start, t + e2 - start, txt))
        t += dur
    listing = work / f"{platform}_list.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in segs))
    seq = work / f"{platform}_seq.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(seq)])
    return seq, t, events, entries


# ---------------------------------------------------------------- final render

def render_final(project, brief, platform, seq, seq_dur, events, work, out, with_music=True):
    spec = PLATFORMS[platform]
    pconf = brief["platforms"][platform]
    W, H = spec["w"], spec["h"]
    vo = resolve(project, pconf.get("voiceover")) if platform == "youtube" and pconf.get("voiceover") else None
    music = resolve(project, brief["music"].get("file")) if with_music and brief["music"].get("file") else None
    cap = pconf.get("max_seconds", spec["max_seconds"])

    target = probe(vo)["duration"] if vo else seq_dur
    warnings = []
    if cap and target > cap:
        warnings.append(f"{platform}: {target:.1f}s trimmed to {cap}s cap; set platforms.{platform}.clips to choose the cut")
        target = float(cap)

    cmd = ["ffmpeg", "-y", "-hide_banner"]
    cmd += (["-stream_loop", "-1"] if vo and target > seq_dur else []) + ["-i", str(seq)]
    idx = 1
    vo_i = mu_i = None
    if vo:
        cmd += ["-i", str(vo)]
        vo_i, idx = idx, idx + 1
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        mu_i, idx = idx, idx + 1

    voice_chain = "highpass=f=80,afftdn=nr=8,acompressor=threshold=-20dB:ratio=3:attack=5:release=120:makeup=2"
    parts = []
    if vo:
        parts.append(f"[{vo_i}:a]aresample=48000,aformat=channel_layouts=stereo,{voice_chain}[lead]")
        amb = float(pconf.get("ambient_volume", 0.12))
        parts.append(f"[0:a]volume={amb}[amb]")
    else:
        parts.append(f"[0:a]{voice_chain}[lead]")
    mix_inputs = ["[l1]"]
    parts.append("[lead]asplit=2[l1][l2]")
    if vo:
        mix_inputs.append("[amb]")
    if music:
        mv = float(brief["music"].get("volume", 0.18))
        parts.append(f"[{mu_i}:a]aresample=48000,aformat=channel_layouts=stereo,volume={mv}[m]")
        parts.append("[m][l2]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=450[md]")
        mix_inputs.append("[md]")
    else:
        parts.append("[l2]anullsink")
    fo = max(0.0, target - 1.0)
    parts.append(f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:normalize=0,"
                 f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                 f"afade=t=in:d=0.25,afade=t=out:st={fo:.2f}:d=1[aout]")

    vchain = "[0:v]"
    filters = []
    hook = (brief.get("hook") or "").strip()
    if hook:
        vertical = H > W
        hf = work / f"{platform}_hook.txt"
        hf.write_text(wrap(hook.upper(), 16 if vertical else 28))
        size = 92 if vertical else 84
        y = spec["safe_top"] + (60 if vertical else 40)
        filters.append(f"drawtext=fontfile='{esc(TITLE_FONT)}':textfile='{esc(hf)}':fontsize={size}:"
                       f"fontcolor=0x080808:line_spacing=6:box=1:boxcolor=0xC8921E@0.96:boxborderw=26:"
                       f"x=(w-text_w)/2:y={y}:enable='lt(t,3.5)'")
    evs = []
    if brief.get("captions", True):
        vsrt = project / "voiceover.srt"
        if vo:  # clip captions don't line up under a voiceover; use voiceover.srt
            evs = chunk_events(parse_srt(vsrt), 9) if vsrt.exists() else []
        else:
            evs = chunk_events(events, 4 if H > W else 9)
    if evs:
        ass = work / f"{platform}.ass"
        write_ass(evs, spec, ass)
        filters.append(f"subtitles='{esc(ass)}':fontsdir='{esc(FONTS)}'")
    filters.append(f"fade=t=in:d=0.25,fade=t=out:st={fo:.2f}:d=1")
    parts.append(vchain + ",".join(filters) + "[vout]")

    script = work / f"{platform}_graph.txt"
    script.write_text(";\n".join(parts))
    cmd += ["-filter_complex_script", str(script), "-map", "[vout]", "-map", "[aout]", "-t", f"{target:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(out)]
    run(cmd)
    return target, warnings


# ---------------------------------------------------------------- thumbnails

def grab_frame(path, t, width=None):
    vf = ["-vf", f"scale={width}:-2"] if width else []
    res = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", *vf,
                          "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if res.returncode != 0 or not res.stdout:
        return None
    from io import BytesIO
    return Image.open(BytesIO(res.stdout)).convert("RGB")


def frame_score(img):
    a = np.asarray(img.convert("L"), dtype=np.float32) / 255.0
    lap = a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    sharp = float(lap.var()) * 1000
    bright = 1 - abs(float(a.mean()) - 0.5) * 2
    rgb = np.asarray(img, dtype=np.float32)
    rg = rgb[..., 0] - rgb[..., 1]
    yb = 0.5 * (rgb[..., 0] + rgb[..., 1]) - rgb[..., 2]
    color = (math.hypot(rg.std(), yb.std()) + 0.3 * math.hypot(rg.mean(), yb.mean())) / 100
    contrast = float(a.std()) * 4
    return sharp * 0.5 + bright * 1.0 + color * 0.8 + contrast * 0.7


def pick_frames(entries, work, n=6):
    cands = []
    for e in entries:
        if e.get("_brand"):
            continue
        info = probe(e["path"])
        s = float(e.get("in", 0) or 0)
        en = float(e.get("out") or info["duration"])
        steps = max(3, min(10, int((en - s) / 2)))
        for k in range(steps):
            t = s + (en - s) * (k + 0.5) / steps
            img = grab_frame(e["path"], t, 320)
            if img is not None:
                cands.append((frame_score(img), e["path"], t, img))
    cands.sort(key=lambda c: c[0], reverse=True)
    chosen = []
    for c in cands:  # keep candidates spread out in time
        if all(not (c[1] == o[1] and abs(c[2] - o[2]) < 3) for o in chosen):
            chosen.append(c)
        if len(chosen) == n:
            break
    if chosen:
        sheet = Image.new("RGB", (3 * 320, 2 * 220), INK)
        d = ImageDraw.Draw(sheet)
        f = ImageFont.truetype(str(BODY_FONT), 14)
        for i, (sc, p, t, img) in enumerate(chosen):
            thumb = img.copy()
            thumb.thumbnail((320, 200))
            x, y = (i % 3) * 320, (i // 3) * 220
            sheet.paste(thumb, (x, y))
            d.text((x + 6, y + 202), f"#{i + 1} {p.name} @ {t:.1f}s", fill=WHITE, font=f)
        sheet.save(work / "thumb_candidates.jpg", quality=88)
    return chosen


def cover(img, W, H, cx=0.5):
    s = max(W / img.width, H / img.height)
    img = img.resize((math.ceil(img.width * s), math.ceil(img.height * s)), Image.LANCZOS)
    x = int((img.width - W) * cx)
    y = (img.height - H) // 2
    return img.crop((x, y, x + W, y + H))


def layout_text(draw, text, max_w, max_h, max_lines, size_hi, size_lo=40):
    tokens = []
    for m in re.finditer(r"\*([^*]+)\*|(\S+)", text.upper()):
        if m.group(1):
            tokens += [(w, True) for w in m.group(1).split()]
        else:
            tokens.append((m.group(2), False))
    for size in range(size_hi, size_lo - 1, -4):
        font = ImageFont.truetype(str(TITLE_FONT), size)
        space = font.getlength(" ")
        lines, cur, cur_w = [], [], 0
        for tok in tokens:
            w = font.getlength(tok[0])
            if cur and cur_w + space + w > max_w:
                lines.append(cur)
                cur, cur_w = [tok], w
            else:
                cur_w = cur_w + (space if cur else 0) + w
                cur.append(tok)
        if cur:
            lines.append(cur)
        lh = int(size * 0.95)
        widest = max(sum(font.getlength(t[0]) for t in l) + space * (len(l) - 1) for l in lines)
        if len(lines) <= max_lines and lh * len(lines) <= max_h and widest <= max_w:
            return font, lines, lh, space
    return font, lines, lh, space


def draw_title(img, text, box, align="left", size_hi=220, max_lines=3):
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(img)
    font, lines, lh, space = layout_text(d, text, x1 - x0, y1 - y0, max_lines, size_hi)
    total = lh * len(lines)
    y = y1 - total
    stroke = max(4, font.size // 22)
    for line in lines:
        lw = sum(font.getlength(t[0]) for t in line) + space * (len(line) - 1)
        x = x0 if align == "left" else x0 + (x1 - x0 - lw) / 2
        for word, gold in line:
            d.text((x, y), word, font=font, fill=GOLD if gold else WHITE, stroke_width=stroke, stroke_fill=INK)
            x += font.getlength(word) + space
        y += lh
    return y1 - total


def gradient(W, H, direction):
    g = np.zeros((H, W), dtype=np.float32)
    if direction == "left":
        ramp = np.clip(1.15 - np.linspace(0, 1, W) * 1.6, 0, 1)
        g[:] = ramp[None, :]
    else:  # bottom
        ramp = np.clip((np.linspace(0, 1, H) - 0.25) * 1.5, 0, 1)
        g[:] = ramp[:, None]
    alpha = Image.fromarray((g * 215).astype(np.uint8), "L")
    layer = Image.new("RGBA", (W, H), INK + (255,))
    layer.putalpha(alpha)
    return layer


def make_thumbnail(frame, text, label, W, H, dest, cx=0.5):
    img = cover(frame, W, H, cx)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.18)
    horizontal = W > H
    img = Image.alpha_composite(img.convert("RGBA"), gradient(W, H, "left" if horizontal else "bottom")).convert("RGB")
    d = ImageDraw.Draw(img)
    if horizontal:
        box = (int(W * 0.05), int(H * 0.12), int(W * 0.62), int(H * 0.86))
        top = draw_title(img, text, box, "left", size_hi=230) if text else box[3]
        bar_x, bar_y = box[0], box[3] + 18
        d.rectangle((bar_x, bar_y, bar_x + int(W * 0.14), bar_y + 12), fill=GOLD)
    else:
        # keep text inside the centre 3:4 area so the IG profile grid crop doesn't cut it
        safe_h = int(W * 4 / 3)
        sy0 = (H - safe_h) // 2
        box = (int(W * 0.07), sy0 + int(safe_h * 0.35), int(W * 0.93), sy0 + int(safe_h * 0.90))
        top = draw_title(img, text, box, "center", size_hi=210, max_lines=4) if text else box[3]
        d.rectangle((W // 2 - 90, box[3] + 20, W // 2 + 90, box[3] + 32), fill=GOLD)
    if label:
        lf = ImageFont.truetype(str(BODY_FONT), 34 if horizontal else 40)
        lw = d.textlength(label.upper(), font=lf)
        x, y = (box[0], top - 80) if horizontal else ((W - lw - 40) / 2, top - 90)
        d.rectangle((x, y, x + lw + 40, y + 60), fill=BLUE)
        d.text((x + 20, y + 8), label.upper(), font=lf, fill=WHITE)
    if dest.suffix == ".jpg":
        q = 92
        img.save(dest, quality=q, optimize=True)
        while dest.stat().st_size > 1_900_000 and q > 60:  # YouTube limit is 2 MB
            q -= 6
            img.save(dest, quality=q, optimize=True)
    else:
        img.save(dest)


def thumbnails(project, brief, entries_by_platform, work, out):
    text = brief.get("thumbnail_text") or brief.get("title") or ""
    label = brief.get("thumbnail_label") or ""
    tf = brief.get("thumbnail_frame")
    made = []
    for platform, entries in entries_by_platform.items():
        if tf:
            path, t = resolve(project, tf["file"]), float(tf.get("time", 0))
            cx = float(tf.get("crop_x", 0.5))
        else:
            chosen = pick_frames(entries, work)
            if not chosen:
                continue
            shutil.copy(work / "thumb_candidates.jpg", out / f"{platform}_thumb_candidates.jpg")
            _, path, t, _ = chosen[0]
            cx = next((float(e.get("crop_x", 0.5)) for e in entries if e["path"] == path), 0.5)
        frame = grab_frame(path, t)
        spec = PLATFORMS[platform]
        if platform == "youtube":
            dest = out / "youtube_thumbnail.jpg"
            make_thumbnail(frame, text, label, 1280, 720, dest, cx)
        else:
            dest = out / f"{platform}_cover.jpg"
            make_thumbnail(frame, text, label, spec["w"], spec["h"], dest, cx)
        made.append((platform, dest, f"{path.name} @ {t:.1f}s"))
    return made


# ---------------------------------------------------------------- commands

def cmd_init(project):
    clips = find_clips(project)
    if not clips:
        raise SystemExit(f"no video files found in {project} or {project}/clips")
    brief = project / "brief.yml"
    if brief.exists():
        raise SystemExit(f"{brief} already exists")
    lines = [f"  - file: {c.name}\n    # in: 0\n    # out: {probe(c)['duration']:.1f}\n" for c in clips]
    tpl = (HERE / "brief.template.yml").read_text()
    brief.write_text(tpl.replace("CLIPS_PLACEHOLDER\n", "".join(lines)))
    log(f"wrote {brief}")


def cmd_review(project):
    clips = find_clips(project)
    rev = project / "output" / "review"
    rev.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in clips:
        info = probe(c)
        i, tp = loudness(c) if info["has_audio"] else (None, None)
        info.update(file=c.name, lufs=i, true_peak=tp)
        rows.append(info)
        n = 8
        sheet = Image.new("RGB", (4 * 360, 2 * 230), INK)
        d = ImageDraw.Draw(sheet)
        f = ImageFont.truetype(str(BODY_FONT), 16)
        for k in range(n):
            t = info["duration"] * (k + 0.5) / n
            img = grab_frame(c, t, 360)
            if img:
                img.thumbnail((360, 203))
                x, y = (k % 4) * 360, (k // 4) * 230
                sheet.paste(img, (x, y))
                d.text((x + 6, y + 206), f"{t:.1f}s", fill=WHITE, font=f)
        sheet.save(rev / f"{c.stem}_sheet.jpg", quality=85)
        log(f"{c.name}: {info['duration']:.1f}s {info.get('width')}x{info.get('height')} "
            f"audio={'yes' if info['has_audio'] else 'NO'} loudness={i} LUFS")
    (rev / "clips.json").write_text(json.dumps(rows, indent=2))
    log(f"contact sheets + clips.json in {rev}")


def cmd_build(project, only, transcribe):
    brief = load_brief(project)
    out = project / "output"
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    if transcribe:
        transcribe_missing(project, [e["path"] for e in clip_entries(project, brief, "tiktok")])
    platforms = [p for p in PLATFORMS if brief["platforms"][p].get("enabled", True) and (not only or p in only)]
    results, warnings, entries_by = [], [], {}
    for p in platforms:
        seq, dur, events, entries = build_sequence(project, brief, p, work)
        entries_by[p] = entries
        dest = out / f"{p}.mp4"
        log(f"{p}: final render")
        length, w = render_final(project, brief, p, seq, dur, events, work, dest)
        warnings += w
        results.append((p, dest, length))
        if p in ("tiktok", "reels") and brief["music"].get("file"):
            clean = out / f"{p}_no_music.mp4"
            render_final(project, brief, p, seq, dur, events, work, clean, with_music=False)
            results.append((p + " (no music)", clean, length))
    thumbs = thumbnails(project, brief, entries_by, work, out)
    write_post_copy(brief, out)
    report = ["# Build report", ""]
    report.append("| Output | Length | Loudness | True peak |\n|---|---|---|---|")
    for name, path, length in results:
        i, tp = loudness(path)
        report.append(f"| {path.name} | {length:.1f}s | {i} LUFS | {tp} dBFS |")
    report += ["", "## Thumbnails", ""] + [f"- {d.name} — frame {src}" for _, d, src in thumbs]
    if warnings:
        report += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    (out / "report.md").write_text("\n".join(report) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    print("\n".join(report))


def write_post_copy(brief, out):
    post = brief.get("post") or {}
    tags = " ".join(f"#{t.lstrip('#')}" for t in post.get("hashtags") or [])
    title = brief.get("title") or ""
    desc = post.get("description") or ""
    body = [f"# Post copy — {title}", ""]
    for p, label in (("tiktok", "TikTok"), ("reels", "Instagram Reels"), ("youtube", "YouTube")):
        cap = post.get(p) or desc
        body += [f"## {label}", ""]
        if p == "youtube":
            body += [f"**Title:** {title}", ""]
        body += [cap, "", tags, ""]
    (out / "post_copy.md").write_text("\n".join(body))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "review", "build"])
    ap.add_argument("project", type=Path)
    ap.add_argument("--only", help="comma list: tiktok,reels,youtube")
    ap.add_argument("--transcribe", action="store_true", help="auto-caption clips with faster-whisper")
    a = ap.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found — install it (mac: brew install ffmpeg)")
    project = a.project.resolve()
    if a.command == "init":
        cmd_init(project)
    elif a.command == "review":
        cmd_review(project)
    else:
        cmd_build(project, set(a.only.split(",")) if a.only else None, a.transcribe)


if __name__ == "__main__":
    main()

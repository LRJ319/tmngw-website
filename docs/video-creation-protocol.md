# Video Creation Protocol — Clips In, Platform-Ready Videos Out

Hand Claude a folder of clips. Get back a TikTok, an Instagram Reel, a faceless YouTube video, a thumbnail/cover for each, mastered audio, and post copy.

Tool: `video/tmngw_video.py` (ffmpeg + Python). Brief template: `video/brief.template.yml`.

---

## What you hand over

```
ep42-split-the-bill/
  clips/            raw clips, any order, any phone/camera format
  music/            optional — royalty-free bed (see Audio rules)
  voiceover.mp3     optional — narration for the faceless YouTube cut
  notes.txt         optional — 2–3 lines: the topic, the moment that hits, who it's for
```

Name clips `01_…`, `02_…` if you already know the order. Otherwise Claude picks it.

**How to get it to Claude**
- **Share a Google Drive folder link** — Claude pulls the clips and drops the finished files in an `output/` folder there.
- **Upload in the session** — fine for short clips.
- **Big raw footage (1 GB+):** run it on your own laptop with one command (see *Run it yourself*). Claude still writes the brief.

The one thing Claude needs from you: **which clips show faces.** Anything tagged with a face stays out of the faceless channel.

---

## What Claude does — 6 steps

| # | Step | What happens | Output |
|---|---|---|---|
| 1 | **Intake** | `init` lists every clip in a fresh `brief.yml` | `brief.yml` |
| 2 | **Review** | `review` makes a contact sheet for each clip and measures length, size, audio level | `output/review/` |
| 3 | **Brief** | Claude watches the sheets, picks the order and trims, writes the hook, thumbnail text, title, captions, hashtags | filled `brief.yml` |
| 4 | **Build** | `build` renders all three versions, masters the audio, burns captions, makes thumbnails | `output/*.mp4`, `*.jpg` |
| 5 | **QC** | Claude checks the report and stills against the checklist below; fixes and re-renders if anything fails | `output/report.md` |
| 6 | **Deliver** | Files + post copy, ready to upload | `output/post_copy.md` |

You approve at one point only: **after step 3** Claude sends the hook, thumbnail text, and cut order in one message. Say "go" or change it.

---

## The three versions

| | TikTok | Instagram Reels | Faceless YouTube |
|---|---|---|---|
| Canvas | 1080×1920 (9:16) | 1080×1920 (9:16) | 1920×1080 (16:9) |
| Default length cap | 60s | 90s | none |
| Hook text | First 3.5s, top | First 3.5s, top | First 3.5s, top |
| Captions | 4 words at a time, ALL CAPS, above the app UI | Same | Full lines, bottom |
| Faces | Yes | Yes | **No** — `faceless: false` clips are skipped |
| Lead audio | Clip audio | Clip audio | Voiceover if provided, clips underneath |
| Cover / thumbnail | `tiktok_cover.jpg` 1080×1920 | `reels_cover.jpg` 1080×1920, text inside the 3:4 grid crop | `youtube_thumbnail.jpg` 1280×720, under 2 MB |
| Extra | `tiktok_no_music.mp4` | `reels_no_music.mp4` | Optional brand intro/outro |

**Why a no-music version:** trending sounds get reach on TikTok and IG, and they can only be added in the app. Post the no-music file and add the sound there.

**Wide footage going vertical:** if the clip's shape is close to the target, it gets cropped to fill (use `crop_x` to keep the left or right host in frame). If not, the full frame sits on a blurred fill so nobody gets cut out.

---

## Audio rules

Every version gets the same chain:

1. **Clean** — rumble cut below 80 Hz, light noise reduction, gentle compression so both hosts sit at the same level.
2. **Duck** — music drops automatically whenever someone talks.
3. **Master** — normalized to **−14 LUFS, −1.5 dB true peak** (the level YouTube, TikTok, and IG play back at, so nothing gets turned down or distorts).
4. **Fade** — quarter-second in, one-second out. Small fades at every cut so there are no clicks.

**Music:** royalty-free only — YouTube Audio Library, Epidemic Sound, Artlist, or a track you own. Commercial songs get the YouTube version muted or demonetized. For TikTok/IG, use the no-music file and add a trending sound in the app.

**Matching the vibe:**

| Segment type | Music |
|---|---|
| Hot take / debate | No bed, or a low hip-hop loop at 0.12 |
| Story / storytime | Lo-fi or R&B instrumental at 0.15–0.18 |
| Comedy bit | No bed — let the room carry it |
| Faceless explainer | Steady instrumental at 0.18–0.22 under the voiceover |

---

## Hook, thumbnail, and title rules

**Hook (on screen, first 3 seconds)**
- 3–7 words. A take, a question, or a quote from the clip.
- Say what the viewer is about to hear, not the episode name.
- ✅ "Splitting the bill? Be serious." ❌ "TMNGW Episode 42 Clip"

**Thumbnail text**
- 2–5 words, never a repeat of the title.
- Put one word in `*stars*` to make it gold. That's the word the eye lands on.
- Frame: Claude picks the sharpest, best-lit, most colorful moment and saves the top 6 as `*_thumb_candidates.jpg`. Want a different one? Name the number.

**YouTube title (faceless channel)**
- Under 60 characters. Front-load the topic word people search.
- ✅ "Is Splitting the Bill on a First Date a Red Flag?"

---

## QC checklist (Claude runs this before delivery)

- [ ] Hook readable and not covered by the app UI
- [ ] Captions sit above the TikTok/IG buttons and match what's said
- [ ] No face in any frame of the faceless YouTube cut
- [ ] Every file reads −14 LUFS (±1) and true peak at or under −1 dB in `report.md`
- [ ] TikTok ≤ cap, Reels ≤ cap — if trimmed, the cut ends on a complete thought (if not, Claude writes a tighter `clips` list for that platform)
- [ ] Thumbnail text readable at phone size; YouTube thumbnail under 2 MB
- [ ] Music is royalty-free or the post goes out with the no-music file

---

## Captions

Two ways:
- **Auto:** `build --transcribe` (needs `pip install faster-whisper`). Writes `captions/<clip>.srt` — Claude proofreads names and slang before the final render.
- **Bring your own:** drop `captions/<clip-name>.srt` (timed to the raw clip) and `voiceover.srt` for the faceless cut. Trims and reorders are handled.

---

## Run it yourself (laptop)

One-time setup (Mac):
```bash
brew install ffmpeg
pip3 install pillow numpy pyyaml          # add faster-whisper for auto-captions
```

Each video:
```bash
python3 video/tmngw_video.py init   ~/Desktop/ep42      # creates brief.yml
python3 video/tmngw_video.py review ~/Desktop/ep42      # contact sheets → send to Claude
python3 video/tmngw_video.py build  ~/Desktop/ep42      # renders everything
python3 video/tmngw_video.py build  ~/Desktop/ep42 --only tiktok   # re-render one version
```

Everything lands in `~/Desktop/ep42/output/`.

---

## Prompt to start a video

> New video. Folder: [Drive link]. Topic: [one line]. Faces in clips 01, 03. Music: [file / none / your call].

#!/usr/bin/env python3
"""把《本体与知识图谱》讲解渲染成带旁白的 MP4（默认使用微软神经网络语音「云希」）。

用法：
    python make_video.py                 # 用 edge-tts（免费、无需密钥）合成云希语音并输出 MP4
    python make_video.py --tts azure     # 用 Azure 语音服务（需环境变量 AZURE_SPEECH_KEY、AZURE_SPEECH_REGION）
    python make_video.py --tts none      # 不配音，按估算时长出一版无声预览

流程：旁白逐句合成 → 按音频时长排时间轴 → 浏览器逐帧渲染 page.html → ffmpeg 编码为 H.264 + AAC。
同时输出 .srt 字幕文件；MP4 内带章节标记。
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
SR = 24000                      # 旁白采样率（Hz，单声道 16 bit）
INTRO, CH_LEAD, LEAD, GAP, OUTRO = 1.2, 0.9, 0.25, 0.5, 3.0   # 秒：片头、换章、画面先于声音、句间停顿、片尾
VIEWPORT = {"width": 1280, "height": 720}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def log(*a):
    print(*a, flush=True)


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if not exe:
            sys.exit("找不到 ffmpeg：请 pip install imageio-ffmpeg，或自行安装 ffmpeg。")
        return exe


# ---------------------------------------------------------------- 语音合成
def tts_edge(text, voice, rate, out):
    import edge_tts

    async def go():
        await edge_tts.Communicate(text, voice, rate=rate).save(str(out))
    asyncio.run(go())


def tts_azure(text, voice, rate, out):
    key = os.environ.get("AZURE_SPEECH_KEY")
    region = os.environ.get("AZURE_SPEECH_REGION", "eastasia")
    if not key:
        sys.exit("使用 --tts azure 需要设置环境变量 AZURE_SPEECH_KEY（以及 AZURE_SPEECH_REGION）。")
    ssml = (f"<speak version='1.0' xml:lang='zh-CN'><voice name='{voice}'>"
            f"<prosody rate='{rate}'>{escape(text)}</prosody></voice></speak>")
    req = urllib.request.Request(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        data=ssml.encode("utf-8"),
        headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3", "User-Agent": "ontology-explainer"})
    with urllib.request.urlopen(req, timeout=90) as r:
        out.write_bytes(r.read())


def synthesize(lines, backend, voice, rate, ff):
    tts_dir = BUILD / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    for i, l in enumerate(lines):
        say = l.get("say") or l["text"]
        if backend == "none":
            l["dur"] = round(len(say) / 4.6 + 0.3, 3)
            continue
        h = hashlib.sha1(f"{backend}|{voice}|{rate}|{say}".encode()).hexdigest()[:12]
        mp3, wav = tts_dir / f"{i:03d}-{h}.mp3", tts_dir / f"{i:03d}-{h}.wav"
        if not wav.exists():
            for attempt in range(4):
                try:
                    (tts_edge if backend == "edge" else tts_azure)(say, voice, rate, mp3)
                    if mp3.stat().st_size > 1000:
                        break
                except Exception as e:  # 网络抖动时重试
                    if attempt == 3:
                        raise
                    log(f"  第 {i + 1} 句合成失败，重试：{e}")
                time.sleep(2 ** attempt)
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(mp3), "-ac", "1", "-ar", str(SR),
                            "-sample_fmt", "s16", str(wav)], check=True)
            log(f"  语音 {i + 1}/{len(lines)}")
        with wave.open(str(wav)) as w:
            l["dur"] = w.getnframes() / w.getframerate()
        l["wav"] = str(wav)


# ---------------------------------------------------------------- 时间轴与字幕
def split_caption(text, limit=34):
    """按句号等切成字幕块；过长的块在最靠近中间的逗号处再切。"""
    parts = [p.strip() for p in re.findall(r"[^。！？；]+[。！？；]?", text) if p.strip()]
    merged = []
    for p in parts:
        if merged and len(p) < 6:
            merged[-1] += p
        else:
            merged.append(p)
    out = []

    def cut(s):
        if len(s) <= limit:
            out.append(s)
            return
        commas = [m.end() for m in re.finditer(r"[，、：]", s) if 4 < m.end() < len(s) - 3]
        if not commas:
            out.append(s)
            return
        k = min(commas, key=lambda c: abs(c - len(s) / 2))
        cut(s[:k])
        cut(s[k:])
    for m in merged:
        cut(m)
    return out


def build_timeline(lines):
    t = INTRO
    for idx, l in enumerate(lines):
        if idx and l["ci"] != lines[idx - 1]["ci"]:
            t += CH_LEAD
        l["start"] = round(t, 3)
        l["audio_at"] = t + LEAD
        l["end"] = l["audio_at"] + l["dur"]
        chunks = split_caption(l["text"])
        total = sum(len(c) for c in chunks)
        caps, acc = [], 0
        for k, c in enumerate(chunks):
            ct = l["start"] if k == 0 else l["audio_at"] + l["dur"] * acc / total
            caps.append({"t": round(ct, 3), "text": c})
            acc += len(c)
        l["caps"] = caps
        t = l["end"] + GAP + float(l.get("pause", 0))
    return round(t + OUTRO, 3)


def srt_time(s):
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(lines, path):
    rows, n = [], 1
    for i, l in enumerate(lines):
        for k, c in enumerate(l["caps"]):
            end = l["caps"][k + 1]["t"] if k + 1 < len(l["caps"]) else (lines[i + 1]["start"] if i + 1 < len(lines) else l["end"] + 1)
            rows.append(f"{n}\n{srt_time(c['t'])} --> {srt_time(end)}\n{c['text']}\n")
            n += 1
    path.write_text("\n".join(rows), encoding="utf-8")


def write_chapters(script, lines, total, path):
    starts = {}
    for l in lines:
        starts.setdefault(l["ci"], 0.0 if l["ci"] == 0 else l["start"])
    order = sorted(starts)
    out = [";FFMETADATA1", f"title={script['title']}"]
    for j, ci in enumerate(order):
        end = starts[order[j + 1]] if j + 1 < len(order) else total
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(starts[ci] * 1000)}", f"END={int(end * 1000)}",
                f"title={ci:02d} {script['chapters'][ci]}"]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def mix_narration(lines, total, path):
    buf = bytearray(int(math.ceil(total * SR)) * 2)
    for l in lines:
        with wave.open(l["wav"]) as w:
            data = w.readframes(w.getnframes())
        off = int(round(l["audio_at"] * SR)) * 2
        buf[off:off + len(data)] = data[:len(buf) - off]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(buf))


# ---------------------------------------------------------------- 字体（Google Fonts 按需子集，本地缓存）
def ensure_fonts(texts):
    chars = sorted(set("".join(texts)) - set("\n\r\t"))
    sig = hashlib.sha1("".join(chars).encode()).hexdigest()[:12]
    css_path = BUILD / "fonts.css"
    if css_path.exists() and sig in css_path.read_text(encoding="utf-8")[:200]:
        return
    font_dir = BUILD / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)
    latin = [c for c in chars if ord(c) < 0x250]
    families = [("Noto Sans SC", "400;500;700", chars), ("Noto Serif SC", "700;900", chars),
                ("JetBrains Mono", "400;600", latin)]
    faces = [f"/* fonts for text signature {sig} */"]
    try:
        for fam, weights, pool in families:
            for n in range(0, len(pool), 150):
                chunk = "".join(pool[n:n + 150])
                url = ("https://fonts.googleapis.com/css2?family=" + urllib.parse.quote_plus(fam) + f":wght@{weights}"
                       + "&text=" + urllib.parse.quote(chunk, safe="") + "&display=block")
                css = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60).read().decode()
                ranges = ",".join(f"U+{ord(c):04X}" for c in chunk)
                for block in re.findall(r"@font-face\s*{[^}]*}", css):
                    weight = re.search(r"font-weight:\s*(\d+)", block).group(1)
                    src = re.search(r"url\(([^)]+)\)", block).group(1)
                    name = f"{fam.replace(' ', '')}-{weight}-{n // 150}.woff2"
                    (font_dir / name).write_bytes(urllib.request.urlopen(
                        urllib.request.Request(src, headers={"User-Agent": UA}), timeout=60).read())
                    faces.append(f"@font-face{{font-family:'{fam}';font-style:normal;font-weight:{weight};font-display:block;"
                                 f"src:url('fonts/{name}') format('woff2');unicode-range:{ranges}}}")
        css_path.write_text("\n".join(faces) + "\n", encoding="utf-8")
        log(f"  字体已缓存：{len(faces) - 1} 个子集")
    except Exception as e:
        log(f"  警告：下载 Google Fonts 失败（{e}），将使用系统字体。")
        css_path.write_text(f"/* fonts unavailable {sig} */\n", encoding="utf-8")


# ---------------------------------------------------------------- 逐帧渲染 + 编码
def render(payload, total, fps, scale, ff, audio, chapters, out):
    from playwright.sync_api import sync_playwright
    n = int(math.ceil(total * fps))
    cmd = [ff, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-"]
    maps = ["-map", "0:v"]
    if audio:
        cmd += ["-i", str(audio)]
        maps += ["-map", "1:a"]
    cmd += ["-f", "ffmetadata", "-i", str(chapters)]
    meta_idx = 2 if audio else 1
    cmd += maps + ["-map_metadata", str(meta_idx), "-map_chapters", str(meta_idx),
                   "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps)]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-movflags", "+faststart", "-shortest", str(out)]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    shots = 0
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=scale)
        page.goto((HERE / "page.html").as_uri())
        loaded = page.evaluate("d => window.__setup(d)", payload)
        log(f"  已加载字体 {loaded} 个；共 {n} 帧（{fps} fps）")
        last = None
        for f in range(n):
            dirty = page.evaluate("ms => window.__renderAt(ms)", f * 1000 / fps)
            if dirty or last is None:
                last = page.screenshot(type="jpeg", quality=92)
                shots += 1
            enc.stdin.write(last)
            if f % (fps * 30) == 0:
                el = time.time() - t0
                log(f"  渲染 {f / fps:6.1f}s / {total:.1f}s   截图 {shots}   已用 {el:4.0f}s")
        browser.close()
    enc.stdin.close()
    if enc.wait() != 0:
        sys.exit("ffmpeg 编码失败")
    log(f"  完成：{shots} 张独立画面，用时 {time.time() - t0:.0f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tts", choices=["edge", "azure", "none"], default="edge", help="语音来源（默认 edge）")
    ap.add_argument("--voice", default="zh-CN-YunxiNeural", help="语音名称（默认 云希 zh-CN-YunxiNeural）")
    ap.add_argument("--rate", default="+0%", help="语速，如 +10%% 或 -5%%")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--scale", type=float, default=1.5, help="1.5 → 1920×1080；1 → 1280×720")
    ap.add_argument("--out", default=str(HERE / "out" / "本体与知识图谱.mp4"))
    args = ap.parse_args()

    script = json.loads((HERE / "script.json").read_text(encoding="utf-8"))
    lines = script["lines"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(exist_ok=True)
    ff = ffmpeg_exe()

    log(f"1/4 合成旁白（{args.tts}，{args.voice}）")
    synthesize(lines, args.tts, args.voice, args.rate, ff)
    total = build_timeline(lines)
    log(f"    视频总长 {total // 60:.0f} 分 {total % 60:.0f} 秒")

    log("2/4 准备字幕、章节和音轨")
    write_srt(lines, out.with_suffix(".srt"))
    chapters = BUILD / "chapters.txt"
    write_chapters(script, lines, total, chapters)
    audio = None
    if args.tts != "none":
        audio = BUILD / "narration.wav"
        mix_narration(lines, total, audio)

    log("3/4 准备字体")
    page_text = re.sub(r"<[^>]+>", " ", (HERE / "page.html").read_text(encoding="utf-8"))
    ensure_fonts([page_text] + [l["text"] for l in lines] + script["chapters"])

    log("4/4 逐帧渲染并编码")
    payload = {"total": total, "chapters": script["chapters"],
               "lines": [{k: l[k] for k in ("ci", "step", "start", "end", "caps")} for l in lines]}
    render(payload, total, args.fps, args.scale, ff, audio, chapters, out)
    log(f"输出：{out}\n字幕：{out.with_suffix('.srt')}")


if __name__ == "__main__":
    main()

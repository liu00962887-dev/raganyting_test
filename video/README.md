# 讲解视频：本体与知识图谱（MP4）

把 `../ontology-explainer` 互动页改编成一段不需要操作、从头播到尾的讲解视频：

- 旁白：微软神经网络语音「云希」（`zh-CN-YunxiNeural`）
- 画面：1920×1080、30 fps，字幕直接烧录在画面下方
- 输出：`out/本体与知识图谱.mp4`（内含章节标记）和同名 `.srt` 字幕文件
- 时长：约 10–12 分钟，由云希的实际语速决定

## 在自己电脑上生成（推荐）

需要 Python 3.9 或更新版本，并且能联网。

```bash
cd video
pip install -r requirements.txt
python -m playwright install chromium
python make_video.py
```

整个过程大约 10–20 分钟，终端里会显示进度。生成好的视频在 `video/out/` 里。

默认用 [edge-tts](https://github.com/rany2/edge-tts) 调用微软 Edge 浏览器“大声朗读”背后的云希语音，免费，不需要账号或密钥。

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--rate=+10%` | 调整语速：`--rate=+10%` 更快，`--rate=-5%` 更慢（要用等号连写） |
| `--voice zh-CN-YunyangNeural` | 换成其他微软中文语音 |
| `--scale 1` | 输出 1280×720，渲染更快 |
| `--tts azure` | 改用 Azure 语音服务（见下文） |
| `--tts none` | 不配音，按估算时长出一版无声预览，用来检查画面 |

## 使用 Azure 语音服务

如果你有 Azure 语音服务的订阅，也可以走官方接口，音色完全一样：

```bash
export AZURE_SPEECH_KEY=你的密钥
export AZURE_SPEECH_REGION=eastasia   # 你的资源所在区域
python make_video.py --tts azure
```

## 修改内容

- **旁白和字幕**：编辑 `script.json`。`text` 是字幕，`say` 是可选的朗读文本（用来纠正英文缩写、编号的读法），`pause` 是这句话之后额外停顿的秒数。
- **画面**：`page.html` 是逐帧渲染的页面，`step` 决定每句话出现时画面进行到哪一步。

改完重新运行 `python make_video.py` 即可。已经合成过的句子会复用 `build/tts/` 里的缓存，只重新合成改动过的句子。

## 工作原理

1. 逐句合成旁白，测出每句的准确时长。
2. 按音频时长排出时间轴：画面比声音早 0.25 秒切换，句与句之间停 0.5 秒，换章时多停 0.9 秒。
3. 用无头浏览器打开 `page.html`，按“虚拟时间”一帧一帧往前推。页面里所有动画在出现时都会被暂停，再定位到当前帧对应的时刻，所以每一帧都能准确复现，不会掉帧。
4. 画面没有变化的帧直接复用上一张截图，然后和旁白一起交给 ffmpeg，编码成 H.264 + AAC 的 MP4。

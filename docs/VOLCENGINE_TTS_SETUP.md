# Volcengine/Doubao TTS Backend — 配置与接入说明

## 1. 使用哪个 API

**火山引擎豆包语音合成 HTTP Chunked 流式 V3**

- 官方文档：https://www.volcengine.com/docs/6561/1598757
- 协议：HTTP POST，流式 JSON Lines 响应（每行一个 base64 音频块）
- Endpoint：`https://openspeech.bytedance.com/api/v3/tts/unidirectional`
- 音色模型：`seed-tts-1.0`（默认音色为 `moon_bigtts` 系列）

## 2. 控制台需要的字段

| 字段名 | 用途 | 环境变量 / config.json key |
|--------|------|----------------------------|
| API Key | 新版鉴权密钥（推荐） | `VOLCENGINE_TTS_API_KEY` |
| Resource ID | 模型版本标识 | `VOLCENGINE_TTS_RESOURCE_ID` |
| Voice Type | 发音人音色代号 | `VOLCENGINE_TTS_VOICE_TYPE` |
| Audio Format | 输出音频格式 | `VOLCENGINE_TTS_AUDIO_FORMAT` |
| Endpoint | API 地址（可选） | `VOLCENGINE_TTS_ENDPOINT` |

默认值：
- `VOLCENGINE_TTS_RESOURCE_ID` = `seed-tts-1.0`
- `VOLCENGINE_TTS_VOICE_TYPE` = `zh_female_shuangkuaisisi_moon_bigtts`
- `VOLCENGINE_TTS_AUDIO_FORMAT` = `mp3`
- `VOLCENGINE_TTS_ENDPOINT` = `https://openspeech.bytedance.com/api/v3/tts/unidirectional`

## 3. 请求 Headers

```http
POST /api/v3/tts/unidirectional
Content-Type: application/json
X-Api-Key: {API Key}
X-Api-Resource-Id: seed-tts-1.0
X-Api-App-Key: aGjiRDfUWi
X-Api-Request-Id: {UUID}
```

## 4. 请求体 (JSON)

```json
{
  "user": {
    "uid": "voice-claude-agent"
  },
  "req_params": {
    "text": "要合成的文本",
    "speaker": "zh_female_shuangkuaisisi_moon_bigtts",
    "audio_params": {
      "format": "mp3",
      "sample_rate": 24000
    }
  }
}
```

## 5. 响应格式

流式 JSON Lines，每行一个 JSON 对象：

```json
{"code": 0, "data": "<base64 编码的音频块>"}
{"code": 0, "data": "<base64 编码的音频块>"}
...
{"code": 20000000, "message": "ok", "data": null}
```

收到 `code=0` 的所有块后拼接 base64 音频；`code=20000000` 表示本次流式合成正常结束。随后写入临时文件，用 `afplay` 播放。

## 6. 在项目中的使用方式

### 6.1 设置凭据

```bash
# 环境变量
export VOICE_TTS_BACKEND=volcengine-doubao
export VOLCENGINE_TTS_API_KEY="your-api-key"
```

```bash
# 或写入 config.json
cat >> ~/.voice-claude-agent/config.json <<'JSON'
{
  "VOICE_TTS_BACKEND": "volcengine-doubao",
  "VOLCENGINE_TTS_API_KEY": "your-api-key",
  "VOLCENGINE_TTS_RESOURCE_ID": "seed-tts-1.0",
  "VOLCENGINE_TTS_VOICE_TYPE": "zh_female_shuangkuaisisi_moon_bigtts"
}
JSON
```

### 6.2 预览音色 (Preview TTS Voice)

配好 TTS 后，最快验证音色的方式是通过菜单栏 App 的 **Preview TTS Voice** 菜单项：

1. 启动 App（Finder 双击或终端 `voice-claude-agent app`）。
2. 点击菜单栏 **Preview TTS Voice**。
3. 在弹出的对话框中输入测试文本（默认：`你好，我是语音助手。当前正在测试语音播报效果。`）。
4. 点击 OK → 用当前 `VOICE_TTS_BACKEND` 配置的语音引擎播放。
   - 如果 `VOICE_TTS_BACKEND=volcengine-doubao`，用当前 `VOLCENGINE_TTS_VOICE_TYPE` 播放。
   - 如果 Volcengine TTS 失败，自动 fallback 到 macOS `say`，并在 `app_events` 中记录 `tts_preview_failed`。
5. 菜单项标题变化：`Preview TTS Voice` → `Previewing...` → `Preview TTS Voice`。
6. 如果点击 Cancel，不播放、不报错。

预览不会调用 Claude CLI，也不会写 session。

### 6.3 选择音色 (TTS Voice)

菜单栏 App 提供 **TTS Voice** 子菜单，可以直接切换常用音色，不需要手动编辑 `config.json`。默认列表只放入已知匹配 `seed-tts-1.0` 的 `moon_bigtts` 音色：

| 菜单显示 | 写入的 `VOLCENGINE_TTS_VOICE_TYPE` |
|---|---|
| 爽快思思（女声） | `zh_female_shuangkuaisisi_moon_bigtts` |
| 清润男声 | `zh_male_qingrun_moon_bigtts` |
| VV 女声（方言） | `zh_female_vv_uranus_bigtts` |

选择 `moon_bigtts` 系列音色时，App 会自动设置：

```text
VOLCENGINE_TTS_RESOURCE_ID=seed-tts-1.0
```

`BV701_streaming` / `BV120_streaming` 被放入 Experimental 分组。它们可能需要不同的 `VOLCENGINE_TTS_RESOURCE_ID`；如果 Resource ID 不匹配，火山接口会返回 `resource ID is mismatched with speaker related resource`，App 会 fallback 到 macOS say，并在 View Logs 中显示诊断。除非你已经在火山控制台确认对应 Resource ID，否则建议优先使用上面的 `moon_bigtts` 音色。

切换后可立即使用 **Preview TTS Voice** 验证效果，不需要重启 App。

### 6.4 检查配置状态

```bash
voice-claude-agent check
# 或菜单栏 App → Health Check / Mic Diagnostic
```

## 7. Fallback 行为

- 缺少 `VOLCENGINE_TTS_API_KEY`：自动 fallback 到 macOS `say`。
- API 返回错误：自动 fallback 到 macOS `say`。
- 网络错误 / 超时：自动 fallback 到 macOS `say`。
- 每次 fallback 都会记录到 `agent_state/app_events.jsonl`。

## 8. Security

- API Key 仅在环境变量或 `~/.voice-claude-agent/config.json` 中存储，不写入仓库。
- Settings UI 中密钥显示为脱敏形式（`<keep existing secret>`）。
- 日志、诊断输出中密钥始终脱敏（前 4 + 后 4 字符）。

## 9. 常用音色列表

| speaker 参数 | 描述 |
|---|---|
| `zh_female_shuangkuaisisi_moon_bigtts` | 爽快思思（女声，默认，使用 `seed-tts-1.0`） |
| `zh_male_qingrun_moon_bigtts` | 清润男声（使用 `seed-tts-1.0`） |
| `zh_female_vv_uranus_bigtts` | VV 女声（支持方言，使用 `seed-tts-1.0`） |
| `BV701_streaming` | 标准女声（Experimental，需确认 Resource ID） |
| `BV120_streaming` | 标准男声（Experimental，需确认 Resource ID） |

# Volcengine/Doubao ASR Backend

This project uses Volcengine's BigModel ASR Flash API for the
`volcengine-doubao` STT backend.

Official API reference:
https://www.volcengine.com/docs/6561/1631584

## API

- Method: `POST`
- Endpoint: `https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash`
- Protocol: synchronous HTTP JSON request
- Audio: base64-encoded WAV bytes in the JSON body
- Model: `bigmodel`

## Authentication

Two console credential styles are supported.

New console:

```text
VOLCENGINE_ASR_API_KEY
VOLCENGINE_ASR_RESOURCE_ID=volc.bigasr.auc_turbo
```

Old console:

```text
VOLCENGINE_ASR_APP_ID
VOLCENGINE_ASR_ACCESS_TOKEN
VOLCENGINE_ASR_RESOURCE_ID=volc.bigasr.auc_turbo
```

Optional:

```text
VOLCENGINE_ASR_ENDPOINT=https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash
VOLCENGINE_ASR_LANGUAGE=zh-CN
VOLCENGINE_ASR_CLUSTER=volcengine_input_common
```

`VOLCENGINE_ASR_LANGUAGE` and `VOLCENGINE_ASR_CLUSTER` are currently kept for
configuration visibility and compatibility with older notes. The v3 flash
request body only sends `user`, `audio`, and `request`.

## Headers

New console:

```http
X-Api-Key: <api-key>
X-Api-Resource-Id: volc.bigasr.auc_turbo
X-Api-Request-Id: <uuid>
X-Api-Sequence: -1
Content-Type: application/json
```

Old console:

```http
X-Api-App-Key: <app-id>
X-Api-Access-Key: <access-token>
X-Api-Resource-Id: volc.bigasr.auc_turbo
X-Api-Request-Id: <uuid>
X-Api-Sequence: -1
Content-Type: application/json
```

## Body

```json
{
  "user": {
    "uid": "voice-claude-agent"
  },
  "audio": {
    "data": "<base64-encoded WAV>"
  },
  "request": {
    "model_name": "bigmodel"
  }
}
```

When old-console credentials are used, `uid` is the configured App ID. When
new-console API key credentials are used, `uid` falls back to
`voice-claude-agent`.

## Response

The backend reads:

```text
result.text
```

and falls back to:

```text
result.utterances[].text
```

API errors are returned as `[STT error: ...]`, and the voice pipeline skips
Claude CLI when STT returns such an error.

## Local Config

For the menu bar app, prefer `~/.voice-claude-agent/config.json` over shell
exports, because apps launched from Finder do not reliably inherit terminal
environment variables.

Example using a new-console API key:

```json
{
  "VOICE_STT_BACKEND": "volcengine-doubao",
  "VOLCENGINE_ASR_API_KEY": "your-api-key",
  "VOLCENGINE_ASR_RESOURCE_ID": "volc.bigasr.auc_turbo"
}
```

Example using old-console credentials:

```json
{
  "VOICE_STT_BACKEND": "volcengine-doubao",
  "VOLCENGINE_ASR_APP_ID": "your-app-id",
  "VOLCENGINE_ASR_ACCESS_TOKEN": "your-access-token",
  "VOLCENGINE_ASR_RESOURCE_ID": "volc.bigasr.auc_turbo"
}
```

## Security

- Credentials are read only from environment variables or local config.
- Do not commit `~/.voice-claude-agent/config.json`.
- CLI checks, Mic Diagnostic, and Settings display credentials masked.
- Settings uses `<keep existing secret>` for existing secrets so full keys are
  not placed in editable default text.

## Commands

```bash
voice-claude-agent record --stt-backend volcengine-doubao
voice-claude-agent voice --stt-backend volcengine-doubao
voice-claude-agent wake --stt-backend volcengine-doubao
```

## whisper-cli vs volcengine-doubao

| Dimension | whisper-cli | volcengine-doubao |
| --- | --- | --- |
| Runtime | Local/offline | Cloud API |
| Accuracy | Model-size dependent | Better Chinese recognition expected |
| Privacy | Audio stays local | Audio is uploaded to Volcengine |
| Cost | Local compute | Cloud billing may apply |
| Setup | Install whisper.cpp + model | Volcengine account + credentials |

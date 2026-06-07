# Voice Claude Agent — Settings UI Manual Test (Phase 14)

This checklist verifies the menu-bar Settings UI added in F068.

## Prerequisites

- Launch the installed app from Finder or with `open ~/Applications/VoiceClaudeAgent.app`.
- Confirm `Health Check` opens successfully.
- Confirm `~/.voice-claude-agent/config.json` is either backed up or safe to edit.

## 1. Open Settings

1. Click the menu-bar microphone icon.
2. Click `Settings...`.

Expected:
- A dialog opens with the config path.
- It lists `VOICE_RECORD_SECONDS`, `VOICE_STT_BACKEND`, `WHISPER_CPP_MODEL`, and `WHISPER_CPP_LANGUAGE`.
- The editable text area contains `KEY=value` lines.

## 2. Save Valid Values

Enter:

```text
VOICE_RECORD_SECONDS=10
VOICE_STT_BACKEND=whisper-cli
WHISPER_CPP_MODEL=/Users/orange/.local/share/whisper.cpp/models/ggml-base.bin
WHISPER_CPP_LANGUAGE=zh
```

Expected:
- The app shows `Settings Saved`.
- `~/.voice-claude-agent/config.json` contains the saved values.
- `Mic Diagnostic` shows `Record duration: 10s`, `STT backend: whisper-cli`, and the config path.
- `Health Check` shows the config path.

## 3. Reject Invalid Duration

Open `Settings...` and enter:

```text
VOICE_RECORD_SECONDS=0
```

Expected:
- The app shows `Settings Validation Error`.
- The previous config remains unchanged.

## 4. Reject Invalid Backend

Open `Settings...` and enter:

```text
VOICE_STT_BACKEND=bad-backend
```

Expected:
- The app shows `Settings Validation Error`.
- The previous backend remains unchanged.

## 5. Reset Settings

Click `Reset Settings`.

Expected:
- `~/.voice-claude-agent/config.json` is removed.
- The app shows `Settings Reset`.
- `Mic Diagnostic` shows default `Record duration: 5s` and `STT backend: text-input`.

## 6. Smoke Test After Save

After saving valid settings:

1. Click `Trigger Recording`.
2. Speak a short normal command.
3. Wait for transcription and Claude response.

Expected:
- The app uses the saved recording duration and backend.
- Last Transcript and Last Summary update.
- View Logs includes the normal record/STT/Claude/TTS cycle.

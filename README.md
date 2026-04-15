# LiveKit Voice Agent

A LiveKit-powered voice agent for real-time conversational AI, integrated with Coval for testing and evaluation.

## Overview

This project provides a voice-enabled AI assistant built on the LiveKit Agents framework. It includes:

- **Voice Agent** (`agent.py`) - Real-time conversational AI using speech-to-text, LLM, and text-to-speech
- **Token Server** (`token_server.py`) - Flask server that generates LiveKit access tokens for client authentication

## Tech Stack

| Component | Provider |
|-----------|----------|
| Speech-to-Text | AssemblyAI Universal Streaming |
| LLM | OpenAI GPT-4.1-mini |
| Text-to-Speech | Cartesia Sonic 3 |
| Voice Activity Detection | Silero VAD |
| Turn Detection | Multilingual Model |
| Noise Cancellation | BVC (with SIP telephony support) |

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- LiveKit Cloud account (or self-hosted LiveKit server)
- API keys for AssemblyAI, OpenAI, and Cartesia

## Setup

1. **Install uv:**
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Install dependencies:**
   ```bash
   uv sync
   ```

3. **Configure environment variables:**

   Create a `.env.local` file with:
   ```
   LIVEKIT_API_KEY=your_api_key
   LIVEKIT_API_SECRET=your_api_secret
   LIVEKIT_URL=wss://your-project.livekit.cloud
   OPENAI_API_KEY=your_openai_key
   ASSEMBLYAI_API_KEY=your_assemblyai_key
   CARTESIA_API_KEY=your_cartesia_key
   COVAL_API_KEY=your_default_coval_key
   COVAL_API_KEY_CAL_DEMO=your_cal_demo_key
   COVAL_API_KEY_DDBD=your_ddbd_key
   ```

   Multi-org tracing notes:
   `COVAL_API_KEY` remains the legacy fallback.
   Add one env var per org using `COVAL_API_KEY_<LABEL>`, for example
   `COVAL_API_KEY_CAL_DEMO` and `COVAL_API_KEY_DDBD`.

## Running

Start both components in separate terminals:

**Terminal 1 - Token Server:**
```bash
uv run python token_server.py
```
Runs on `http://localhost:8888`

**Terminal 2 - Voice Agent:**
```bash
uv run agent.py start
```

## API Endpoints

### POST /token
Generate a LiveKit access token for room access.

**Request:**
```json
{
  "room_name": "my-room",
  "participant_name": "user-123"
}
```

**Response:**
```json
{
  "token": "eyJhbG...",
  "serverUrl": "wss://your-project.livekit.cloud",
  "room_name": "my-room"
}
```

### GET /health
Health check endpoint.

## Coval Integration

This agent is designed to work with [Coval](https://coval.dev) for voice agent testing and evaluation. Coval calls the `/token` endpoint to obtain credentials, then connects to LiveKit to interact with your agent.

## Docker

Build and run with Docker:

```bash
docker build -t livekit-voice-agent .
docker run -p 8888:8888 --env-file .env.local livekit-voice-agent
```

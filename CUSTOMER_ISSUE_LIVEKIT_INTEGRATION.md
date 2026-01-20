# Customer Issue: LiveKit Integration Not Working

**Date:** January 2026
**Customer:** Joash Johnson
**Issue Type:** LiveKit Integration Failure

---

## Customer Report

The customer reported that their LiveKit integration was not working despite having a functioning agent and token endpoint. Their setup:

### Token Endpoint Response Format
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "room_name": "3f0a81166c10449bb20629d8db9937fa",
  "agent": {
    "status": "Running",
    "session_id": "4709cc48-198c-4b78-8d67-c1e9797ac91a"
  }
}
```

### Customer Questions
1. "We provided the room URL as well along with this but the integration is not working."
2. "I also saw another parameter that it expects - Sandbox ID, which we do not have at the moment, since we are not using the livekit cloud for the agent deployment, it's on a separate cloud. Does it mean that we can only integrate agents which are deployed on livekit agents platform?"

---

## Root Cause Analysis

### Issue 1: Missing `serverUrl` in Token Response

Coval's `LiveKitModelManager._generate_token()` method looks for the LiveKit server URL in the token response using these field names:
- `serverUrl`
- `server_url`

**The customer's response did not include this field.** Without it, Coval either:
- Uses the `livekit_url` configured in the dashboard (if provided)
- Fails to connect if no URL is available

**Relevant code** (`backend/evaluation_pipeline/models/LiveKitModelManager.py`, lines 191-203):
```python
if "serverUrl" in response_data:
    self.livekit_url = response_data["serverUrl"]
elif "server_url" in response_data:
    self.livekit_url = response_data["server_url"]
```

### Issue 2: Sandbox ID Confusion

The customer assumed Sandbox ID was required. **It is not.**

The Sandbox ID field is **only required for LiveKit Cloud's agent dispatch feature**. It gets passed as an `X-Sandbox-ID` header to the token endpoint. Self-hosted LiveKit deployments do not need this.

**The UI did not clearly indicate this was optional** (now fixed - see UI changes below).

---

## Resolution Steps

### For the Customer

1. **Update token endpoint response** to include `serverUrl`:
   ```json
   {
     "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
     "serverUrl": "wss://your-livekit-server.com",
     "room_name": "3f0a81166c10449bb20629d8db9937fa"
   }
   ```

2. **OR configure `livekit_url` in Coval dashboard** with their LiveKit server URL (e.g., `wss://their-server.livekit.cloud`)

3. **Leave Sandbox ID empty** - it's only for LiveKit Cloud agent dispatch

### Platform Changes Made

1. **UI Update**: Changed Sandbox ID label to "Sandbox ID (Optional)" with description: "Only required for LiveKit Cloud agent dispatch. Leave empty if self-hosting or using your own token endpoint."

2. **Documentation Updates**: Updated LiveKit integration guides with:
   - Token endpoint request/response format requirements
   - Clarification that Sandbox ID is optional
   - Common troubleshooting scenarios

---

## Token Endpoint Requirements

### Request Format (from Coval)
```http
POST /your-token-endpoint
Content-Type: application/json

{
  "room_name": "abc123-uuid-generated-by-coval",
  "participant_name": "simulated_user"
}
```

### Expected Response Format
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "serverUrl": "wss://your-livekit-server.com",
  "room_name": "abc123-uuid-generated-by-coval"
}
```

### Accepted Token Field Names
Coval looks for the token in these fields (in order):
- `participantToken`
- `token`
- `accessToken`
- `participant_token`
- `access_token`

### Accepted Server URL Field Names
- `serverUrl`
- `server_url`

### Accepted Room Name Field Names
- `roomName`
- `room_name`

---

## Testing the Integration Locally

See the `token_server.py` file in this directory for a working example token endpoint that can be used for local testing with Coval.

### Running Locally with Docker Backend
If running Coval's backend in Docker, `localhost` won't work. Use either:
- `http://host.docker.internal:8888/token` (Docker's host alias)
- ngrok to expose your local server: `ngrok http 8888`

---

## Files Modified

- `frontend/components/shared/forms/SimulatorFormFields.tsx` - Made Sandbox ID optional in UI
- `docs/guides/livekit-guide/livekit-guide.mdx` - Updated integration guide
- `docs/concepts/agents/connections/livekit.mdx` - Updated connection docs

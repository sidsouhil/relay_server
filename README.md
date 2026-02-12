# SDL/Unity Relay Server (HTTP + SSE)

A lightweight FastAPI relay that bridges SDL (Python/Arduino) and Unity clients across different networks using only HTTP:
- POST /send to publish any message.
- SSE /sse to receive real-time streams.
- /recent to replay the last messages after reconnect.
- Single API key auth via query param `key=`.

## Architecture
- Clients POST JSON envelopes to `/send`.
- Relay routes by workstation + target (`unity`, `sdl`, or `broadcast`).
- Unity subscribes to `/sse?workstation=<id>&client=unity&key=...` and receives `target==unity` or `broadcast`.
- SDL subscribes to `/sse?workstation=<id>&client=sdl&key=...` and receives `target==sdl` or `broadcast`.
- In-memory ring buffers (per workstation/target plus broadcast) backfill `/recent`.
- Keepalive ping every 15s on SSE. Slow subscribers are dropped to avoid blocking.

## Envelope schema
```json
{
  "workstation": "elegoo_3d" | "pumps_sdl" | "opentrons",
  "source": "unity" | "sdl" | "platform" | "relay",
  "target": "unity" | "sdl" | "broadcast",
  "type": "waypoint" | "event" | "frequency" | "platform_update" | "command" | "status" | "camera",
  "ts": 1700000000000,
  "payload": { "...": "any content" }
}
```
The relay is payload-agnostic: you can send motion, frequency, camera frames (e.g., `jpeg_b64`), BO candidates/distances/iterations/current_iteration, status/logs, etc.

## Security
- Set `RELAY_API_KEY` on Render.
- Every request must include `?key=<RELAY_API_KEY>` or gets 401.

## Deploy on Render (Web Service)
1. Create a new Web Service, connect this folder (`relay_server`).
2. Environment: Python.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Set env var `RELAY_API_KEY` (required).
6. Deploy. Note: Render free tier sleeps; for always-on, pick a paid plan.

## Test with curl
Replace `<KEY>` with your API key and `<BASE>` with your Render URL.

- Health:
  ```
  curl -s <BASE>/health
  ```

- Send a message:
  ```
  curl -X POST "<BASE>/send?key=<KEY>" \
       -H "Content-Type: application/json" \
       -d '{
         "workstation":"elegoo_3d",
         "source":"platform",
         "target":"broadcast",
         "type":"status",
         "ts": 1700000000000,
         "payload":{"msg":"hello world"}
       }'
  ```

- Stream as Unity:
  ```
  curl -N "<BASE>/sse?workstation=elegoo_3d&client=unity&key=<KEY>"
  ```
  You should see lines like `data: {...}\n\n` and keepalive `: ping`.

- Recent (last 20 for Unity):
  ```
  curl "<BASE>/recent?workstation=elegoo_3d&client=unity&limit=20&key=<KEY>"
  ```

## How SDL and Unity connect
- **SDL (Python)**:
  - POST candidates/frequency/status to `/send?key=...`
  - SSE subscribe to `/sse?workstation=<id>&client=sdl&key=...` to receive platform commands, broadcast updates, etc.

- **Unity**:
  - SSE subscribe to `/sse?workstation=<id>&client=unity&key=...` to receive all messages targeted to Unity or broadcast.
  - POST UI-originated commands/events back to `/send?key=...`.

## UI Sections (high level for upcoming Unity work)
1. **Home**: status overview, last connection, quick connect, recent runs preview.
2. **Workstations**: pick Elegoo 3D (active), Pumps SDL (coming soon), Opentrons (coming soon); show capabilities and status.
3. **Experiments**: list per workstation; Elegoo “Colors Mixing Experiment” ready with description & required devices.
4. **Control Panel**:
   - 3D preview, live camera panel
   - Connect/disconnect
   - Manual vs Self-driving mode
   - Manual: send commands to SDL
   - Self-driving: show candidates, iterations/current_iteration, distance, start/stop trial
   - Status/log panel
   - Frequency widget + closest frequency to target
5. **History / Results**: table of trials (trial_index, params, frequency, distance, timestamps), charts, export CSV/JSON.
6. **Settings**: relay URL, API key, workstation id; camera settings (fps/quality); preferences; diagnostics (SSE reconnect, ping).
7. **Exit**: quit/close with safe disconnect.

## Notes
- Next step (separate message): Unity client, SDL Python client, full data-flow diagram.
- Free Render tier may sleep; use paid/always-on for uninterrupted relay.

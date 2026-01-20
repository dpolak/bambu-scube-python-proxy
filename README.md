# Bambu Lab MQTT Proxy for SCUBE

A simple Python Flask proxy server that provides real-time printer data from Bambu Lab printers via MQTT.

## Why This Exists

Bambu Lab's REST API only returns basic device information (online status, device name). **Real-time data like temperatures, print progress, and layer count are only available via MQTT.** This proxy bridges that gap by:

1. Connecting to Bambu Lab's MQTT broker
2. Caching real-time data for each printer
3. Exposing a simple REST API for your WordPress site

## Endpoints

### `GET /api/devices`
Returns list of devices from Bambu Cloud.

### `GET /api/status/all`
**Main endpoint for SCUBE** - Returns all devices with real-time data merged.

### `POST /api/realtime/start`
Starts an MQTT session for a specific device.
```json
{"device_id": "01S00C123456789"}
```

### `GET /api/realtime/<device_id>`
Get cached MQTT data for a device.

### `GET /health`
Health check endpoint.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BAMBU_TOKEN` | ✅ | Your Bambu Lab JWT access token |
| `SCUBE_API_KEY` | ✅ | API key for authenticating requests from SCUBE |
| `PORT` | ❌ | Server port (default: 5000) |
| `MQTT_SESSION_DURATION` | ❌ | How long MQTT sessions stay active (default: 120 seconds) |

## Getting Your Bambu Token

1. Log in to https://bambulab.com
2. Open browser DevTools (F12) → Network tab
3. Look for any API request to `api.bambulab.com`
4. Find the `Authorization: Bearer ...` header
5. The token is everything after "Bearer "

Or use the authentication script from [Bambu-Lab-Cloud-API](https://github.com/coelacant1/Bambu-Lab-Cloud-API).

---

## Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new)

1. Create a new project on [Railway](https://railway.app)
2. Connect your GitHub repository (or deploy from this folder)
3. Add environment variables in the Railway dashboard
4. Railway will automatically detect the `Procfile` and deploy

---

## Deploy to Render

1. Create a new **Web Service** on [Render](https://render.com)
2. Connect your repository
3. Settings:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
4. Add environment variables
5. Deploy!

---

## Deploy to Fly.io

1. Install Fly CLI: `curl -L https://fly.io/install.sh | sh`
2. Login: `fly auth login`
3. Launch:
```bash
cd bambu-proxy
fly launch
```
4. Set secrets:
```bash
fly secrets set BAMBU_TOKEN="your_token_here"
fly secrets set SCUBE_API_KEY="your_api_key_here"
```

---

## Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment file
cp .env.example .env
# Edit .env with your values

# Run locally
python app.py
```

---

## Testing

```bash
# Health check
curl http://localhost:5000/health

# Get devices (with API key)
curl -H "X-API-Key: your_api_key" http://localhost:5000/api/devices

# Get all status (main endpoint)
curl -H "X-API-Key: your_api_key" http://localhost:5000/api/status/all
```

---

## How It Works

```
┌─────────────────────┐     REST API      ┌──────────────────┐
│   SCUBE WordPress   │◄─────────────────►│   This Proxy     │
│   (PHP Backend)     │                   │   (Flask/Python) │
└─────────────────────┘                   └────────┬─────────┘
                                                   │
                                          MQTT (TLS/8883)
                                                   │
                                                   ▼
                                        ┌──────────────────┐
                                        │ Bambu Lab Cloud  │
                                        │   MQTT Broker    │
                                        └──────────────────┘
```

1. SCUBE calls `/api/status/all`
2. Proxy connects to Bambu MQTT for each device
3. MQTT data is cached for ~2 minutes
4. Response merges REST data + MQTT real-time data

---

## Response Example

```json
{
  "devices": [
    {
      "dev_id": "01S00C123456789",
      "name": "P1S Office",
      "online": true,
      "print_status": "RUNNING",
      "realtime": {
        "nozzle_temper": 220.0,
        "bed_temper": 60.0,
        "mc_percent": 45,
        "layer_num": 23,
        "total_layer_num": 150,
        "mc_remaining_time": 3600,
        "gcode_state": "RUNNING",
        "last_update": "2024-01-15T10:30:00Z"
      }
    }
  ],
  "timestamp": "2024-01-15T10:30:05Z"
}
```

---

## Security Notes

- **Never commit your `.env` file!** It contains secrets.
- The `SCUBE_API_KEY` should be a strong random string (32+ chars)
- Rate limiting is enabled (60 requests/minute by default)
- Only your SCUBE instance should know the API key

---

## Troubleshooting

### "MQTT connection failed"
- Check your `BAMBU_TOKEN` is valid and not expired
- Tokens may expire; re-authenticate if needed

### "No real-time data"
- Printer must be online and connected to Bambu Cloud
- First request starts MQTT; data arrives within seconds
- Check printer has cloud connection enabled

### "401 Unauthorized"
- Make sure you're sending `X-API-Key` header
- Verify `SCUBE_API_KEY` matches on both sides

---

## License

MIT License - Use freely in your SCUBE installation.

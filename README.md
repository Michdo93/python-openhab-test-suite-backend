# python-openhab-test-suite-backend

Stateless Flask backend for the
[python-openhab-test-suite](https://github.com/Michdo93/openhab-test-suite)
web frontend.

Every request carries the openHAB credentials in the body — no session state
is stored on the server. The backend instantiates the appropriate tester class
per request, runs the method, captures stdout/stderr, and returns the result.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Health check / wake-up |
| `POST` | `/api/connect` | Verify credentials |
| `POST` | `/api/test` | Run a tester method |

### `POST /api/connect`

```json
{ "url": "https://myopenhab.org", "username": "user@example.com", "password": "secret" }
```

Response:

```json
{ "loggedIn": true, "isCloud": true }
```

### `POST /api/test`

```json
{
  "url":      "https://myopenhab.org",
  "username": "user@example.com",
  "password": "secret",
  "tester":   "ItemTester",
  "method":   "testSwitch",
  "params":   ["MySwitch", "ON", "ON", 10]
}
```

Response:

```json
{ "result": true, "output": "OK: MySwitch reached state ON" }
```

Available testers: `ItemTester`, `ThingTester`, `RuleTester`,
`ChannelTester`, `PersistenceTester`, `SitemapTester`.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
# → http://localhost:8080
```

## Docker

```bash
docker build -t python-openhab-test-suite-backend .
docker run -p 8080:8080 python-openhab-test-suite-backend
```

## Deploy on Render.com

1. Push this repository to GitHub.
2. On [render.com](https://render.com): **New → Web Service → Connect repository**.
3. Settings:
   - **Language:** Docker
   - **Region:** Frankfurt (EU Central)
   - **Plan:** Free
   - **Environment variable:** `PORT = 8080`
4. Click **Deploy**.

The live URL will be:
`https://python-openhab-test-suite-backend.onrender.com`

## License

MIT License

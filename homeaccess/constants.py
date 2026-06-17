"""Static protocol facts for the Philips Home Access cloud API.

Everything here is a fixed, reverse-engineered constant (not user config and not
runtime state). See research/FINDINGS.md for how each was derived.
"""
from __future__ import annotations

# --- Auth backend (Juzi Wulian "Oneness") ----------------------------------
AUTH_BASE = "https://user-oneness.juziwulian.com"
LOGIN_PATH = "/homeaccess/oauth/login"
REGION_PATH = "/user/region"
DATACENTERS_PATH = "/datacenters"

# --- Datacenters: code -> endpoints (from POST /datacenters) ----------------
# api_base is used for device commands; ws_addr / mqtt_addr for realtime.
DATACENTERS: dict[str, dict[str, str]] = {
    "PhilipsNorthAmerica": {
        "api_base": "https://api.idlespacetech.com",
        "ws_addr": "wss://ws.idlespacetech.com",
        "mqtt_addr": "",
    },
    "PhilipsSingapore": {
        "api_base": "https://app-sg.cone-x.com",
        "ws_addr": "",
        "mqtt_addr": "mqtt-sg-app.cone-x.com:5883",
    },
    "PhilipsOneness": {
        "api_base": "https://user-oneness.juziwulian.com",
        "ws_addr": "",
        "mqtt_addr": "",
    },
}
DEFAULT_DATACENTER = "PhilipsNorthAmerica"


def datacenter_code_for(device_field: str, fallback: str = DEFAULT_DATACENTER) -> str:
    """Map a device record's `dataCenter` (e.g. "north-america") to a code.

    The account may hold tokens for several datacenters and each one's device
    list can echo the same lock, so the lock's own `dataCenter` field is the
    authoritative home for routing commands. Transform: "north-america" ->
    "PhilipsNorthAmerica", "singapore" -> "PhilipsSingapore", etc.
    """
    if not device_field:
        return fallback
    parts = device_field.replace("_", "-").split("-")
    code = "Philips" + "".join(p.capitalize() for p in parts)
    return code if code in DATACENTERS else fallback

# --- HTTP headers ----------------------------------------------------------
K_TENANT = "philips"
K_VERSION = "4.14.0"
K_SIGNV = "1.0.0"
DEFAULT_LANGUAGE = "en_US"
# Device-API user agent (wp1 interceptor builds this from Build.* fields).
DEVICE_USER_AGENT = "System: Android 10/ Model: ONEPLUS A6010/ Brand: OnePlus"
# Login uses the raw OkHttp UA.
LOGIN_USER_AGENT = "okhttp/4.10.0"

# Authorization is a custom "token" header (NOT Authorization: Bearer).
TOKEN_HEADER = "token"
# Header that flags an encrypted (open/close) request body.
ENCRYPT_DATA_HEADER = "encrypt_data"

# Server rejects an expired/invalid token as HTTP 200 with this body code
# (string "444", msg "Not logged in").
AUTH_FAIL_CODES = {"444"}

# --- Endpoint paths --------------------------------------------------------
DEVICE_LIST_PATH = "/homeaccess/device/list"
OPEN_DEVICE_PATH = "/v3/device/open-device"
CLOSE_DEVICE_PATH = "/v3/device/close-device"
QUERY_ATTR_PATH = "/v4/device/query-device-attr"
QUERY_VERSION_PATH = "/v4/device/query-device-versioninfo"
DTIM_WAKE_PATH = "/v3/device/dtim-wake"
PART_LIST_PATH = "/v3/user/part/list"
CHECK_TOKEN_PATH = "/v3/user/check-user-token"

# --- Embedded RSA keys (from DEX class ld5) ---------------------------------
# App private key: signs requests (sign256) and decrypts responses. Static,
# shared by every install -- NOT a per-device secret.
APP_PRIVATE_KEY_DER_B64 = (
    "MIICdwIBADANBgkqhkiG9w0BAQEFAASCAmEwggJdAgEAAoGBAOlqfLIYbh+4br4o/spjYE8UwNmeJznE7lPT8TaksfB7FpeiGp5WjR95y6/Z4Rm/V96XyphhUTi0VrkH9lVXL+gX5E31naefJBt5SWLA1fb12jpPArBp9jIWUD16wI0ExfJcYjIOhG4WAf53CwP6Q8wzPbzPkMn4xruedUQwVfUvAgMBAAECgYEA4DogscGQQL6e++RL50aR5UYtgKBSVEefHz5R0Uljen30FRRvd73zcdJB3ptyh5ZtpfKxd7K9ILj1Omiwtgi8heaN68ssiJAl9DjmhR+CegUCkIMll/Plos/wsv/H9/8JQyfbM95+At0hzyIGicpXBpTuFanxAe3qKZKXT7k1zBkCQQD0d5NuIRMrhNj8BjL5My3AYQw3JzxKA8MlVv14UEBsKDcgMMtkickNQ5+K4Lvu51W2qI7ly/Rl0Q4NpIE44iVNAkEA9G1yauyBRblbA3h8t7GUukeDNaA+iZtIQKNLH+99J88GpcvqjwUswvKOYnl8JFMimH2qcDzUkewi9KzjQCrWawJAb92LzBg8cmyO8fxQNPIzXFXMRiyhDOld0edVg0mNwTBB0WwiljXqlzQ7fExMEw0ujq/g+8xxYGniOWHuc74f/QJAUgtCsp08LxkucZXJ1ybmUziZ1DA7jZjvwbKODuQmUGxvQMuXqfYEtlMQdAFvKAo3vJPB1/azK1/lw9ccWHeIjQJBAPQ1WYweAJAiS7Srq4Z/55zr5Cngvd71YBPdrmdzENFlpx4+Ro4awOiZOQvcj8yFb8wU5qR87FOa3MtwSTQ08IE="
)
# Server public key B: used to ENCRYPT open/close request bodies. Its private
# half is server-side, so we can build commands but not decrypt captured ones.
SERVER_PUB_B_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDAhvfVLGrJ/M3xpUnT1xlN30E1UESxhAmGmFyTx3p3vpxF4zMYpUjwHckCvg/zvZwhNTgsm3CNT7LAdE8lCl2YK4BoUZ6IYbbXSOa02/brASX4kjpOPbTcaDfYud2CFWQba95d5dlf3Jf9Z3eTPwNK7YQ0LDDWMOQ6LxoGqcLciQIDAQAB"
)

# --- Realtime (WebSocket) event decoding -----------------------------------
# wfevent "record" eventType selects the category:
EVENT_TYPE_LOCK = 1   # deadbolt lock/unlock
EVENT_TYPE_DOOR = 4   # door open/close (magnetic sensor)

# Lock records (eventType 1), eventCode mapping by source.
#   remote/app: eventSource 8, userID 0, appID 2
#   manual:     eventSource 9/255, userID 255, appID 0
REMOTE_EVENT_SOURCE = 8
EVENT_CODE_REMOTE = {1: "locked", 2: "unlocked"}
EVENT_CODE_MANUAL = {8: "locked", 9: "unlocked"}

# Door records (eventType 4), eventCode mapping.
DOOR_EVENT_CODE = {1: "opened", 2: "closed"}

# action snapshot / device-list openStatus mapping (deadbolt bolt state).
OPEN_STATUS = {1: "locked", 2: "unlocked"}

# device-list magneticStatus -> door contact state. Best-effort: only observed
# closed (=2) so far; the open (=1) mapping mirrors the door eventCode and
# self-corrects on the first door event. Verify by opening the door + `status`.
MAGNETIC_STATUS = {1: "open", 2: "closed"}

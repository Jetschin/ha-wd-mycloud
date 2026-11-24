"""Constants for the WD My Cloud integration (no Home Assistant imports)."""

DOMAIN = "wd_mycloud"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_DEVICE_ID = "device_id"
CONF_FOLDER = "folder"

DEFAULT_FOLDER = "HA_Backup"

CONFIG_URL = "https://config.mycloud.com/config/v1/config"
CLIENT_ID = "9B0Gi617tROKHc2rS95sT1yJzR6MkQDm"
CLIENT_SECRET = "oSJOB1KOWnLVZm11DVknu2wZkTj5AGKxcINEDtEUPE30jHKvEqorM8ocWbyo17Hd"
SCOPE = "openid offline_access nas_read_write nas_read_only user_read device_read"

CHUNK_SIZE = 8 * 1024 * 1024

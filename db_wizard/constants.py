"""
Constants and configuration values for db-wizard
Centralized location for magic numbers and default values
"""

# ============================================================================
# Generic Connection Settings
# ============================================================================

# Default connection timeout in milliseconds (used by all engines)
DEFAULT_CONNECTION_TIMEOUT = 5000

# Shorter timeout for quick checks (e.g., listing hosts status)
QUICK_CHECK_TIMEOUT = 1000

# ============================================================================
# MongoDB-specific Settings
# ============================================================================

# Default batch size for Python fallback document insertion (MongoDB only)
DEFAULT_BATCH_SIZE = 1000

# Default sample size for verification (MongoDB only)
DEFAULT_VERIFICATION_SAMPLE_SIZE = 100

# Threshold for running checksums (documents count, MongoDB only)
# Collections smaller than this will get full checksum verification
CHECKSUM_THRESHOLD = 10000

# ============================================================================
# MySQL-specific Settings
# ============================================================================

# Default MySQL port
DEFAULT_MYSQL_PORT = 3306

# MySQL system databases to exclude from listings
MYSQL_SYSTEM_DATABASES = frozenset({
    'information_schema', 'mysql', 'performance_schema', 'sys'
})

# MongoDB system databases to exclude from listings
MONGO_SYSTEM_DATABASES = frozenset({
    'admin', 'config', 'local'
})

# ============================================================================
# SSH/SCP Settings
# ============================================================================

# SSH connection timeout in seconds
SSH_CONNECT_TIMEOUT = 10

# SSH keep-alive interval in seconds
SSH_KEEPALIVE_INTERVAL = 5

# SSH keep-alive max count
SSH_KEEPALIVE_MAX_COUNT = 3

# SCP transfer timeout in seconds (5 minutes)
SCP_TRANSFER_TIMEOUT = 300

# ============================================================================
# FTP Settings
# ============================================================================

# Default FTP port
DEFAULT_FTP_PORT = 21

# ============================================================================
# Dump/Restore Settings
# ============================================================================

# Timeout for dump|restore pipe operations (10 minutes)
PIPE_TIMEOUT = 600

# ============================================================================
# Incremental Copy Settings
# ============================================================================

# Safety overlap for numeric watermarks (rows).
# With AUTO_INCREMENT ids, a row with a LOWER id can be committed AFTER a row
# with a higher one (transactions commit out of order). If we resumed exactly
# from MAX(id) we would skip those late arrivals forever. Copying a few rows
# back is cheap and the duplicates get discarded by INSERT IGNORE.
INCREMENTAL_OVERLAP_ROWS = 10

# Safety overlap for time-based watermarks (seconds).
# Same reasoning, applied to timestamp columns and MongoDB ObjectIds (whose
# leading 4 bytes are a unix timestamp).
INCREMENTAL_OVERLAP_SECONDS = 5

# ============================================================================
# Formatting Thresholds
# ============================================================================

# Threshold for using 'K' suffix (thousands)
FORMAT_THOUSANDS_THRESHOLD = 1_000

# Threshold for using 'M' suffix (millions)
FORMAT_MILLIONS_THRESHOLD = 1_000_000

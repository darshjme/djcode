"""Keep regression tests out of the developer's real DJcode history/configuration."""
import os
import tempfile

_test_config = tempfile.TemporaryDirectory(prefix="djcode-tests-")
os.environ["DJCODE_CONFIG_DIR"] = _test_config.name
os.environ["DJCODE_NO_UPDATE_CHECK"] = "1"

os.environ["DJCODE_SKIP_STARTUP_CHECK"] = "1"

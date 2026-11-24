import unittest
from types import SimpleNamespace
from unittest.mock import patch

from oss_audit.core import resource_limits


class ResourceAdmission(unittest.TestCase):
    def test_low_disk_and_unknown_memory_pause(self):
        config = {"resources": {"min_available_disk_gib": 5, "min_available_memory_gib": 2}}
        with (
            patch.object(resource_limits.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**3)),
            patch.object(resource_limits, "available_memory", side_effect=OSError("unavailable")),
        ):
            result = resource_limits.admission(config, ".")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reasons"], ["low_disk", "unknown_memory"])

    def test_healthy_host_passes(self):
        config = {"resources": {"min_available_disk_gib": 5, "min_available_memory_gib": 2}}
        with (
            patch.object(
                resource_limits.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)
            ),
            patch.object(resource_limits, "available_memory", return_value=4 * 1024**3),
        ):
            self.assertTrue(resource_limits.admission(config, ".")["ok"])


if __name__ == "__main__":
    unittest.main()

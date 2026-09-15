import unittest

from odoo_manager_core.odoo_log_display import OdooLogDisplay, compact_odoo_log_text


def gc_missing_block(fname, exception="FileNotFoundError: [Errno 2] No such file or directory"):
    path = f"/home/odoo/srv/data/filestore/domeau_15092026/{fname}"
    return "\n".join([
        f"2026-09-15 13:29:13,805 640 INFO domeau_15092026 odoo.addons.base.models.ir_attachment: _file_gc could not unlink {path}",
        "Traceback (most recent call last):",
        '  File "/home/odoo/srv/server/odoo/odoo/addons/base/models/ir_attachment.py", line 219, in _gc_file_store_unsafe',
        "    os.unlink(self._full_path(fname))",
        f"{exception}: '{path}'",
    ])


class OdooLogDisplayTests(unittest.TestCase):
    def test_compacts_only_known_missing_gc_blocks_and_keeps_real_error(self):
        value = "\n".join([
            gc_missing_block("df/df51055e9dc30b9231e1a879115af0b0de5398f6"),
            gc_missing_block("09/091b81309802f710e6728f7b7a28b108ffb8c39e"),
            "2026-09-15 13:29:14,005 640 INFO domeau_15092026 odoo.addons.base.models.ir_attachment: filestore gc 13 checked, 1 removed",
            "2026-09-15 13:29:20,386 640 INFO domeau_15092026 odoo.addons.base.models.ir_cron: Job 'Base: Auto-vacuum internal data' done in 6.865s",
            "2026-09-15 13:30:00,000 640 ERROR domeau_15092026 odoo.modules: Module update failed",
        ])

        result = compact_odoo_log_text(value)

        self.assertIn("2 fichier(s) déjà absent(s)", result)
        self.assertIn("filestore gc 13 checked, 1 removed", result)
        self.assertIn("Auto-vacuum internal data' done", result)
        self.assertIn("ERROR domeau_15092026 odoo.modules: Module update failed", result)
        self.assertNotIn("FileNotFoundError", result)

    def test_keeps_unknown_gc_failure_and_partial_traceback(self):
        permission = gc_missing_block("df/file", "PermissionError: [Errno 13] Permission denied")
        partial = gc_missing_block("09/file").split("\n")[0]

        self.assertEqual(compact_odoo_log_text(permission), permission)
        self.assertEqual(compact_odoo_log_text(partial), partial)

    def test_streaming_and_snapshot_have_same_result(self):
        value = "\n".join([
            gc_missing_block("df/file"),
            "2026-09-15 13:29:14,005 640 INFO domeau_15092026 odoo.addons.base.models.ir_attachment: filestore gc 1 checked, 0 removed",
        ])
        display = OdooLogDisplay()
        output = []
        for line in value.splitlines():
            output.extend(display.feed(line))
        output.extend(display.finish())

        self.assertEqual("\n".join(output), compact_odoo_log_text(value))

    def test_compose_prefixed_log_does_not_hide_following_error(self):
        gc_lines = gc_missing_block("df/file").splitlines()
        value = "\n".join([
            f"odoo-DOMEAU | {gc_lines[0]}",
            *gc_lines[1:],
            "odoo-DOMEAU | 2026-09-15 13:30:00,000 640 ERROR domeau_15092026 odoo.modules: Update failed",
        ])

        result = compact_odoo_log_text(value)

        self.assertIn("1 fichier(s) déjà absent(s)", result)
        self.assertIn("ERROR domeau_15092026 odoo.modules: Update failed", result)


if __name__ == "__main__":
    unittest.main()

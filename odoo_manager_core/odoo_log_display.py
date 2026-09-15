"""Compact known non-fatal Odoo filestore GC tracebacks for display only."""

import re


LOG_RECORD = re.compile(r"^(?:[^|\n]+\|\s*)?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]\d+")
GC_MISSING_HEADER = re.compile(
    r"\bINFO\b.*\bodoo\.addons\.base\.models\.ir_attachment: "
    r"_file_gc could not unlink .*?/filestore/([^/]+)/\S+"
)


class OdooLogDisplay:
    def __init__(self):
        self._pending = []
        self._missing_count = 0
        self._databases = set()

    def _flush_pending(self):
        if not self._pending:
            return []
        lines = self._pending
        self._pending = []
        header = GC_MISSING_HEADER.search(lines[0])
        body = "\n".join(lines[1:])
        if (
            header
            and "_gc_file_store_unsafe" in body
            and "os.unlink" in body
            and "FileNotFoundError: [Errno 2]" in body
        ):
            self._missing_count += 1
            self._databases.add(header.group(1))
            return []
        return lines

    def _flush_summary(self):
        if not self._missing_count:
            return []
        count = self._missing_count
        databases = ", ".join(sorted(self._databases))
        self._missing_count = 0
        self._databases.clear()
        return [
            f"Info Odoo ({databases}) : {count} fichier(s) déjà absent(s) lors du nettoyage "
            "du filestore. Le nettoyage continue ; traces complètes dans les Logs bruts."
        ]

    def feed(self, line):
        line = line.rstrip("\n")
        output = []
        if self._pending and (LOG_RECORD.match(line) or len(self._pending) >= 12):
            output.extend(self._flush_pending())

        if not self._pending and LOG_RECORD.match(line) and GC_MISSING_HEADER.search(line):
            self._pending.append(line)
            return output

        if self._pending:
            self._pending.append(line)
            return output

        output.extend(self._flush_summary())
        output.append(line)
        return output

    def finish(self):
        return [*self._flush_pending(), *self._flush_summary()]


def compact_odoo_log_text(value):
    display = OdooLogDisplay()
    output = []
    for line in value.splitlines():
        output.extend(display.feed(line))
    output.extend(display.finish())
    return "\n".join(output)

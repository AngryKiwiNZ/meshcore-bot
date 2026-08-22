"""Helpers for managing reversible repeater-monitor target suppression."""

import csv
import io
import os
from pathlib import Path
from typing import Dict, List, Optional

_DISABLED_VALUES = {"0", "false", "disabled", "disable", "off", "no", "skip"}


def node_keys_match(left: str, right: str) -> bool:
    left_key = (left or "").strip().lower()
    right_key = (right or "").strip().lower()
    return bool(left_key and right_key) and (
        left_key.startswith(right_key) or right_key.startswith(left_key)
    )


def list_suppressed_targets(nodes_file: str) -> List[Dict[str, str]]:
    path = Path(nodes_file)
    if not path.exists():
        return []
    targets: List[Dict[str, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = next(csv.reader([raw_line], skipinitialspace=True), [])
        if not fields or not (fields[0] or "").strip():
            continue
        status = (fields[2] if len(fields) > 2 else "").strip().lower()
        if status in _DISABLED_VALUES:
            node_key = fields[0].strip().lower()
            targets.append({
                "node_key": node_key,
                "display_name": (fields[1] if len(fields) > 1 else "").strip() or node_key[:12],
            })
    return targets


def restore_suppressed_target(nodes_file: str, node_key: str, display_name: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Enable a matching disabled target, preserving any pinned route."""
    path = Path(nodes_file)
    if not path.exists():
        return None
    updated_lines: List[str] = []
    restored: Optional[Dict[str, str]] = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            updated_lines.append(raw_line)
            continue
        fields = next(csv.reader([raw_line], skipinitialspace=True), [])
        status = (fields[2] if len(fields) > 2 else "").strip().lower() if fields else ""
        existing_key = (fields[0] if fields else "").strip()
        if restored is None and status in _DISABLED_VALUES and node_keys_match(existing_key, node_key):
            while len(fields) < 3:
                fields.append("")
            if display_name and display_name.strip():
                fields[1] = display_name.strip()
            elif not fields[1].strip():
                fields[1] = existing_key[:12]
            fields[2] = "enabled"
            output = io.StringIO()
            csv.writer(output).writerow(fields)
            updated_lines.append(output.getvalue().rstrip("\r\n"))
            restored = {"node_key": existing_key.lower(), "display_name": fields[1].strip()}
        else:
            updated_lines.append(raw_line)
    if restored is None:
        return None
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")
    os.replace(temporary_path, path)
    return restored

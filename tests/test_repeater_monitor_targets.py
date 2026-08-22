from modules.repeater_monitor_targets import list_suppressed_targets, restore_suppressed_target


def test_restore_suppressed_target_preserves_fixed_path(tmp_path):
    nodes_file = tmp_path / "nodes.txt"
    nodes_file.write_text("# targets\nabcdef,Old Name,disabled,0262\n123456,Active,enabled\n", encoding="utf-8")
    restored = restore_suppressed_target(str(nodes_file), "abcdef99", "Current Name")
    assert restored == {"node_key": "abcdef", "display_name": "Current Name"}
    assert "abcdef,Current Name,enabled,0262" in nodes_file.read_text(encoding="utf-8")
    assert list_suppressed_targets(str(nodes_file)) == []


def test_restore_only_matches_disabled_target(tmp_path):
    nodes_file = tmp_path / "nodes.txt"
    nodes_file.write_text("abcdef,Active,enabled\n", encoding="utf-8")
    assert restore_suppressed_target(str(nodes_file), "abcdef") is None
    assert nodes_file.read_text(encoding="utf-8") == "abcdef,Active,enabled\n"

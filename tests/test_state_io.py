from state_io import read_json, write_json


def test_json_state_is_replaced_without_leftover_part(tmp_path):
    path = tmp_path / "queue.json"
    write_json(path, {"status": "pending"})
    write_json(path, {"status": "done"})

    assert read_json(path, {}) == {"status": "done"}
    assert not (tmp_path / "queue.json.part").exists()


def test_invalid_json_returns_default(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")
    assert read_json(path, []) == []

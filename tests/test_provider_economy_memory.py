"""Preserve private operator memory exactly while adding the explicit order."""
from deploy.provider_economy import MEMORY, preserve_addition


def test_memory_addition_preserves_original_and_is_idempotent(tmp_path):
    target = tmp_path / "profile/memories/MEMORY.md"
    target.parent.mkdir(parents=True)
    original = b"Existing private memory\nwith original bytes.\n"
    target.write_bytes(original)
    first = preserve_addition(target, MEMORY.encode(), tmp_path / "backups")
    assert first["status"] == "added" and first["original_preserved"]
    assert target.read_bytes() == MEMORY.encode() + original
    assert next((tmp_path / "backups").glob("*.original")).read_bytes() == original
    second = preserve_addition(target, MEMORY.encode(), tmp_path / "backups")
    assert second["status"] == "already_present" and second["sha256"] == first["sha256"]


def test_absent_memory_is_created_without_changing_other_files(tmp_path):
    target = tmp_path / "profile/memories/MEMORY.md"
    unrelated = tmp_path / "auth.json"
    unrelated.write_bytes(b"do not modify")
    preserve_addition(target, MEMORY.encode(), tmp_path / "backups")
    assert target.read_bytes() == MEMORY.encode()
    assert unrelated.read_bytes() == b"do not modify"

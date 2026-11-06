from app.artifacts import ArtifactCache


def counter():
    calls = []

    def compute():
        calls.append(1)
        return {"text": "page one", "n": len(calls)}

    return calls, compute


def test_computes_once_then_reads_from_disk(tmp_path):
    cache = ArtifactCache(tmp_path)
    calls, compute = counter()
    kw = dict(file_sha="ab" * 32, stage="ocr", version=1, params={"dpi": 200})
    assert cache.get_or_compute_json(**kw, compute=compute) == {"text": "page one", "n": 1}
    assert cache.get_or_compute_json(**kw, compute=compute) == {"text": "page one", "n": 1}
    assert len(calls) == 1


def test_version_or_params_change_recomputes(tmp_path):
    cache = ArtifactCache(tmp_path)
    calls, compute = counter()
    base = dict(file_sha="cd" * 32, stage="ocr", compute=compute)
    cache.get_or_compute_json(**base, version=1, params={"dpi": 200})
    cache.get_or_compute_json(**base, version=2, params={"dpi": 200})
    cache.get_or_compute_json(**base, version=2, params={"dpi": 300})
    assert len(calls) == 3


def test_disabled_always_computes(tmp_path):
    cache = ArtifactCache(tmp_path, enabled=False)
    calls, compute = counter()
    for _ in range(2):
        cache.get_or_compute_json(file_sha="ef" * 32, stage="ocr", version=1, params={}, compute=compute)
    assert len(calls) == 2
    assert not any(tmp_path.iterdir())


def test_bytes_round_trip_and_no_temp_files_left(tmp_path):
    cache = ArtifactCache(tmp_path)
    kw = dict(file_sha="12" * 32, stage="render", version=1, params={"page": 3}, ext=".png")
    data = cache.get_or_compute_bytes(**kw, compute=lambda: b"\x89PNG fake")
    assert cache.get_or_compute_bytes(**kw, compute=lambda: b"other") == data
    assert not [p for p in tmp_path.rglob(".tmp-*")]

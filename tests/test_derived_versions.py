from __future__ import annotations


def test_stage_versions_propagate_staleness():
    from git_synapse.analysis.derived import stale_stages

    assert stale_stages({"aggregate": "2026-09-08.2"}) == {
        "score", "depbump", "declared", "predict", "mining"
    }


def test_derived_stages_rebuild_once_in_dependency_order(scratch_db, monkeypatch):
    from git_synapse.analysis import derived
    from git_synapse.db.engine import connection

    calls: list[str] = []
    with connection() as conn:
        conn.execute(
            "INSERT INTO repo (owner, name, full_name, pair_count) "
            "VALUES ('test', 'derived', 'test/derived', 1)"
        )
    monkeypatch.setattr(derived.aggregate, "rebuild_repo",
                        lambda repo_id, conn: calls.append("aggregate"))
    monkeypatch.setattr(derived.score, "score_all",
                        lambda conn: calls.append("score"))
    monkeypatch.setattr(derived.score, "score_repo",
                        lambda repo_id, conn: calls.append("score"))
    monkeypatch.setattr(derived.depbump, "rebuild",
                        lambda **kwargs: calls.append("depbump"))
    monkeypatch.setattr(derived.depbump, "refresh_declared",
                        lambda **kwargs: calls.append("declared"))
    monkeypatch.setattr(derived.depbump, "refresh_modules",
                        lambda **kwargs: calls.append("modules"))
    monkeypatch.setattr(derived.predict, "rebuild",
                        lambda **kwargs: calls.append("predict"))
    monkeypatch.setattr(derived.mining, "rebuild",
                        lambda **kwargs: calls.append("mining"))

    with connection() as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'watermark:derived:%'")
        first = derived.ensure_current(conn)
    ordered = [name for name in calls if name != "aggregate"]
    expected = ["score", "depbump", "declared", "modules", "predict", "mining"]
    positions = [ordered.index(name) for name in expected]
    assert positions == sorted(positions)
    calls.clear()
    with connection() as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'watermark:derived:%'")
    isolated = derived.ensure_current()
    calls.clear()
    second = derived.ensure_current()

    assert first == [stage.name for stage in derived.STAGES]
    assert isolated == first
    assert second == []
    assert calls == []

    # The connection-supplied path also needs to skip a stage whose watermark
    # is already current; this is the fast path used by service requests.
    with connection() as conn:
        assert derived.ensure_current(conn) == []

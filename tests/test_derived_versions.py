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
    monkeypatch.setattr(derived.aggregate, "rebuild_repo",
                        lambda repo_id, conn: calls.append("aggregate"))
    monkeypatch.setattr(derived.score, "score_all",
                        lambda conn: calls.append("score"))
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
        second = derived.ensure_current(conn)

    assert first == [stage.name for stage in derived.STAGES]
    assert second == []
    ordered = [name for name in calls if name != "aggregate"]
    assert ordered == ["score", "depbump", "declared", "modules", "predict", "mining"]
    if "aggregate" in calls:
        assert calls.index("aggregate") < calls.index("score")

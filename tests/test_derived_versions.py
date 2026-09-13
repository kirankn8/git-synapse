from __future__ import annotations


def test_stage_versions_propagate_staleness():
    from git_synapse.analysis.derived import stale_stages

    assert stale_stages({"aggregate": "2026-09-08.2"}) == {
        "score", "depbump", "declared", "predict", "mining"
    }


def test_derived_stages_rebuild_once_in_dependency_order(scratch_db, monkeypatch):
    from git_synapse.analysis import derived
    from git_synapse.db.orm import models, session_scope

    calls: list[str] = []
    with session_scope() as conn:
        conn.query(models().Repo).filter_by(full_name="test/derived").delete(synchronize_session=False)
        conn.add(models().Repo(owner="test", name="derived", full_name="test/derived", pair_count=1))
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

    with session_scope() as conn:
        conn.query(models().Meta).filter(models().Meta.key.like("watermark:derived:%")).delete(synchronize_session=False)
        first = derived.ensure_current(conn)
    ordered = [name for name in calls if name != "aggregate"]
    expected = ["score", "depbump", "declared", "modules", "predict", "mining"]
    positions = [ordered.index(name) for name in expected]
    assert positions == sorted(positions)
    calls.clear()
    with session_scope() as conn:
        conn.query(models().Meta).filter(models().Meta.key.like("watermark:derived:%")).delete(synchronize_session=False)
    isolated = derived.ensure_current()
    calls.clear()
    second = derived.ensure_current()

    assert first == [stage.name for stage in derived.STAGES]
    assert isolated == first
    assert second == []
    assert calls == []

    with session_scope() as conn:
        assert derived.ensure_current(conn) == []

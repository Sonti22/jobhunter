# -*- coding: utf-8 -*-
"""Маркер «сбор выполнен» не должен зависеть от одного канала из двухсот.

14.09: last_ingest.txt стоял на 07.09, хотя сбор отрабатывал каждый день.
Один канал с ошибкой (10.09 — forpython) делал весь шаг «неуспешным», и
догон при каждом включении машины заново гонял сбор на полчаса с лишним.
"""
from jobhunter.autopilot import ingest_result
from jobhunter.scheduled import has_errors, wrap_marked


def _partial():
    return {
        "careered": {"errors": 1},                        # источник лёг
        "telegram": {"scan_channels": 208, "scan_errors": 1, "new": 812,
                     "scan_error_channels": ["forpython"]},
        "hn": {"seen": 40, "new": 3, "errors": 0},
        "remotive": {"error": "HTTPStatusError"},          # 403
    }


def test_partial_source_failure_marks_ingest_done():
    res = ingest_result(_partial(), new=815, contacts=403)
    assert not has_errors(res), "один упавший источник — не провал всего сбора"
    assert res["failed_sources"] == ["careered", "remotive"]
    assert res["new"] == 815 and res["with_contact"] == 403


def test_details_are_kept_for_diagnostics():
    res = ingest_result(_partial(), new=815, contacts=403)
    by_name = {d["source"]: d for d in res["sources"]}
    assert by_name["telegram"]["scan_errors"] == 1
    assert by_name["remotive"]["error"] == "HTTPStatusError"
    assert "scan_error_channels" not in by_name["telegram"], "списки не тащим"


def test_all_sources_down_is_a_failure():
    totals = {"careered": {"errors": 1}, "remotive": {"error": "ConnectError"},
              "telegram": {"scan_channels": 208, "scan_errors": 208}}
    res = ingest_result(totals, new=0, contacts=0)
    assert has_errors(res)
    assert res["error"]


def test_half_of_telegram_channels_down_counts_as_failed_source():
    res = ingest_result({"telegram": {"scan_channels": 208, "scan_errors": 110},
                         "hn": {"seen": 10, "new": 1}}, new=1, contacts=0)
    assert res["failed_sources"] == ["telegram"]
    assert not has_errors(res), "HN отработал — шаг выполнен"


def test_scheduler_writes_marker_for_partial_ingest(tmp_path, monkeypatch):
    from jobhunter import scheduled
    monkeypatch.setattr(scheduled, "record", lambda *a, **kw: None)
    wrapped = wrap_marked(lambda: ingest_result(_partial(), 815, 403),
                          "ingest", tmp_path)
    wrapped()
    assert (tmp_path / "last_ingest.txt").exists(), \
        "частичный сбой не должен заставлять повторять сбор при каждом старте"
    assert wrapped() is None, "второй запуск за день — пропуск"

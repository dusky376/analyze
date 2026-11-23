"""精华版前端契约：只暴露悟空真经一条策略的接口与页面。

守住三件事：
1. 页面必须存在，且引用的 JS/CSS 可访问；
2. 三个数据出口必须 200 且带悟空真经关键字段；
3. 页面/接口不得再出现 E1–E4、威科夫、LLM 等已删除策略的名称。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(scope="module")
def client():
    import app as app_module
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _json(resp):
    return json.loads(resp.data.decode("utf-8"))


def test_index_and_assets(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "悟空真经" in html
    assert "/app.js" in html and "/style.css" in html
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_version_and_health(client):
    v = _json(client.get("/api/version"))
    assert v["strategy"] == "E5:B1"
    assert "build" in v and "stale" in v

    h = _json(client.get("/api/health"))
    assert h["ok"] is True
    assert "panel" in h and "recorder" in h


def test_live_sim_contract(client):
    d = _json(client.get("/api/live-sim"))
    for key in ("day", "today_orders", "positions", "summary", "entry_rule", "exit_rule"):
        assert key in d
    assert "二板" in d["entry_rule"]
    assert "断板" in d["exit_rule"]


def test_guitu_contract(client):
    d = _json(client.get("/api/guitu"))
    assert "sim" in d and "queue_book" in d
    assert d["wukong_cases"]["detail"] == "/api/guitu-cases"


def test_guitu_cases_contract(client):
    d = _json(client.get("/api/guitu-cases"))
    assert d["available"] is True
    assert "groups" in d and "thesis" in d


def test_no_removed_strategies_in_ui(client):
    html = client.get("/").data.decode("utf-8")
    js = client.get("/app.js").data.decode("utf-8")
    for word in ("威科夫", "冰点二板", "规则 1", "LLM", "蚂蚁觅食",
                 "情绪周期", "周期看板", "题材雷达", "决策总线", "周度复盘"):
        assert word not in html + js


def test_web_only_exposes_wukong_single_page():
    """web/ 只允许悟空真经单页所需文件；禁止旧周期看板等孤儿页回潮。"""
    from pathlib import Path
    import app as app_module

    web = Path(app_module.WEB)
    allowed = {"index.html", "app.js", "style.css", "favicon.svg"}
    actual = {p.name for p in web.iterdir() if p.is_file()}
    assert actual == allowed, f"web/ 出现非悟空文件：{sorted(actual - allowed)}"
    assert not (web / "cycle.html").exists(), "旧周期看板 cycle.html 不应存在"

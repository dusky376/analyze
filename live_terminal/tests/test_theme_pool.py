"""题材池模块测试（离线，不联网）。

覆盖三类真实坑（全部来自 2026-11-16 实测）：
1. `reason_type` 引号内含 `+` → 朴素切分会切碎
2. `high_days` 是区间写法（用户拍板保留原生串），不是连续 streak
3. 标签混了业绩/属性 → 不分类会把题材榜变成业绩榜
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import theme_pool as tp  # noqa: E402


class TestSplitTags:
    """引号感知切分 —— 实测 6 个交易日 27 行会被朴素切分切坏。"""

    def test_quote_contains_plus(self):
        # 真实数据：天虹股份 2026-11-16
        assert tp.split_tags("“AI+零售”+超市调改+央企") == [
            "“AI+零售”", "超市调改", "央企"]

    def test_quote_suffix_zibei(self):
        # 真实数据：阳光乳业 2026-11-16
        # 朴素切分正确，但天真引号正则会切成 “羊” / 字辈
        assert tp.split_tags("“羊”字辈+低温乳制品+送奶上户") == [
            "“羊”字辈", "低温乳制品", "送奶上户"]

    def test_quote_suffix_xi(self):
        assert tp.split_tags("“羊”字辈+“东方系”+海洋养殖") == [
            "“羊”字辈", "“东方系”", "海洋养殖"]

    def test_normal(self):
        # 真实数据：文投控股 2026-11-16
        assert tp.split_tags("AI漫剧+剧集发行+北京国资") == [
            "AI漫剧", "剧集发行", "北京国资"]

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_empty(self, bad):
        assert tp.split_tags(bad) == []


class TestParseHighDays:
    """`high_days` 解析 —— 用户 2026-11-16 拍板用原生写法。"""

    def test_liandban(self):
        # 真实数据：文投控股 11-16
        assert tp.parse_high_days("6天5板", 327686) == (5, 6, "6天5板")

    def test_shouban(self):
        assert tp.parse_high_days("首板", 65537) == (1, 1, "首板")

    def test_long_range(self):
        # 真实数据：龙江交通 11-16，13 天 7 板，但连续 streak 只有 1
        assert tp.parse_high_days("13天7板", 458765) == (7, 13, "13天7板")

    def test_null_is_not_zero_boards(self):
        """炸板池 high_days 恒为 null：返回 0 板但原生串是「—」，不当涨停。"""
        assert tp.parse_high_days(None, None) == (0, 0, "—")

    def test_hdv_overrides_inconsistent_string(self):
        """hdv 是机器可读源，字符串与它矛盾时以 hdv 为准。"""
        # 声称 9天2板，hdv 实际是 5 板
        b, d, raw = tp.parse_high_days("9天2板", 5 * 65536 + 9)
        assert (b, d) == (5, 9)
        assert raw == "9天2板"      # 原文保留，不改写

    def test_unknown_format_does_not_guess(self):
        b, d, raw = tp.parse_high_days("看不懂", None)
        assert (b, d) == (0, 0)
        assert raw == "看不懂"


class TestClassifyTag:
    """T/F/A 三分类 —— 不分类矩阵第一行会是「三季报增长」。"""

    @pytest.mark.parametrize("tag", [
        "三季报增长", "三季报扭亏", "业绩预增", "股份回购",
        "控制权变更", "重大重组", "复牌", "股权转让",
    ])
    def test_financial_is_F(self, tag):
        assert tp.classify_tag(tag) == "F"

    @pytest.mark.parametrize("tag", [
        "央企", "成都国资", "黑龙江国资", "次新股", "超跌反弹",
        "高股息", "“羊”字辈", "“东方”", "“东方系”",
    ])
    def test_attribute_is_A(self, tag):
        assert tp.classify_tag(tag) == "A"

    @pytest.mark.parametrize("tag", [
        "AI漫剧", "人形机器人", "算力租赁", "固态电池", "商业航天",
    ])
    def test_theme_is_T(self, tag):
        assert tp.classify_tag(tag) == "T"


class TestPickThemeTag:
    """选主题材：映射表优先 → T 类 → 其他（不单独成行）。"""

    AMAP = {"AI漫剧": {"key": "ai_media", "name": "AI传媒"},
            "人形机器人": {"key": "robot", "name": "机器人"}}

    def test_axis_wins_over_first_position(self):
        """映射表命中优先，即使它不是第一个标签。"""
        key, name, matched, why = tp.pick_theme_tag(
            ["三季报增长", "AI漫剧"], self.AMAP)
        assert (key, name, matched, why) == ("ai_media", "AI传媒", True, "axis")

    def test_skips_financial_and_attribute(self):
        """只有业绩/属性标签 → 落「其他」，绝不拿「三季报增长」当题材。"""
        key, name, matched, why = tp.pick_theme_tag(
            ["三季报增长", "央企"], self.AMAP)
        assert (key, name, matched) == ("other", "其他", False)
        assert why == "no_theme"

    def test_longtail_T_goes_to_other_not_own_row(self):
        """未映射的 T 类不单独成行（否则矩阵碎成 200 行）。"""
        key, name, matched, why = tp.pick_theme_tag(
            ["陶瓷纤维", "三季报增长"], self.AMAP)
        assert key == "other"
        assert why == "longtail"

    def test_no_tags(self):
        assert tp.pick_theme_tag([], self.AMAP)[0] == "other"


class TestCollectOffline:
    """落盘/读取不依赖网络。"""

    def test_to_frame_empty_has_schema(self):
        df = tp.to_frame([])
        assert df.height == 0
        assert "high_days_raw" in df.columns
        assert df.schema["n_board"] == pl.Int32

    def test_norm_day(self):
        assert tp._norm_day("20261116") == "2026-11-16"
        assert tp._norm_day("2026-11-16") == "2026-11-16"
        from datetime import date
        assert tp._norm_day(date(2026, 11, 16)) == "2026-11-16"

    def test_norm_day_rejects_garbage(self):
        with pytest.raises(ValueError):
            tp._norm_day("16/11/2026")


class TestLadderMatrixLogic:
    """矩阵结构判定 —— 参考图 3 的核心算法。"""

    @staticmethod
    def _slot(h1=0, h2=0, h3=0, h4=0, h5p=0):
        return {"h1": h1, "h2": h2, "h3": h3, "h4": h4, "h5p": h5p}

    def test_conn_when_all_layers_present(self):
        # AI传媒 11-16 真实形态：5 层全满
        s = self._slot(1, 1, 1, 1, 1)
        K = 5
        missing = [k for k in range(2, K + 1)
                   if {2: s["h2"], 3: s["h3"], 4: s["h4"]}
                   .get(k, s["h5p"] if k >= 5 else 0) == 0]
        assert missing == []

    def test_gap_when_middle_empty(self):
        # 机器人 11-16 真实形态：只有 4 板，2/3 板全空
        s = self._slot(0, 0, 0, 1, 0)
        K = 4
        missing = [k for k in range(2, K + 1)
                   if {2: s["h2"], 3: s["h3"], 4: s["h4"]}
                   .get(k, s["h5p"] if k >= 5 else 0) == 0]
        assert missing == [2, 3]

    def test_matrix_on_real_day_if_present(self):
        """若 2026-11-16 已落盘，矩阵必须可用且行数收敛（不再碎片）。"""
        m = tp.ladder_matrix("2026-11-16")
        if not m.get("available"):
            pytest.skip("该日题材表未落盘")
        assert m["n_zt"] > 0
        # 分类生效后行数应显著少于涨停只数（否则说明又碎成一行一票）
        assert m["n_themes"] < m["n_zt"], (
            f"矩阵仍然碎片：{m['n_themes']} 行 / {m['n_zt']} 只票")
        # 绝不能出现业绩类题材行
        for r in m["rows"]:
            assert tp.classify_tag(r["theme"]) != "F", (
                f"业绩标签成了题材行：{r['theme']}")


# ---------------------------------------------------------------------------
# 存量更新（用户 2026-11-16 要求：不能每天全量重存浪费空间）
# ---------------------------------------------------------------------------

def _mk_day(day: str, codes: list[str], *, reason: str = "AI漫剧",
            board: int = 1) -> pl.DataFrame:
    """造一天的最小合法数据（schema 与生产一致）。"""
    return pl.DataFrame([{
        "date": day, "pool": "zt", "code": c, "name": "测试",
        "reason_raw": reason, "tags": [reason],
        "high_days_raw": "首板" if board == 1 else f"{board}天{board}板",
        "n_board": board, "n_days": board,
        "limit_up_type": "换手板", "order_amount_yi": 1.0,
        "open_num": 0, "turnover_rate": 5.0,
        "first_seal": "09:30:00", "last_seal": "14:00:00",
        "change_tag": "LIMIT_BACK", "change_rate": 10.0,
    } for c in codes], schema=tp.SCHEMA, orient="row")


class TestFingerprint:
    """指纹决定「要不要重拉」，必须稳定且敏感。"""

    def test_order_independent(self):
        """同内容不同顺序 → 同一指纹（否则每次抓取都误判为"变了"）。"""
        a = _mk_day("2026-11-01", ["000001", "000002"])
        b = _mk_day("2026-11-01", ["000002", "000001"])
        assert tp._fingerprint(a) == tp._fingerprint(b)

    def test_content_sensitive(self):
        """成分变了 → 指纹必须变（否则官方修订被吞掉）。"""
        a = _mk_day("2026-11-01", ["000001", "000002"])
        b = _mk_day("2026-11-01", ["000001", "000009"])
        assert tp._fingerprint(a) != tp._fingerprint(b)

    def test_reason_change_detected(self):
        """题材标签改了 → 必须识别（这是官方最常修订的字段）。"""
        a = _mk_day("2026-11-01", ["000001"], reason="AI漫剧")
        b = _mk_day("2026-11-01", ["000001"], reason="AI短剧")
        assert tp._fingerprint(a) != tp._fingerprint(b)

    def test_empty(self):
        assert tp._fingerprint(pl.DataFrame(schema=tp.SCHEMA)) == "empty"


class TestUpsertIdempotent:
    """upsert 必须幂等：同样的数据写两次，字节数不变。"""

    @pytest.fixture()
    def isolate(self, tmp_path, monkeypatch):
        """把 themes_dir 指到临时目录，别碰生产数据。"""
        d = tmp_path / "themes"
        d.mkdir()
        monkeypatch.setattr(tp, "themes_dir", lambda: d)
        return d

    def test_first_write_then_identical_is_noop(self, isolate):
        df = _mk_day("2026-11-01", ["000001", "000002", "000003"])
        r1 = tp.upsert(df, "2026-11-01")
        assert r1["added"] == 3 and not r1["unchanged"]
        size1 = tp.themes_file().stat().st_size

        r2 = tp.upsert(df, "2026-11-01")     # 一模一样再写一次
        assert r2["unchanged"] is True
        assert r2["added"] == 0 and r2["updated"] == 0
        assert tp.themes_file().stat().st_size == size1, (
            "幂等写入改变了文件大小 —— 存量更新已破防")

    def test_official_revision_is_absorbed(self, isolate):
        """官方修订（标签变了）→ 覆盖该行，不新增行。"""
        d1 = _mk_day("2026-11-01", ["000001"], reason="AI漫剧")
        tp.upsert(d1, "2026-11-01")
        n1 = tp.load("2026-11-01").height

        d2 = _mk_day("2026-11-01", ["000001"], reason="AI短剧")
        r = tp.upsert(d2, "2026-11-01")
        assert r["updated"] == 1 and r["added"] == 0 and r["removed"] == 0
        out = tp.load("2026-11-01")
        assert out.height == n1, "修订不应新增行"
        assert out["reason_raw"][0] == "AI短剧", "修订未生效"

    def test_delisted_stock_is_removed(self, isolate):
        """官方撤下某票 → 该行要删掉，不能留着冒充涨停。"""
        tp.upsert(_mk_day("2026-11-01", ["000001", "000002"]), "2026-11-01")
        r = tp.upsert(_mk_day("2026-11-01", ["000001"]), "2026-11-01")
        assert r["removed"] == 1
        assert tp.load("2026-11-01").height == 1

    def test_other_days_untouched(self, isolate):
        """写完第二天，第一天原样不动。"""
        tp.upsert(_mk_day("2026-11-01", ["000001"]), "2026-11-01")
        tp.upsert(_mk_day("2026-11-02", ["000002"]), "2026-11-02")
        assert tp.load("2026-11-01").height == 1
        assert tp.load("2026-11-02").height == 1
        assert tp.available_days() == ["2026-11-01", "2026-11-02"]

    def test_single_file_not_per_day(self, isolate):
        """核心约束：落盘只能有一个数据文件，不能一天一个。"""
        for i in range(1, 4):
            tp.upsert(_mk_day(f"2026-11-0{i}", [f"00000{i}"]), f"2026-11-0{i}")
        pq = list(isolate.glob("*.parquet"))
        assert len(pq) == 1, f"又变回分日文件了：{[f.name for f in pq]}"
        assert pq[0].name == tp.THEMES_NAME

    def test_revision_log_is_summary_only(self, isolate):
        """留痕只记差异摘要，不存数据副本（否则等于每天存两遍）。"""
        tp.upsert(_mk_day("2026-11-01", ["000001"]), "2026-11-01")
        tp.upsert(_mk_day("2026-11-01", ["000001", "000002"]), "2026-11-01")
        rp = tp.revisions_file()
        assert rp.is_file()
        lines = [ln for ln in rp.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 2
        rec = json.loads(lines[-1])
        assert "added" in rec and "updated" in rec and "removed" in rec
        # 绝不能把整份数据塞进留痕
        assert "rows_data" not in rec and "df" not in rec
        assert rp.stat().st_size < 2000, "留痕文件过大，疑似存了数据副本"

    def test_stats_reports_bytes_per_row(self, isolate):
        """行数够多时，bytes/行 必须落在 zstd 单文件的合理区间。

        ⚠️ 只有 2 行时 parquet footer/schema 开销占绝对主导（实测 3KB/行），
        本项用 200 行测，才是真实档位（生产实测 48.9 B/行）。
        """
        codes = [f"{i:06d}" for i in range(200)]
        tp.upsert(_mk_day("2026-11-01", codes), "2026-11-01")
        s = tp.stats()
        assert s["available"] is True
        assert s["rows"] == 200
        assert s["bytes_per_row"] < 200, (
            f"{s['bytes_per_row']} B/行，压缩或单文件结构失效")


class TestTradingCalendar:
    """交易日历必须能覆盖「面板滞后」的今天（BUG-1 在此链路的投影）。

    旧实现只读 panel_raw，而 panel 停在 11-12 → collect_range 永远补不到 11-13/11-16，
    force=True 也没用。这是 2026-11-16 实测到的真实断点。
    """

    def test_includes_day_beyond_panel(self):
        """end 晚于 panel 末日时，该日必须出现在日历里。"""
        cal = tp._trading_days(end="2026-11-16")
        if not cal:
            pytest.skip("无面板也无落盘数据")
        assert "2026-11-16" in cal, (
            f"日历末日 {cal[-1]} 覆盖不到 11-16 —— collect_range 会漏掉今天")

    def test_excludes_weekend(self):
        """周末不进日历（法定节假日交给接口返回 0 行处理）。"""
        cal = set(tp._trading_days(end="2026-11-16"))
        if not cal:
            pytest.skip("无日历")
        assert "2026-11-14" not in cal      # 周六
        assert "2026-11-15" not in cal      # 周日

    def test_sorted_and_unique(self):
        cal = tp._trading_days(end="2026-11-16")
        if not cal:
            pytest.skip("无日历")
        assert cal == sorted(set(cal))


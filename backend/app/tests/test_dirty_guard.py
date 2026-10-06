"""脏种与非法态拒写测例。

种子脏批（冻饺 qty=1 dirty、鸡蛋 qty=-3 dirty）必须保持可见；
四路径期望列如下，每个测例头部再列本例期望列，断言失败打印路径名。

| 路径 | 期望列 |
| --- | --- |
| 消费 | 脏批直接扣减 http=409；脏批 qty_remain/status 原样；正区余量合计不减；干净品项 deductions 不含脏批 |
| 入库 | 到期日非法或数量为负 http=400；lots 行数不增；正区余量合计不变 |
| 下架 | 未到期批下架 http=409；status 仍为 on_shelf；expire-sweep 不扫它 |
| 顶条 | alerts 无 qty_remain<=0 行；负余量批（含 warn 窗口内）不出现时 soon；soon 行全为正余量 |

每个测例走双通道：HTTP（TestClient）与全层读取（直连 sqlite 读全层 lots），两通道同结论。
"""
import os
import shutil
import sqlite3
import tempfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.engines.fefo import consume_fefo, sort_lots_fefo
from app.main import app

# 路径名：断言失败时打印
P_CONSUME = "消费"
P_INBOUND = "入库"
P_REMOVE = "下架"
P_ALERTS = "顶条"
P_SEED = "种子可见"

# 种子脏批主键（seed.py 固定写入顺序）
DIRTY_DUMPLING_ID = 4   # 冻饺 qty_remain=1 已过期 dirty
DIRTY_EGG_ID = 5        # 鸡蛋 qty_remain=-3 dirty


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    """每测例一座全新冷库：种子脏批原样落盘。

    库文件优先放 /dev/shm（tmpfs），规避慢速 overlayfs 上 sqlite fsync 的秒级开销。
    """
    shm = "/dev/shm" if os.path.isdir("/dev/shm") else None
    data_dir = tempfile.mkdtemp(prefix="pfifo-", dir=shm) if shm else str(tmp_path)
    monkeypatch.setenv("DATA_DIR", data_dir)
    from app import seed
    seed.init_db()
    with TestClient(app) as client:
        yield client, os.path.join(data_dir, "pantryfifo.db")
    shutil.rmtree(data_dir, ignore_errors=True)


def read_all_layers(db_file):
    """全层读取通道：绕过 HTTP，直连库表读全层 lots。"""
    c = sqlite3.connect(db_file)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT lots.*, items.name, items.layer FROM lots "
        "JOIN items ON items.id=lots.item_id ORDER BY lots.id")]
    c.close()
    return rows


def table_count(db_file, table):
    c = sqlite3.connect(db_file)
    n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    c.close()
    return n


def positive_total(rows):
    """正区余量合计。"""
    return sum(r["qty_remain"] for r in rows if r["qty_remain"] > 0)


def by_id(rows):
    return {r["id"]: r for r in rows}


# ---------- 种子可见 ----------

def test_seed_dirty_visible(fx):
    """路径=种子可见：负数量与 dirty 冻饺保持可见。

    期望列: 冻饺 dirty qty_remain=1 status=on_shelf | 鸡蛋 dirty qty_remain=-3 status=on_shelf
    """
    client, db_file = fx
    channels = {"HTTP": client.get("/api/fridge").json(), "全层读取": read_all_layers(db_file)}
    for ch, rows in channels.items():
        dum = [r for r in rows if r["name"] == "冻饺" and r["data_quality"] == "dirty"]
        assert len(dum) == 1 and dum[0]["qty_remain"] == 1 and dum[0]["status"] == "on_shelf", \
            f"[{P_SEED}/{ch}] dirty 冻饺须原样可见: {dum}"
        egg = [r for r in rows if r["name"] == "鸡蛋" and r["data_quality"] == "dirty"]
        assert len(egg) == 1 and egg[0]["qty_remain"] == -3 and egg[0]["status"] == "on_shelf", \
            f"[{P_SEED}/{ch}] 负数量脏批须原样可见: {egg}"


# ---------- 消费 ----------

def test_consume_dirty_batch_rejected(fx):
    """路径=消费：对脏批直接按临期确认扣减必须失败，正区余量不减。

    期望列: http=409 | 脏批(冻饺 id=4) qty_remain=1 | status=on_shelf
            | 正区余量合计不变 | consumptions 行数不变
    """
    client, db_file = fx
    before = read_all_layers(db_file)
    r = client.post("/api/consume", json={"item_id": 3, "qty": 1, "note": "临期确认"})
    assert r.status_code == 409, f"[{P_CONSUME}] 脏批直接扣减应 409: {r.status_code} {r.text}"
    after = read_all_layers(db_file)
    dirty = by_id(after)[DIRTY_DUMPLING_ID]
    assert dirty["qty_remain"] == 1 and dirty["status"] == "on_shelf" \
        and dirty["data_quality"] == "dirty", f"[{P_CONSUME}] 脏批须锁定原样: {dirty}"
    assert positive_total(after) == positive_total(before), \
        f"[{P_CONSUME}] 正区余量不减: {positive_total(before)} -> {positive_total(after)}"
    assert table_count(db_file, "consumptions") == 0, \
        f"[{P_CONSUME}] 拒写不得落 consumptions: {table_count(db_file, 'consumptions')}"
    # HTTP 通道同结论：全层视图里脏冻饺仍 1 袋在架
    fridge = client.get("/api/fridge").json()
    f_dirty = [x for x in fridge if x["id"] == DIRTY_DUMPLING_ID]
    assert len(f_dirty) == 1 and f_dirty[0]["qty_remain"] == 1, \
        f"[{P_CONSUME}] HTTP 全层视图脏批须原样可见: {f_dirty}"


def test_consume_clean_item_skips_dirty(fx):
    """路径=消费：干净品项扣减只动干净批，脏批不得被当成普通正余量扣光。

    期望列: http=200 | deductions 仅含干净批 id=3 take=1 | 干净批 12->11 | 脏批 id=5 qty_remain=-3 不变
    """
    client, db_file = fx
    r = client.post("/api/consume", json={"item_id": 2, "qty": 1})
    assert r.status_code == 200, f"[{P_CONSUME}] 干净品项扣减应 200: {r.status_code} {r.text}"
    ded = r.json()["deductions"]
    assert [d["lot_id"] for d in ded] == [3], f"[{P_CONSUME}] FEFO 候选不得含脏批: {ded}"
    rows = by_id(read_all_layers(db_file))
    assert rows[3]["qty_remain"] == 11, f"[{P_CONSUME}] 干净批应 12->11: {rows[3]}"
    assert rows[DIRTY_EGG_ID]["qty_remain"] == -3 \
        and rows[DIRTY_EGG_ID]["status"] == "on_shelf", \
        f"[{P_CONSUME}] 脏批不得被扣: {rows[DIRTY_EGG_ID]}"


def test_engine_fefo_quarantines_dirty():
    """路径=消费（引擎层）：隔离已实现——脏批不得出现在普通 FEFO 候选。

    期望列: sort_lots_fefo 候选无脏批 | 仅剩脏批时 consume_fefo ok=False 且 short=需求量
            | 缺省 data_quality 视为干净（兼容旧测例）
    """
    dirty = {"id": 9, "qty_remain": 5, "expiry": "2026-01-01", "data_quality": "dirty"}
    clean = {"id": 8, "qty_remain": 2, "expiry": "2026-02-01", "data_quality": "clean"}
    cand = sort_lots_fefo([dirty, clean])
    assert [l["id"] for l in cand] == [8], f"[{P_CONSUME}] 脏批混入 FEFO 候选: {cand}"
    r = consume_fefo([dict(dirty)], 1)
    assert r["ok"] is False and r["short"] == 1, f"[{P_CONSUME}] 仅剩脏批时必须报缺: {r}"
    legacy = consume_fefo([{"id": 1, "qty_remain": 2, "expiry": "2026-01-01"}], 1)
    assert legacy["ok"], f"[{P_CONSUME}] 缺省 data_quality 应视为干净: {legacy}"


# ---------- 入库 ----------

def test_inbound_rejects_negative_qty(fx):
    """路径=入库：数量为负必须拒写。

    期望列: http=400 | lots 行数不增 | 正区余量合计不变
    """
    client, db_file = fx
    before = read_all_layers(db_file)
    r = client.post("/api/lots", json={"item_id": 1, "qty": -2, "expiry": "2026-12-01"})
    assert r.status_code == 400, f"[{P_INBOUND}] 负数量入库应 400: {r.status_code} {r.text}"
    after = read_all_layers(db_file)
    assert len(after) == len(before), \
        f"[{P_INBOUND}] lots 行数不增: {len(before)} -> {len(after)}"
    assert positive_total(after) == positive_total(before), \
        f"[{P_INBOUND}] 正区余量不变: {positive_total(before)} -> {positive_total(after)}"
    fridge = client.get("/api/fridge").json()
    assert len(fridge) == len([x for x in before if x["status"] == "on_shelf"]), \
        f"[{P_INBOUND}] HTTP 全层视图行数同结论: {len(fridge)}"


def test_inbound_rejects_bad_expiry(fx):
    """路径=入库：到期日非法必须拒写。

    期望列: 每个非法到期日 http=400 | lots 行数不增
    非法样例: "not-a-date" | "2026-13-40" | "" | "2026-1-1"
    """
    client, db_file = fx
    before = read_all_layers(db_file)
    for bad in ["not-a-date", "2026-13-40", "", "2026-1-1"]:
        r = client.post("/api/lots", json={"item_id": 1, "qty": 1, "expiry": bad})
        assert r.status_code == 400, f"[{P_INBOUND}] 非法到期日 {bad!r} 应 400: {r.status_code} {r.text}"
        assert len(read_all_layers(db_file)) == len(before), \
            f"[{P_INBOUND}] 非法到期日 {bad!r} 入库后 lots 行数不增"


def test_inbound_valid_still_accepted(fx):
    """路径=入库（正控）：合法入库仍成功。

    期望列: http=200 | lots 行数 +1 | 新批 status=on_shelf data_quality=clean
    """
    client, db_file = fx
    before = read_all_layers(db_file)
    r = client.post("/api/lots", json={"item_id": 1, "qty": 2, "expiry": "2026-12-15"})
    assert r.status_code == 200, f"[{P_INBOUND}] 合法入库应 200: {r.status_code} {r.text}"
    after = read_all_layers(db_file)
    assert len(after) == len(before) + 1, \
        f"[{P_INBOUND}] 合法入库 lots 行数应 +1: {len(before)} -> {len(after)}"
    new = by_id(after)[r.json()["id"]]
    assert new["status"] == "on_shelf" and new["data_quality"] == "clean", \
        f"[{P_INBOUND}] 新批应在架干净: {new}"


# ---------- 下架 ----------

def test_remove_unexpired_rejected(fx):
    """路径=下架：对未到期批提交下架必须失败，status 仍为 on_shelf。

    期望列: http=409 | status=on_shelf | expire-sweep 不含该批 | HTTP 全层视图仍在架
    """
    client, db_file = fx
    r = client.post("/api/lots", json={"item_id": 2, "qty": 6, "expiry": "2099-01-01"})
    lid = r.json()["id"]
    r = client.post(f"/api/lots/{lid}/remove")
    assert r.status_code == 409, f"[{P_REMOVE}] 未到期批下架应 409: {r.status_code} {r.text}"
    row = by_id(read_all_layers(db_file))[lid]
    assert row["status"] == "on_shelf", f"[{P_REMOVE}] 拒写后 status 仍为 on_shelf: {row}"
    sweep = client.post("/api/expire-sweep").json()
    assert lid not in sweep["expired_ids"], f"[{P_REMOVE}] 未到期批不得被清扫下架: {sweep}"
    row = by_id(read_all_layers(db_file))[lid]
    assert row["status"] == "on_shelf", f"[{P_REMOVE}] 清扫后 status 仍为 on_shelf: {row}"
    fridge = client.get("/api/fridge").json()
    assert any(x["id"] == lid for x in fridge), f"[{P_REMOVE}] HTTP 全层视图应在架可见"


def test_remove_expired_accepted(fx):
    """路径=下架（正控）：已到期批可下架。

    期望列: http=200 | status=removed
    """
    client, db_file = fx
    r = client.post("/api/lots", json={"item_id": 2, "qty": 1, "expiry": "2020-01-01"})
    lid = r.json()["id"]
    r = client.post(f"/api/lots/{lid}/remove")
    assert r.status_code == 200, f"[{P_REMOVE}] 已到期批下架应 200: {r.status_code} {r.text}"
    row = by_id(read_all_layers(db_file))[lid]
    assert row["status"] == "removed", f"[{P_REMOVE}] 已到期批下架后 status=removed: {row}"


# ---------- 顶条 ----------

def test_alerts_never_treat_negative_as_soon(fx):
    """路径=顶条：负余量不得当普通 soon。

    期望列: alerts 每行 qty_remain>0 | 种子负批(id=5)缺席 | warn 窗口内新增负批也缺席
            | 全层读取里负批仍在架可见（双通道同结论：可见但不报）
    """
    client, db_file = fx
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    c = sqlite3.connect(db_file)
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) "
        "VALUES (2,-1,-1,?,'on_shelf','dirty')", (tomorrow,))
    neg_id = cur.lastrowid
    c.commit(); c.close()

    alerts = client.get("/api/alerts").json()
    assert all(a["qty_remain"] > 0 for a in alerts), \
        f"[{P_ALERTS}] 顶条不得出现非正余量: {[a for a in alerts if a['qty_remain'] <= 0]}"
    assert all(a["id"] not in (DIRTY_EGG_ID, neg_id) for a in alerts), \
        f"[{P_ALERTS}] 负余量批不得进顶条: ids={[a['id'] for a in alerts]}"
    soon = [a for a in alerts if a["level"] == "soon"]
    assert all(a["qty_remain"] > 0 for a in soon), f"[{P_ALERTS}] soon 行必须为正余量: {soon}"
    # 全层读取通道：负批本体仍在架可见，与顶条同结论（可见但不报 soon）
    neg = by_id(read_all_layers(db_file))[neg_id]
    assert neg["status"] == "on_shelf" and neg["qty_remain"] == -1, \
        f"[{P_ALERTS}] 负批应在架可见: {neg}"
    fridge = client.get("/api/fridge").json()
    assert any(x["id"] == neg_id for x in fridge), \
        f"[{P_ALERTS}] HTTP 全层视图同结论：负批可见但不进顶条"

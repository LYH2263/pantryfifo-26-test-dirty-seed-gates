"""脏种与非法态拒写测例.

四条路径期望列(失败时 assert 消息打印路径名):

  [consume] 消费: 脏批(含负余量)不进普通 FEFO 候选; 对脏批直接按临期确认扣减
            -> HTTP 409, 正区余量不减, 脏批不得被当成普通正余量扣光.
  [inbound] 入库: 到期日非法或数量为负 -> HTTP 400, lots 行数不增.
  [pull]    下架: 对未到期批提交下架 -> HTTP 409, status 仍为 on_shelf.
  [alerts]  顶条: 负余量不得被当成普通 soon 出现.

双通道同结论: 每条写路径同时用 HTTP 响应与 /api/fridge 全层读取
(行数校验直读 SQLite)校验同一结论.

隔离清洗已实现: data_quality='dirty' 的批在引擎(sort_lots_fefo)与
HTTP(/api/consume 查询)两层都被排除出普通 FEFO 候选, 因此测例直接断言
候选排除; 即便退回未隔离实现, "脏批不被扣光、正区余量不减" 的结果断言
仍是必须守住的底线.
"""
import pytest
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, sort_lots_fefo
from app.main import app

# 种子常量(与 app/seed.py 对齐; 今日 2026-10-06 视角)
ITEM_EGG = 2            # 鸡蛋: 正区 12(lot 3) + 脏负 -3(lot 5)
ITEM_DUMPLING = 3       # 冻饺: 只有脏批(lot 4)
CLEAN_FRESH_LOT = 3     # 鸡蛋 qty 12, 2026-11-01 未到期, clean
DIRTY_EXPIRED_LOT = 4   # 冻饺 qty 1, 2025-01-01 已过期, dirty
DIRTY_NEGATIVE_LOT = 5  # 鸡蛋 qty -3, 2026-12-01 未到期, dirty
SEED_LOT_ROWS = 5

EXPECTATIONS = {
    "consume": "脏批拒扣: HTTP 409, 正区余量不减, 脏批锁死不被扣光",
    "inbound": "非法到期日/负数量: HTTP 400, lots 行数不增",
    "pull": "未到期批提交下架: HTTP 409, status 仍为 on_shelf",
    "alerts": "负余量不得被当成普通 soon 出现在顶条",
}


def check(path, cond, msg):
    assert cond, f"[{path}] {msg} | 期望: {EXPECTATIONS[path]}"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    with TestClient(app) as c:  # startup 重跑 init_db, 幂等
        yield c


def fridge_rows(client):
    """全层读取通道: /api/fridge 不带层过滤."""
    return client.get("/api/fridge").json()


def lot_row(lot_id):
    c = connect()
    r = c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def lots_count():
    c = connect()
    n = c.execute("SELECT COUNT(*) c FROM lots").fetchone()["c"]
    c.close()
    return n


# ---------- 种子可见性(全层读取通道) ----------

def test_seed_dirty_and_negative_visible(client):
    rows = {r["id"]: r for r in fridge_rows(client)}
    check("consume", DIRTY_EXPIRED_LOT in rows
          and rows[DIRTY_EXPIRED_LOT]["data_quality"] == "dirty",
          f"dirty 冻饺批应保持可见: {rows.get(DIRTY_EXPIRED_LOT)}")
    check("consume", DIRTY_NEGATIVE_LOT in rows
          and rows[DIRTY_NEGATIVE_LOT]["qty_remain"] == -3,
          f"负数量种子应保持可见: {rows.get(DIRTY_NEGATIVE_LOT)}")


# ---------- [consume] 消费 ----------

def test_consume_dirty_lot_rejected(client):
    """对脏批直接按临期确认扣减必须失败且正区余量不减."""
    before = {r["id"]: r["qty_remain"] for r in fridge_rows(client)}
    pos_before = sum(q for q in before.values() if q > 0)

    resp = client.post("/api/consume", json={"item_id": ITEM_DUMPLING, "qty": 1})
    check("consume", resp.status_code == 409,
          f"脏冻饺临期确认扣减应 409, 实得 {resp.status_code}: {resp.text}")

    after = {r["id"]: r["qty_remain"] for r in fridge_rows(client)}
    check("consume", sum(q for q in after.values() if q > 0) == pos_before,
          f"正区余量不减: {pos_before} -> {sum(q for q in after.values() if q > 0)}")
    check("consume", after == before,
          f"拒写后全层余量不得变化: {before} -> {after}")
    check("consume", lot_row(DIRTY_EXPIRED_LOT)["qty_remain"] == 1,
          f"脏批不得被当成普通正余量扣光: {lot_row(DIRTY_EXPIRED_LOT)}")


def test_consume_mixed_item_locks_dirty(client):
    """同品项正区+脏负并存: 只扣干净正区, 脏负余量锁死不凑数."""
    resp = client.post("/api/consume", json={"item_id": ITEM_EGG, "qty": 13})
    check("consume", resp.status_code == 409,
          f"超出正区 12 不得拿脏负余量凑数, 实得 {resp.status_code}: {resp.text}")
    check("consume", resp.json()["detail"]["short"] == 1,
          f"缺口应精确为 1(脏批 -3 不计入可用): {resp.json()}")

    resp = client.post("/api/consume", json={"item_id": ITEM_EGG, "qty": 2})
    check("consume", resp.status_code == 200,
          f"正区内消费应成功, 实得 {resp.status_code}: {resp.text}")
    check("consume", [d["lot_id"] for d in resp.json()["deductions"]] == [CLEAN_FRESH_LOT],
          f"扣减只能落在干净批 lot{CLEAN_FRESH_LOT}: {resp.json()}")
    check("consume", lot_row(DIRTY_NEGATIVE_LOT)["qty_remain"] == -3,
          f"脏负余量保持 -3 锁死: {lot_row(DIRTY_NEGATIVE_LOT)}")
    check("consume", lot_row(CLEAN_FRESH_LOT)["qty_remain"] == 10,
          f"正区 12-2=10: {lot_row(CLEAN_FRESH_LOT)}")


def test_engine_excludes_dirty_candidates():
    """引擎层: 脏批不得出现在普通 FEFO 候选(隔离清洗)."""
    lots = [
        {"id": 1, "qty_remain": 5, "expiry": "2026-01-01", "data_quality": "dirty"},
        {"id": 2, "qty_remain": 5, "expiry": "2026-02-01", "data_quality": "clean"},
        {"id": 3, "qty_remain": -2, "expiry": "2026-01-15", "data_quality": "dirty"},
    ]
    check("consume", [l["id"] for l in sort_lots_fefo(lots)] == [2],
          f"FEFO 候选只剩干净正区: {sort_lots_fefo(lots)}")
    r = consume_fefo([lots[0]], 1)
    check("consume", not r["ok"] and r["short"] == 1,
          f"纯脏批扣减必须失败: {r}")


# ---------- [inbound] 入库 ----------

def test_inbound_negative_qty_rejected(client):
    n0, f0 = lots_count(), len(fridge_rows(client))
    for bad in (-2, 0):
        resp = client.post("/api/lots", json={"item_id": 1, "qty": bad, "expiry": "2026-12-01"})
        check("inbound", resp.status_code == 400,
              f"qty={bad} 应 400, 实得 {resp.status_code}: {resp.text}")
    check("inbound", lots_count() == n0,
          f"lots 行数不增: {n0} -> {lots_count()}")
    check("inbound", len(fridge_rows(client)) == f0,
          f"全层读取行数不增: {f0} -> {len(fridge_rows(client))}")


def test_inbound_bad_expiry_rejected(client):
    n0 = lots_count()
    for bad in ("2026-13-40", "2026-02-30", "not-a-date", "2026-1-1", ""):
        resp = client.post("/api/lots", json={"item_id": 1, "qty": 1, "expiry": bad})
        check("inbound", resp.status_code == 400,
              f"expiry={bad!r} 应 400, 实得 {resp.status_code}: {resp.text}")
    check("inbound", lots_count() == n0,
          f"lots 行数不增: {n0} -> {lots_count()}")
    # 正对照: 合法入库仍成功, 证明拒的是非法态而非入库本身
    resp = client.post("/api/lots", json={"item_id": 1, "qty": 1, "expiry": "2026-12-05"})
    check("inbound", resp.status_code == 200 and lots_count() == n0 + 1,
          f"合法入库不受影响, 实得 {resp.status_code}: {resp.text}")


# ---------- [pull] 下架 ----------

def test_pull_unexpired_rejected(client):
    resp = client.post(f"/api/lots/{CLEAN_FRESH_LOT}/pull")
    check("pull", resp.status_code == 409,
          f"未到期干净批下架应 409, 实得 {resp.status_code}: {resp.text}")
    check("pull", lot_row(CLEAN_FRESH_LOT)["status"] == "on_shelf",
          f"status 仍为 on_shelf: {lot_row(CLEAN_FRESH_LOT)}")
    check("pull", CLEAN_FRESH_LOT in {r["id"] for r in fridge_rows(client)},
          "全层读取仍在架(双通道同结论)")

    resp = client.post(f"/api/lots/{DIRTY_NEGATIVE_LOT}/pull")
    check("pull", resp.status_code == 409,
          f"未到期脏批下架同样失败, 实得 {resp.status_code}: {resp.text}")
    check("pull", lot_row(DIRTY_NEGATIVE_LOT)["status"] == "on_shelf",
          f"status 仍为 on_shelf: {lot_row(DIRTY_NEGATIVE_LOT)}")

    resp = client.post("/api/lots/999/pull")
    check("pull", resp.status_code == 404,
          f"不存在批次应 404, 实得 {resp.status_code}: {resp.text}")


def test_pull_expired_ok_and_sweep_skips_unexpired(client):
    resp = client.post(f"/api/lots/{DIRTY_EXPIRED_LOT}/pull")
    check("pull", resp.status_code == 200,
          f"已到期批下架应成功, 实得 {resp.status_code}: {resp.text}")
    check("pull", lot_row(DIRTY_EXPIRED_LOT)["status"] == "expired",
          f"下架后 status=expired: {lot_row(DIRTY_EXPIRED_LOT)}")
    check("pull", DIRTY_EXPIRED_LOT not in {r["id"] for r in fridge_rows(client)},
          "下架后全层读取不再见(双通道同结论)")

    client.post("/api/expire-sweep")
    check("pull", lot_row(CLEAN_FRESH_LOT)["status"] == "on_shelf",
          f"sweep 不得碰未到期干净批: {lot_row(CLEAN_FRESH_LOT)}")
    check("pull", lot_row(DIRTY_NEGATIVE_LOT)["status"] == "on_shelf",
          f"sweep 不得碰未到期脏负批: {lot_row(DIRTY_NEGATIVE_LOT)}")


# ---------- [alerts] 顶条 ----------

def test_alerts_never_treat_negative_as_soon(client):
    alerts = client.get("/api/alerts").json()
    check("alerts", all(a["qty_remain"] > 0 for a in alerts),
          f"顶条不得出现非正余量: {alerts}")
    check("alerts", DIRTY_NEGATIVE_LOT not in {a["id"] for a in alerts},
          f"负余量脏批不得当普通 soon: {alerts}")
    check("alerts", not [a for a in alerts if a["level"] == "soon" and a["qty_remain"] <= 0],
          f"soon 档位不得含负余量: {alerts}")
    # 双通道对照: 同一负余量批全层读取可见, 顶条不可见
    check("alerts", DIRTY_NEGATIVE_LOT in {r["id"] for r in fridge_rows(client)},
          "负余量批在全层读取保持可见(与顶条结论互证)")

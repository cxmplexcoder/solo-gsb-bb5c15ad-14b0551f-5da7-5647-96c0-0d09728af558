"""分片认领（chunk-claims）端到端测试。"""
from __future__ import annotations

import hashlib
import threading
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app import db, settings, storage


def _create(client: TestClient, size: int = 1000, name: str = "data.bin") -> str:
    data = bytes((i * 7 + 3) % 256 for i in range(size))
    resp = client.post(
        "/api/v1/sessions",
        json={
            "filename": name,
            "total_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _claim(client: TestClient, sid: str, start: int, end: int):
    r = client.post(
        f"/api/v1/sessions/{sid}/chunk-claims",
        json={"start": start, "end": end},
    )
    return r


def _expire_all_claims(sid: str) -> None:
    """直接把该会话全部认领令牌改成已过期（确定性模拟 5 分钟到期）。"""
    conn = db.get_conn()
    conn.execute(
        "UPDATE chunk_claims SET expires_at = ? WHERE session_id = ?",
        ("2000-01-01T00:00:00Z", sid),
    )


# ---------------------------------------------------------------------------
# 创建认领
# ---------------------------------------------------------------------------


def test_create_claim_returns_token_and_5min_expiry(client: TestClient) -> None:
    sid = _create(client, size=1000)
    before = storage.utcnow()
    r = _claim(client, sid, 100, 300)
    after = storage.utcnow()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["session_id"] == sid
    assert body["start"] == 100
    assert body["end"] == 300
    assert isinstance(body["token"], str) and len(body["token"]) >= 32
    assert body["claim_id"]
    created = storage.parse_iso(body["created_at"])
    expires = storage.parse_iso(body["expires_at"])
    assert before - timedelta(seconds=1) <= created <= after + timedelta(seconds=1)
    delta = expires - created
    assert delta == timedelta(seconds=settings.chunk_claim_ttl_seconds)
    assert settings.chunk_claim_ttl_seconds == 300

    # 明文令牌不落库，库里只存哈希。
    row = db.get_conn().execute(
        "SELECT * FROM chunk_claims WHERE id = ?", (body["claim_id"],)
    ).fetchone()
    assert row["status"] == "active"
    assert row["token_hash"] == hashlib.sha256(
        body["token"].encode("ascii")
    ).hexdigest()
    assert body["token"] not in (row["token_hash"],)


def test_claim_rejects_invalid_ranges_and_session_states(client: TestClient) -> None:
    sid = _create(client, size=1000)

    # start >= end
    assert _claim(client, sid, 500, 500).status_code == 422
    assert _claim(client, sid, 600, 500).status_code == 422

    # 越界
    r = _claim(client, sid, 900, 1001)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "claim range exceeds total_size"

    # 未知会话
    assert client.post(
        "/api/v1/sessions/does-not-exist/chunk-claims",
        json={"start": 0, "end": 10},
    ).status_code == 404

    # 已收区间不可认领（与已收重叠）
    data_payload = b"x" * 100
    ok = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=100", content=data_payload
    )
    assert ok.status_code == 200, ok.text
    r = _claim(client, sid, 50, 150)
    assert r.status_code == 409
    assert "already received" in r.json()["detail"]["error"]
    # 相邻但不重叠的缺失区间可以认领。
    assert _claim(client, sid, 100, 200).status_code == 201


def test_claim_on_completed_session_conflicts_and_aborted_gone(
    client: TestClient,
) -> None:
    # 空文件可直接完成。
    sid = _create(client, size=0)
    assert client.post(f"/api/v1/sessions/{sid}/complete").status_code == 200
    r = _claim(client, sid, 0, 0)  # start<end 的约束先拦：用非空区间
    assert r.status_code == 422
    assert client.post(
        f"/api/v1/sessions/{sid}/chunk-claims",
        json={"start": 0, "end": 1},
    ).status_code == 409

    # 放弃后会话行级联删除：404。
    sid2 = _create(client, size=10)
    r = _claim(client, sid2, 0, 5)
    assert r.status_code == 201
    assert client.delete(f"/api/v1/sessions/{sid2}").status_code == 204
    assert _claim(client, sid2, 0, 5).status_code == 404
    # 认领行随外键级联删除。
    assert db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM chunk_claims WHERE session_id = ?", (sid2,)
    ).fetchone()["n"] == 0


def test_claim_ttl_slides_session_expiry(client: TestClient) -> None:
    sid = _create(client, size=1000)
    # 把会话活动时间拨到较早之前（仍未过期），创建认领应按“当前时间 +
    # SESSION_TTL”顺延，而不是沿用旧 expires_at。
    old = storage.iso(storage.utcnow() - timedelta(hours=2))
    old_expires = storage.iso(storage.utcnow() + timedelta(minutes=5))
    db.get_conn().execute(
        "UPDATE upload_sessions SET last_access_at = ?, expires_at = ? WHERE id = ?",
        (old, old_expires, sid),
    )
    _claim(client, sid, 0, 100)
    row = db.get_conn().execute(
        "SELECT expires_at, last_access_at FROM upload_sessions WHERE id = ?", (sid,)
    ).fetchone()
    expected = storage.utcnow() + timedelta(seconds=settings.ttl_seconds)
    assert abs((storage.parse_iso(row["expires_at"]) - expected).total_seconds()) < 5
    assert row["expires_at"] > old_expires


# ---------------------------------------------------------------------------
# 相交的有效认领只能有一个
# ---------------------------------------------------------------------------


def test_overlapping_active_claim_rejected(client: TestClient) -> None:
    sid = _create(client, size=1000)
    first = _claim(client, sid, 100, 300)
    assert first.status_code == 201

    # 完全相同
    r = _claim(client, sid, 100, 300)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "an overlapping claim is already active"
    assert detail["active_claim_expires_at"] == first.json()["expires_at"]

    # 部分重叠 / 子段 / 超集 / 边界跨越
    for s, e in [(200, 400), (150, 250), (0, 1000), (299, 300)]:
        assert _claim(client, sid, s, e).status_code == 409, (s, e)

    # 仅相邻（不相交）的区间允许各自认领。
    second = _claim(client, sid, 300, 500)
    assert second.status_code == 201, second.text
    before = _claim(client, sid, 0, 100)
    assert before.status_code == 201, before.text

    # 新令牌与旧令牌不同。
    assert second.json()["token"] != first.json()["token"]


def test_concurrent_claims_only_one_active(client: TestClient) -> None:
    sid = _create(client, size=1000)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        r = _claim(client, sid, 0, 200)
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1
    assert sorted(results).count(409) == 7
    assert db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM chunk_claims "
        "WHERE session_id = ? AND status = 'active'",
        (sid,),
    ).fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# 过期后可重领，旧令牌不可用
# ---------------------------------------------------------------------------


def test_expired_claim_can_be_reclaimed_and_old_token_dead(
    client: TestClient,
) -> None:
    sid = _create(client, size=1000)
    first = _claim(client, sid, 100, 300).json()

    # 未过期：无令牌不能提交该区间。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=100&end=300", content=b"a" * 200
    )
    assert r.status_code == 409
    assert "claimed" in r.json()["detail"]["error"]

    _expire_all_claims(sid)

    # 过期后可重领同一区间，拿到全新令牌。
    second = _claim(client, sid, 100, 300)
    assert second.status_code == 201, second.text
    new_token = second.json()["token"]
    assert new_token != first["token"]

    # 旧令牌提交：相交于新的有效认领，但哈希不匹配 → 403。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=100&end=300",
        content=b"a" * 200,
        headers={"X-Chunk-Claim": first["token"]},
    )
    assert r.status_code == 403
    assert "does not match" in r.json()["detail"]["error"]

    # 过期后但没有任何有效认领覆盖的区间，未认领沿用现有行为（直接 200）。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=50", content=b"b" * 50
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# X-Chunk-Claim 与 PUT /chunks 的配合
# ---------------------------------------------------------------------------


def test_claimed_chunk_requires_matching_token_and_exact_range(
    client: TestClient,
) -> None:
    sid = _create(client, size=1000)
    body = _claim(client, sid, 100, 300).json()
    token = body["token"]

    # 令牌伪造。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=100&end=300",
        content=b"c" * 200,
        headers={"X-Chunk-Claim": "not-the-real-token"},
    )
    assert r.status_code == 403

    # 令牌正确但区间只交一部分（子段/超集/偏移）→ 409，且不滑动 TTL 之外
    # 的登记副作用：chunks 表无新增。
    for s, e, payload_len in [
        (100, 200, 100),
        (50, 300, 250),
        (100, 301, 201),
    ]:
        r = client.put(
            f"/api/v1/sessions/{sid}/chunks?start={s}&end={e}",
            content=b"d" * payload_len,
            headers={"X-Chunk-Claim": token},
        )
        assert r.status_code == 409, (s, e, r.text)
        assert r.json()["detail"]["error"] == (
            "chunk range must exactly match the claimed interval"
        )

    # 未认领区间携带任意/不带头都沿用现有行为（头被忽略）。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=500&end=600",
        content=b"e" * 100,
        headers={"X-Chunk-Claim": "unrelated-garbage-token"},
    )
    assert r.status_code == 200, r.text

    # 已收区间再被认领必须拒绝（保证认领与已收始终不相交）。
    assert _claim(client, sid, 550, 650).status_code == 409

    # 令牌 + 完全一致区间 → 200，令牌被消费。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=100&end=300",
        content=b"f" * 200,
        headers={"X-Chunk-Claim": token},
    )
    assert r.status_code == 200, r.text
    assert r.json()["received"] == {"start": 100, "end": 300}

    row = db.get_conn().execute(
        "SELECT * FROM chunk_claims WHERE id = ?", (body["claim_id"],)
    ).fetchone()
    assert row["status"] == "consumed"
    assert row["consumed_at"] is not None

    # 消费后旧令牌立即不可用：区间已收 → 409（重叠已收）。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=100&end=300",
        content=b"g" * 200,
        headers={"X-Chunk-Claim": token},
    )
    assert r.status_code == 409

    # 已收区间的相邻新区间不再被已消费认领拦截，未认领直接提交。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=300&end=400", content=b"h" * 100
    )
    assert r.status_code == 200, r.text


def test_token_rejected_before_body_stream_and_no_ttl_slide(
    client: TestClient,
) -> None:
    sid = _create(client, size=1000)
    _claim(client, sid, 0, 100)
    before = client.get(f"/api/v1/sessions/{sid}").json()["expires_at"]

    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=100", content=b"i" * 100
    )
    assert r.status_code == 409
    # 认领准入失败不滑动 TTL（预检在 TTL UPDATE 之前拦截）。
    after = client.get(f"/api/v1/sessions/{sid}").json()["expires_at"]
    assert after == before

    # 非法令牌头格式在读体前 400。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=100",
        content=b"i" * 100,
        headers={"X-Chunk-Claim": "has space"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid X-Chunk-Claim header"


def test_successful_claim_flow_completes(client: TestClient) -> None:
    size = 600
    data = bytes((i * 13 + 1) % 256 for i in range(size))
    sid = client.post(
        "/api/v1/sessions",
        json={
            "filename": "claim-flow.bin",
            "total_size": size,
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    ).json()["session_id"]

    # 两片都先认领再按令牌提交；第三片未认领直接提交。
    cuts = [(0, 200), (200, 400), (400, 600)]
    tokens: dict[tuple[int, int], str] = {}
    for s, e in cuts[:2]:
        r = _claim(client, sid, s, e)
        assert r.status_code == 201, r.text
        tokens[(s, e)] = r.json()["token"]

    # 乱序提交。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=200&end=400",
        content=data[200:400],
        headers={"X-Chunk-Claim": tokens[(200, 400)]},
    )
    assert r.status_code == 200, r.text
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=400&end=600", content=data[400:600]
    )
    assert r.status_code == 200, r.text
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=200",
        content=data[0:200],
        headers={"X-Chunk-Claim": tokens[(0, 200)]},
    )
    assert r.status_code == 200, r.text

    done = client.post(f"/api/v1/sessions/{sid}/complete")
    assert done.status_code == 200, done.text
    dl = client.get(f"/api/v1/sessions/{sid}/download")
    assert dl.status_code == 200
    assert dl.content == data


# ---------------------------------------------------------------------------
# 失败/断流不消费令牌
# ---------------------------------------------------------------------------


def test_failed_or_aborted_submission_does_not_consume_token(
    client: TestClient,
) -> None:
    sid = _create(client, size=1000)
    body = _claim(client, sid, 0, 100).json()
    claim_id = body["claim_id"]

    # 长度不符（Content-Length 与区间不一致）在预检阶段拒绝：令牌不消费。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=100",
        content=b"j" * 99,
        headers={"X-Chunk-Claim": body["token"]},
    )
    assert r.status_code == 400
    row = db.get_conn().execute(
        "SELECT status FROM chunk_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    assert row["status"] == "active"

    # SHA-256 不符：摘要校验在落盘前失败，根本不进消费事务。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=100",
        content=b"j" * 100,
        headers={
            "X-Chunk-Claim": body["token"],
            "X-Chunk-SHA256": "a" * 64,
        },
    )
    assert r.status_code == 422
    row = db.get_conn().execute(
        "SELECT status FROM chunk_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    assert row["status"] == "active"

    # 正确请求仍可用同一令牌成功提交并消费。
    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=100",
        content=b"j" * 100,
        headers={"X-Chunk-Claim": body["token"]},
    )
    assert r.status_code == 200, r.text
    row = db.get_conn().execute(
        "SELECT status FROM chunk_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    assert row["status"] == "consumed"


def test_claim_invalidated_when_session_expires(client: TestClient) -> None:
    sid = _create(client, size=1000)
    body = _claim(client, sid, 0, 100).json()

    # 把会话置为已过期（模拟 TTL 到期）；懒过期在下次访问触发。
    conn = db.get_conn()
    conn.execute(
        "UPDATE upload_sessions SET status = 'uploading', expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00Z", sid),
    )

    r = client.put(
        f"/api/v1/sessions/{sid}/chunks?start=0&end=100",
        content=b"k" * 100,
        headers={"X-Chunk-Claim": body["token"]},
    )
    assert r.status_code == 410
    # 会话过期后不可再创建认领。
    assert _claim(client, sid, 200, 300).status_code == 410

    # 旧令牌对应的认领已不再是有效认领（状态仍在审计行中，但 PUT 拿不到）。
    row = conn.execute(
        "SELECT status FROM chunk_claims WHERE id = ?", (body["claim_id"],)
    ).fetchone()
    assert row["status"] == "active"  # 未消费，仅随会话失效


def test_concurrent_claimed_puts_only_one_consumes(client: TestClient) -> None:
    sid = _create(client, size=1000)
    body = _claim(client, sid, 0, 200).json()
    token = body["token"]
    statuses: list[int] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        r = client.put(
            f"/api/v1/sessions/{sid}/chunks?start=0&end=200",
            content=b"m" * 200,
            headers={"X-Chunk-Claim": token},
        )
        statuses.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 只有一个请求成功登记并消费；其余因与已收区间重叠得到 409。
    assert statuses.count(200) == 1
    assert statuses.count(409) == 3
    row = db.get_conn().execute(
        "SELECT status, COUNT(*) AS n FROM chunk_claims WHERE id = ?",
        (body["claim_id"],),
    ).fetchone()
    assert row["status"] == "consumed"


# ---------------------------------------------------------------------------
# 老库迁移
# ---------------------------------------------------------------------------


def test_init_db_migrates_v3_database(monkeypatch, tmp_path: Path) -> None:
    """v3 老库平滑升级到 v4：chunk_claims 表可用，老数据保留。"""
    import sqlite3

    db_file = tmp_path / "v3.db"
    raw = sqlite3.connect(str(db_file))
    raw.executescript(db.MIGRATION_1)
    raw.executescript(db.MIGRATION_2)
    raw.executescript(db.MIGRATION_3)
    raw.execute("PRAGMA user_version = 3")
    raw.execute(
        "INSERT INTO upload_sessions VALUES ("
        "'sidX','f.bin',10,'" + "a" * 64 + "',4,'uploading',0,"
        "'2026-01-01T00:00:00Z','2030-01-02T00:00:00Z','2026-01-01T00:00:00Z',"
        "NULL)"
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_file}")
    db._local.conn = None
    try:
        db.init_db()
        conn = db.get_conn()
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == db.LATEST_USER_VERSION
            == 4
        )
        # 老数据保留。
        assert conn.execute(
            "SELECT filename FROM upload_sessions WHERE id = 'sidX'"
        ).fetchone()["filename"] == "f.bin"
        # 新表可用且外键级联生效。
        conn.execute(
            "INSERT INTO chunk_claims ("
            "id, session_id, start_offset, end_offset, token_hash, "
            "status, created_at, expires_at) VALUES ("
            "'claim1','sidX',0,10,'" + "b" * 64 + "','active',"
            "'2026-01-01T00:00:00Z','2030-01-01T00:00:00Z')"
        )
        conn.execute("DELETE FROM upload_sessions WHERE id = 'sidX'")
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM chunk_claims WHERE id = 'claim1'"
        ).fetchone()["n"] == 0
        # 再次初始化幂等。
        db.init_db()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        db._local.conn = None

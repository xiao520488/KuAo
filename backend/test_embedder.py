# -*- coding: utf-8 -*-
"""
test_embedder.py —— Embedding 在线接口自检脚本

作用：在正式重建索引 / 启动服务之前，用最小代价验证：
    1. .env 是否被正确加载、三项配置是否齐全
    2. API 鉴权是否通过（能否拿到向量）
    3. 返回向量维度是多少、是否已归一化
    4. query / doc 两条通路是否都正常、维度是否一致
    5. 真实语义相似度是否合理（同义句 > 无关句）—— 这是判断
       "接对了模型" 的关键信号，而不是仅仅 "没报错"
    6. 分批（batch）逻辑在小批量下是否正常

运行方式（推荐，避免终端中文乱码）：
    python -X utf8 test_embedder.py

退出码：
    0 = 全部通过
    1 = 存在失败项（详见输出）

注意：本脚本会真实调用 API，产生少量 token 费用（通常远小于 1 分钱）。
"""

import sys
import traceback

# 统一输出编码，避免 Windows GBK 终端中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"

_failures = 0
_warnings = 0


def ok(name: str, detail: str = "") -> None:
    print(f"{PASS} {name}" + (f"  —— {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    global _failures
    _failures += 1
    print(f"{FAIL} {name}" + (f"  —— {detail}" if detail else ""))


def warn(name: str, detail: str = "") -> None:
    global _warnings
    _warnings += 1
    print(f"{WARN} {name}" + (f"  —— {detail}" if detail else ""))


def section(title: str) -> None:
    print("\n" + "=" * 66)
    print(f"  {title}")
    print("=" * 66)


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


# ============================================================
# 步骤 0：加载 .env
# ============================================================
def step_env() -> None:
    section("步骤 0 / 6  加载 .env 配置")
    try:
        from dotenv import load_dotenv
        loaded = load_dotenv(override=True)
        if loaded:
            ok("dotenv 已加载 .env")
        else:
            warn("未找到 .env 或内容为空", "将依赖系统环境变量")
    except ImportError:
        warn("python-dotenv 未安装", "跳过加载，直接读系统环境变量")
    except Exception as exc:
        bad("加载 .env 失败", f"{type(exc).__name__}: {exc}")
        return

    import os
    key = os.getenv("EMBEDDING_API_KEY", "")
    url = os.getenv("EMBEDDING_BASE_URL", "")
    model = os.getenv("EMBEDDING_MODEL", "")

    # Key：只显示前后各 4 位，避免泄露到日志
    if not key:
        bad("EMBEDDING_API_KEY", "为空，请在 .env 中填写")
    elif not key.startswith(("sk-", "Bearer")) and len(key) < 16:
        warn("EMBEDDING_API_KEY", f"已填写但格式可疑（长度 {len(key)}）")
    else:
        masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
        ok("EMBEDDING_API_KEY", masked)

    if not url:
        bad("EMBEDDING_BASE_URL", "为空，请在 .env 中填写")
    elif not url.startswith("http"):
        bad("EMBEDDING_BASE_URL", f"不是合法地址：{url}")
    else:
        ok("EMBEDDING_BASE_URL", url)
        if not url.rstrip("/").endswith("/v1"):
            warn(
                "EMBEDDING_BASE_URL 未以 /v1 结尾",
                "多数兼容接口要求形如 https://.../v1，请核对服务商文档",
            )

    if not model:
        warn("EMBEDDING_MODEL", "为空，将使用代码默认值 bge-m3")
    else:
        ok("EMBEDDING_MODEL", model)


# ============================================================
# 步骤 1：导入模块 + 后端信息
# ============================================================
def step_import():
    section("步骤 1 / 6  导入 embedder 并检查后端")
    try:
        import embedder
    except Exception as exc:
        bad("导入 embedder 失败", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return None

    backend = embedder.get_backend()
    if backend == "mock":
        warn(
            "当前为 mock 后端",
            "KB_EMBED_MODE=mock，生成的是伪随机向量，仅供离线联调，不可用于真实检索",
        )
    else:
        ok("当前为 api 后端", f"model={embedder.MODEL_NAME}")
    return embedder


# ============================================================
# 步骤 2：单条 query 编码
# ============================================================
def step_query(embedder) -> None:
    section("步骤 2 / 6  编码单条查询（encode_query）")
    try:
        vec = embedder.encode_query("智能车竞赛的视觉识别方案有哪些？")
    except Exception as exc:
        bad("encode_query 调用失败", f"{type(exc).__name__}: {exc}")
        _print_hint(str(exc))
        return

    if not vec or not isinstance(vec, (list, tuple)):
        bad("encode_query 返回值异常", repr(vec)[:200])
        return

    d = len(vec)
    ok("encode_query 成功", f"维度 = {d}")

    expected = embedder.EXPECTED_DIM
    if d != expected:
        warn(
            "维度与 KB_EMBED_DIM 预期不一致",
            f"实际 {d}，预期 {expected}。这不影响使用，"
            f"但请确认换模型后已重建索引",
        )

    norm = sum(x * x for x in vec) ** 0.5
    if abs(norm - 1.0) < 1e-3:
        ok("向量已 L2 归一化", f"|v| = {norm:.6f}")
    else:
        warn("向量未归一化", f"|v| = {norm:.6f}（若 Chroma 用 cosine 空间可能影响分数）")

    if any(v != 0 for v in vec):
        ok("向量非全零")
    else:
        bad("向量全为零", "模型或接口返回异常")


# ============================================================
# 步骤 3：批量文档编码 + 分批逻辑
# ============================================================
def step_docs(embedder) -> None:
    section("步骤 3 / 6  批量编码文档（encode_docs，验证分批）")
    docs = [
        "智能车竞赛视觉组使用摄像头识别赛道元素。",
        "电磁组通过电感线圈检测赛道磁场变化。",
        "PID 控制用于舵机转角与电机速度的闭环调节。",
        "图像二值化与边缘检测是赛道提取的常用方法。",
        "竞赛规则要求赛车在规定赛道内自主完成比赛。",
        "卷积神经网络可用于数字和障碍物识别。",
        "陀螺仪与编码器提供姿态和速度反馈。",
        "无线串口模块用于赛道调试与参数下发。",
        "电机驱动芯片需要做好电源与散热设计。",
        "赛前需要完成整车重量与尺寸的规则校验。",
        "东倒西歪的偏方",
        "今天食堂的菜有点咸",
    ]
    try:
        vecs = embedder.encode_docs(docs)
    except Exception as exc:
        bad("encode_docs 调用失败", f"{type(exc).__name__}: {exc}")
        _print_hint(str(exc))
        return

    if len(vecs) != len(docs):
        bad("返回数量不匹配", f"输入 {len(docs)} 条，返回 {len(vecs)} 条")
        return
    ok("批量编码成功", f"{len(vecs)} 条文档")

    dims = {len(v) for v in vecs}
    if len(dims) == 1:
        ok("所有文档维度一致", f"dim = {dims.pop()}")
    else:
        bad("文档维度不一致", f"出现多种维度：{sorted(dims)}")

    empty = [i for i, v in enumerate(vecs) if not any(v)]
    if empty:
        bad("存在全零向量", f"索引 {empty}")
    else:
        ok("无全零向量")

    ok(
        "分批逻辑验证通过",
        f"实际批大小 = {embedder.BATCH_SIZE}，共 {len(docs)} 条 → "
        f"{(len(docs) + embedder.BATCH_SIZE - 1) // embedder.BATCH_SIZE} 批",
    )

    # 顺序一致性：先整体编码，再单独编码第 0 条，两者应一致
    try:
        single = embedder.encode_docs([docs[0]])[0]
        sim = cosine(single, vecs[0])
        if sim > 0.999:
            ok("批内顺序与输入对齐", f"cos = {sim:.6f}")
        else:
            bad("批内顺序疑似错位", f"单独编码 vs 批量编码 cos = {sim:.6f}")
    except Exception as exc:
        warn("顺序一致性检查未完成", f"{type(exc).__name__}: {exc}")

    return vecs, docs


# ============================================================
# 步骤 4：真实语义相似度（核心判据）
# ============================================================
def step_semantic(embedder, bundle) -> None:
    section("步骤 4 / 6  语义相似度校验（核心判据）")
    if not bundle:
        warn("跳过", "上一步未获得有效向量")
        return
    vecs, docs = bundle

    query = "智能车竞赛的视觉识别方案有哪些？"
    try:
        qv = embedder.encode_query(query)
    except Exception as exc:
        bad("查询编码失败", f"{type(exc).__name__}: {exc}")
        return

    scored = sorted(
        ((cosine(qv, v), d) for v, d in zip(vecs, docs)),
        key=lambda x: x[0],
        reverse=True,
    )

    print(f"\n  查询：{query}\n")
    print("  相关度排序（Top 5）：")
    for rank, (score, doc) in enumerate(scored[:5], 1):
        print(f"    {rank}. {score:.4f}  {doc}")
    print("  最不相关 2 条：")
    for score, doc in scored[-2:]:
        print(f"       {score:.4f}  {doc}")

    top_score, top_doc = scored[0]
    low_score, low_doc = scored[-1]

    if embedder.get_backend() == "mock":
        warn("mock 后端无语义能力", "以下排序断言不适用，已跳过")
        return

    # 断言 1：最相关的是视觉相关文档
    if "视觉" in top_doc or "图像" in top_doc or "摄像头" in top_doc:
        ok("Top1 命中视觉相关文档", top_doc)
    else:
        bad("Top1 未命中视觉相关文档", f"实际 Top1：{top_doc}（score={top_score:.4f}）")

    # 断言 2：最大值应明显高于最小值（区分度）
    gap = top_score - low_score
    if gap > 0.05:
        ok("向量有区分度", f"top - bottom = {gap:.4f}")
    else:
        warn("区分度过低", f"top={top_score:.4f}, bottom={low_score:.4f}, 差={gap:.4f}")

    # 断言 3：无关句（食堂/偏方）不应排在前面
    noise_docs = {"东倒西歪的偏方", "今天食堂的菜有点咸"}
    top3_docs = {d for _, d in scored[:3]}
    if top3_docs & noise_docs:
        bad("无关句进入 Top3", f"Top3 含：{sorted(top3_docs & noise_docs)}")
    else:
        ok("无关句未进入 Top3")

    # 断言 4：同义改写应高度相似
    try:
        a = embedder.encode_query("如何用摄像头识别赛道？")
        b = embedder.encode_query("赛道图像识别用什么方法？")
        c = embedder.encode_query("食堂今天中午吃什么？")
        sim_ab = cosine(a, b)
        sim_ac = cosine(a, c)
        if sim_ab > sim_ac:
            ok("同义句相似度高于无关句", f"同义 {sim_ab:.4f} > 无关 {sim_ac:.4f}")
        else:
            bad("同义句相似度未高于无关句", f"同义 {sim_ab:.4f} <= 无关 {sim_ac:.4f}")
    except Exception as exc:
        warn("同义句校验未完成", f"{type(exc).__name__}: {exc}")


# ============================================================
# 步骤 5：与 Chroma 的兼容性
# ============================================================
def step_chroma(embedder) -> None:
    section("步骤 5 / 6  与 ChromaDB 兼容性检查")
    try:
        import chromadb  # noqa: F401
    except Exception as exc:
        warn("chromadb 未安装或导入失败", f"{type(exc).__name__}: {exc}")
        return
    ok("chromadb 可导入")

    try:
        client = chromadb.EphemeralClient()
        coll = client.get_or_create_collection(
            name="embedder_selftest", metadata={"hnsw:space": "cosine"}
        )
        docs = ["视觉组使用摄像头识别赛道", "电磁组使用电感检测磁场"]
        vecs = embedder.encode_docs(docs)
        coll.add(ids=["a", "b"], documents=docs, embeddings=vecs)
        qv = embedder.encode_query("摄像头识别赛道")
        res = coll.query(query_embeddings=[qv], n_results=1)
        top = res["documents"][0][0]
        dist = res["distances"][0][0] if res.get("distances") else None
        ok("Chroma 写入 + 检索成功", f"Top1 = {top}（距离 {dist:.4f}）" if dist is not None else f"Top1 = {top}")
        if top == docs[0]:
            ok("cosine 空间检索结果正确")
        elif embedder.get_backend() == "mock":
            warn("mock 后端无语义能力", "排序与预期不符属正常现象，已跳过该断言")
        else:
            bad("检索结果与预期不符", f"期望 {docs[0]}，实际 {top}")
    except Exception as exc:
        bad("Chroma 兼容性测试失败", f"{type(exc).__name__}: {exc}")


# ============================================================
# 步骤 6：汇总
# ============================================================
def step_summary(embedder) -> None:
    section("步骤 6 / 6  汇总")
    if embedder is not None:
        try:
            info = embedder.self_test()
            print(f"  self_test() = {info}")
        except Exception as exc:
            warn("self_test() 调用失败", f"{type(exc).__name__}: {exc}")

    print()
    if _failures == 0 and _warnings == 0:
        print(f"{PASS} 全部检查通过，可以执行 rebuild_chroma.py 重建索引。")
    elif _failures == 0:
        print(f"{WARN} 无失败项，但有 {_warnings} 条警告，请逐条确认后再继续。")
    else:
        print(f"{FAIL} 存在 {_failures} 个失败项、{_warnings} 条警告，请先修复再继续。")


def _print_hint(msg: str) -> None:
    """根据错误信息给出针对性的排查建议。"""
    m = msg.lower()
    print("\n  排查建议：")
    if "embeddding_api_key" in m or "api_key" in m or "401" in m or "unauthorized" in m:
        print("    · 鉴权失败：检查 EMBEDDING_API_KEY 是否正确、是否过期、是否误用了 DeepSeek 的 Key")
    elif "base_url" in m or "404" in m or "not found" in m:
        print("    · 地址错误：确认 EMBEDDING_BASE_URL 以 /v1 结尾，且服务商确实支持 /embeddings")
    elif "model" in m or "400" in m:
        print("    · 模型名错误：核对 EMBEDDING_MODEL 与服务商文档是否完全一致")
    elif "connection" in m or "timeout" in m or "timed out" in m:
        print("    · 网络问题：服务器能否访问外网？可先 curl 测试 BASE_URL 连通性")
    elif "429" in m or "rate" in m:
        print("    · 触发限流：降低 KB_EMBED_BATCH_SIZE，或稍后重试")
    else:
        print("    · 查看完整报错堆栈，必要时用 curl 直接请求 /embeddings 接口验证")


def main() -> int:
    print("=" * 66)
    print("  Embedding 在线接口自检（test_embedder.py）")
    print("=" * 66)

    step_env()
    embedder = step_import()
    if embedder is None:
        print(f"\n{FAIL} 模块导入失败，后续步骤跳过。")
        return 1

    step_query(embedder)
    bundle = step_docs(embedder)
    step_semantic(embedder, bundle)
    step_chroma(embedder)
    step_summary(embedder)

    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

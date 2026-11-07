"""
LLM 封装 (llm.py) —— DeepSeek（OpenAI 兼容接口）

对外只暴露三个函数：
    chat()        -> 纯文本补全
    chat_json()   -> 强制 JSON 输出，带清洗/修复/重试，返回 dict
    ping()        -> 连通性自检
    LLMError      -> 统一异常

设计要点：
    - 单例 client，复用连接
    - 指数退避重试（网络抖动 / 5xx / 429）
    - 自动累计 token 用量，供 crawl_jobs.cost_token 记录
    - 兼容推理模型（deepseek-flash 等）：reasoning 会占用 completion 额度，
      故当 content 为空但 finish_reason==length 时，自动放大 max_tokens 重试
"""

import json
import logging
import re
import time
from typing import Any, Optional

from openai import OpenAI, APIError, APITimeoutError, RateLimitError

# 当前锁定的 openai 版本内部使用 httpx2；Timeout 类型必须与 SDK 使用同一套，
# 否则 httpx.Timeout 会被 httpx2 当成普通对象，运行时才报错。
try:  # pragma: no cover - 取决于安装的 openai 版本
    from openai._client import httpx2 as httpx
except ImportError:  # pragma: no cover - 兼容使用标准 httpx 的 openai 版本
    import httpx  # type: ignore[no-redef]

import config as cfg
from json_repair import repair_json

# --- json_repair 版本兼容层 ---------------------------------------------
# 老版本（<=0.3x）提供 repair_json_detailed() / looks_truncated()，
# 新版本（>=0.4x）只保留 repair_json()。此处补齐旧 API，
# 使 llm.py 其余逻辑无需改动，且未来换回老版本依然可用。
try:  # pragma: no cover - 取决于安装的版本
    from json_repair import repair_json_detailed, looks_truncated  # type: ignore
except ImportError:  # pragma: no cover
    class _RepairResult:
        """仿 old-API 的修复结果对象。"""

        __slots__ = ("ok", "value", "strategy", "note")

        def __init__(self, ok, value=None, strategy="unknown", note=""):
            self.ok = ok
            self.value = value
            self.strategy = strategy
            self.note = note

    _FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)

    def _strip_fence(text):
        """剥离 ```json ... ``` 围栏，返回内部文本（无围栏则原样返回）。"""
        if not isinstance(text, str):
            return text
        m = _FENCE_RE.search(text)
        return m.group(1).strip() if m else text.strip()

    def repair_json_detailed(text):  # type: ignore[misc]
        """兼容新版本 json_repair：返回 _RepairResult。"""
        body = _strip_fence(text)
        try:
            value = repair_json(body, return_objects=True, strict=True)
        except Exception as exc:
            return _RepairResult(False, strategy="failed", note=str(exc))
        if value is None:
            return _RepairResult(False, strategy="failed", note="empty result")
        return _RepairResult(True, value=value, strategy="repair_json", note="ok")

    def looks_truncated(text):  # type: ignore[misc]
        """粗略判断文本是否像被 max_tokens 截断的 JSON。"""
        if not isinstance(text, str) or not text.strip():
            return False
        return text.count("{") > text.count("}") or text.count("[") > text.count("]")


logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 配置（统一从 config.py 读取，本文件不再直接读环境变量）
# ------------------------------------------------------------
API_KEY = cfg.LLM_API_KEY
BASE_URL = cfg.LLM_BASE_URL
MODEL = cfg.LLM_MODEL

MAX_RETRIES = cfg.LLM_MAX_RETRIES
RETRY_BASE_DELAY = cfg.LLM_RETRY_BASE_DELAY
# 空正文自动扩容上限
MAX_TOKENS_CEILING = cfg.LLM_MAX_TOKENS_CEILING
# 是否启用 response_format={"type": "json_object"}
USE_JSON_MODE = cfg.LLM_JSON_MODE
# JSON 截断修复开关
JSON_REPAIR_ENABLED = cfg.JSON_REPAIR_ENABLED

_client: Optional[OpenAI] = None

# 累计用量（进程内）
_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


class LLMError(RuntimeError):
    """LLM 调用失败（配置缺失 / 重试耗尽 / 返回不可用）。"""


class _EmptyContent(LLMError):
    """正文为空但 finish_reason=length 且疑似被推理内容占满 —— 可扩容重试。"""


# ------------------------------------------------------------
# 内部工具
# ------------------------------------------------------------

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not API_KEY:
            raise LLMError("缺少 DEEPSEEK_API_KEY，请检查 .env")
        # max_retries=0：关闭 SDK 自带重试，避免与 _call 的指数退避叠加。
        # 连接 5s、读取 45s；httpx 要求未给默认值时四项都显式设置，故补 write/pool。
        _client = OpenAI(
            api_key=API_KEY,
            base_url=BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0),
            max_retries=0,
        )
    return _client


def _should_retry(exc: Exception) -> bool:
    if isinstance(exc, (APITimeoutError, RateLimitError)):
        return True
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        return status is None or status >= 500 or status == 429
    return False


def _accumulate(usage: Any) -> None:
    if usage is None:
        return
    _usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
    _usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
    _usage["total_tokens"] += getattr(usage, "total_tokens", 0) or 0
    _usage["calls"] += 1


def get_usage() -> dict:
    """返回本次进程累计用量（含 calls 次数），并支持 reset_usage()。"""
    return dict(_usage)


def reset_usage() -> None:
    for k in _usage:
        _usage[k] = 0


def _is_reasoning_model(model: str) -> bool:
    """粗判是否为推理模型（其 completion 额度先产出 reasoning）。"""
    m = (model or "").lower()
    return any(tag in m for tag in ("flash", "reasoner", "-r1", "reasoning"))


def _call(messages: list, *, temperature: float, max_tokens: int,
          response_format: Optional[dict], model: str) -> str:
    """
    底层调用 + 重试。返回文本内容。

    对推理模型特化处理：
        若 content 为空且 finish_reason == 'length'（reasoning 吃光额度），
        将 max_tokens 翻倍后重试，直到 MAX_TOKENS_CEILING。
    """
    client = _get_client()
    last_exc: Optional[Exception] = None
    tokens = max_tokens

    for attempt in range(MAX_RETRIES):
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": tokens,
            }
            if response_format:
                kwargs["response_format"] = response_format
            resp = client.chat.completions.create(**kwargs)
            _accumulate(getattr(resp, "usage", None))

            choice = resp.choices[0]
            content = choice.message.content
            if content and content.strip():
                return content

            # 空正文：判断是否为额度耗尽
            if choice.finish_reason == "length" and tokens < MAX_TOKENS_CEILING:
                tokens = min(tokens * 2, MAX_TOKENS_CEILING)
                last_exc = _EmptyContent(
                    f"正文为空(finish_reason=length)，max_tokens 扩容至 {tokens} 重试"
                )
                continue
            raise LLMError("LLM 返回空内容")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == MAX_RETRIES - 1 or not _should_retry(exc):
                break
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))

    raise LLMError(f"LLM 调用失败（{MAX_RETRIES} 次重试后）: {last_exc}") from last_exc


# ------------------------------------------------------------
# 一、纯文本补全
# ------------------------------------------------------------

def chat(prompt: str, *, system: Optional[str] = None, temperature: float = 0.7,
         max_tokens: int = 2048, model: Optional[str] = None) -> str:
    """
    单轮文本补全。

    参数:
        prompt:       用户输入
        system:       系统提示（可选）
        temperature:  0~2，事实抽取建议 0.1~0.3，创意生成可 0.8~1.0
        max_tokens:   推理模型下会被 reasoning 占用，故默认给足 2048
    返回:
        str 模型回复
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return _call(messages, temperature=temperature, max_tokens=max_tokens,
                 response_format=None, model=model or MODEL)


# ------------------------------------------------------------
# 二、JSON 输出
# ------------------------------------------------------------

_JSON_DECODER = json.JSONDecoder()


# ------------------------------------------------------------
# safe_json：全项目统一的「模型输出 -> Python 对象」安全出口
# ------------------------------------------------------------

def safe_json(text: Any, fallback: Any = None, *, strict: bool = False,
              log: bool = True) -> Any:
    """
    把模型返回的任意文本安全解析为 Python 对象，**永不抛异常**。

    与 json_repair.repair_json 的区别：
        - 额外接入配置开关 JSON_REPAIR_ENABLED
        - 额外输出结构化日志（便于线上定位截断问题）
        - strict=True 时修复失败抛 LLMError（给需要强一致的调用方保留出口）

    参数:
        text:     模型输出字符串，或已是 dict/list（直接透传）
        fallback: 解析失败时的返回值
        strict:   True 则失败抛 LLMError 而不是返回 fallback
        log:      是否记录诊断日志
    返回:
        解析出的 dict / list，或 fallback
    """
    # 已是 Python 对象：直接透传，避免二次序列化
    if isinstance(text, (dict, list)):
        return text

    if not JSON_REPAIR_ENABLED:
        # 修复被关闭：退化为最朴素的解析，失败即 fallback
        if isinstance(text, str):
            try:
                return json.loads(text.strip())
            except Exception:
                pass
        if strict:
            raise LLMError("JSON 解析失败（修复功能已关闭）")
        return fallback

    try:
        result = repair_json_detailed(text)
    except Exception as exc:  # 极端情况兜底
        if log:
            logger.warning("safe_json 修复过程异常: %r", exc)
        if strict:
            raise LLMError(f"JSON 解析失败: {exc}") from exc
        return fallback

    if result.ok:
        # 修复成功但用了非常规策略 -> 说明模型输出被截断过，值得记一笔
        if log and result.strategy not in ("direct", "passthrough", "fence", "raw_decode"):
            logger.warning("JSON 已修复(strategy=%s): %s", result.strategy, result.note)
        return result.value

    if log:
        preview = (text[:200] + "...") if isinstance(text, str) and len(text) > 200 else text
        logger.warning("safe_json 解析失败(%s): %r", result.note, preview)
    if strict:
        raise LLMError(f"JSON 解析失败: {result.note}")
    return fallback


def safe_json_lenient(text: Any) -> Any:
    """safe_json 的别名：永远返回一个可迭代容器（保证调用方可以 len()/for）。"""
    val = safe_json(text, fallback=None)
    if isinstance(val, (dict, list)):
        return val
    return {}


def _extract_json(text: str) -> Any:
    """
    从模型输出中尽力提取 JSON：
        1) 直接 json.loads
        2) 去 ```json 代码围栏
        3) 从第一个 { 或 [ 起 raw_decode（容忍前后废话）
    失败抛 ValueError。
    """
    t = text.strip()

    try:
        return json.loads(t)
    except ValueError:
        pass

    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
        try:
            return json.loads(candidate)
        except ValueError:
            t = candidate

    # 原文的有效起始字符决定「期望的顶层类型」
    stripped = t.lstrip()
    expect_list = stripped.startswith("[")

    for i, ch in enumerate(t):
        if ch in "{[":
            # 期望数组时，跳过内部对象的 '{'，只在顶层 '[' 处尝试
            if expect_list and ch != "[":
                continue
            try:
                obj, _ = _JSON_DECODER.raw_decode(t[i:])
                # 快速路径陷阱：期望数组却只解出一个对象，说明数组被硬截断。
                # 此时不能直接返回那个 dict，必须交给修复器还原 list 结构。
                if expect_list and not isinstance(obj, list):
                    break
                return obj
            except ValueError:
                continue

    # 前述三条快速路径都失败 -> 交给修复器（处理 max_tokens 截断）
    if JSON_REPAIR_ENABLED:
        result = repair_json_detailed(t)
        if result.ok:
            logger.warning("_extract_json 经修复成功: strategy=%s, %s",
                           result.strategy, result.note)
            return result.value
        raise ValueError(f"响应中未找到合法 JSON（修复失败: {result.note}）")

    raise ValueError("响应中未找到合法 JSON")


def chat_json(prompt: str, *, system: Optional[str] = None, schema_hint: Optional[str] = None,
              temperature: float = 0.2, max_tokens: int = 2048,
              retries: int = 2, model: Optional[str] = None) -> Any:
    """
    强制模型输出 JSON，并解析为 Python 对象。

    参数:
        schema_hint: 期望结构的文字说明（追加到 prompt 末尾，提高稳定性）
        retries:     解析失败后追加纠错提示重问的次数
    返回:
        dict 或 list
    抛出:
        LLMError 若最终仍无法解析
    """
    sys_msg = system or (
        "你是一个严谨的信息抽取引擎。只输出 JSON，不要输出任何解释性文字、"
        "不要使用 Markdown 代码围栏。"
    )
    user_msg = prompt
    if schema_hint:
        user_msg = f"{prompt}\n\n【输出格式要求】\n{schema_hint}"

    messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_msg},
    ]

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        text = _call(messages, temperature=temperature, max_tokens=max_tokens,
                     response_format={"type": "json_object"}, model=model or MODEL)
        try:
            return _extract_json(text)
        except ValueError as exc:
            last_err = exc
            if attempt >= retries:
                break
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": "上面的输出不是合法 JSON。请只输出合法 JSON，不要任何其它内容。",
            })

    raise LLMError(f"JSON 解析失败: {last_err}")


# ------------------------------------------------------------
# 三、健康检查
# ------------------------------------------------------------

def ping(model: Optional[str] = None) -> dict:
    """
    连通性自检：发一个极小请求，只要求模型回一个字。

    注意：推理模型的 reasoning 会占用 completion 额度，
          故此处 max_tokens 给足 512，而非 16。

    返回 {"ok": bool, "model": str, "latency_ms": int, "error": str|None}
    """
    t0 = time.time()
    m = model or MODEL
    try:
        _get_client()
        chat("请只回复两个字：正常", temperature=0.0, max_tokens=512, model=m)
        return {"ok": True, "model": m,
                "latency_ms": int((time.time() - t0) * 1000), "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "model": m,
                "latency_ms": int((time.time() - t0) * 1000), "error": str(exc)}


# ------------------------------------------------------------
# 四、流式补全（供前端 st.write_stream 使用）
# ------------------------------------------------------------

def chat_stream(prompt: str, *, system: Optional[str] = None,
                temperature: float = 0.5, max_tokens: int = 2048,
                model: Optional[str] = None):
    """
    流式文本补全：逐段 yield 文本增量。

    用途：Streamlit 里 `st.write_stream(llm.chat_stream(...))` 实时打字机效果。

    参数与 chat() 一致。
    注意：流式不走重试（首包失败直接抛异常，由调用方兜底）。
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    client = _get_client()
    stream = client.chat.completions.create(
        model=model or MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    for chunk in stream:
        try:
            delta = chunk.choices[0].delta
        except (IndexError, AttributeError):
            continue
        piece = getattr(delta, "content", None)
        if piece:
            yield piece


def llm_available() -> bool:
    """
    粗粒度探测：本地配置是否齐备（不发起网络请求，不消耗额度）。

    用于 UI 决定走「真流式」还是「本地检索摘要兜底」。
    真正的网络异常仍由调用方 try/except 兜住。
    """
    return bool(API_KEY)


def llm_config_hint() -> str:
    """给 UI 显示的配置提示，不含密钥明文。"""
    if not API_KEY:
        return "未检测到 DEEPSEEK_API_KEY，将使用本地检索摘要兜底。"
    return f"已配置 {MODEL} @ {BASE_URL}"

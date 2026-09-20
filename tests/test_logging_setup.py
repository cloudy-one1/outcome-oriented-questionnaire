"""``src/logging_setup.py`` 的离线契约测试（此前 0%，README「已知缺口」第一条）。

为什么值得补：这个模块是全项目「留痕」的唯一入口 —— src/gui 里那批
``except Exception: pass`` 降级路径全靠它把日志写进 ``wjx`` 名字空间。
它自己没测试，意味着两类回归 CI 都拦不住：

  1. ``_CONFIGURED`` 守卫失效 → 每次入口调用都多挂一个 console handler，
     同一条 WARNING 在终端刷屏；
  2. ``propagate = False`` 被删 → 日志重新流向 root，CLI/GUI 混跑时
     凭空多出一份重复输出，更糟的是被别处的 root handler 吞掉。

顺带钉住几处「只有跑一遍才知道」的真实行为：FileHandler 继承 StreamHandler
所以「一个 console handler」必须按精确类型数；handler 自身级别是 NOTSET、
过滤全靠 logger 的 INFO；``get_logger`` 用的是 ``startswith`` 前缀匹配而不是
分段匹配。另两处是 v2.7 修掉的：已配置状态下重复传同一 ``log_file`` 曾经会
再叠一个 FileHandler 让每条记录写两遍（现在按 ``baseFilename`` 去重），
第二次的 ``level`` 曾经完全不生效（DEBUG 落盘口配 INFO 的 logger = 空文件）。
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src import logging_setup  # noqa: E402
from src.logging_setup import get_logger, setup_logging  # noqa: E402

_NS = "wjx"


class Recorder(logging.Handler):
    """挂在 root 上，用来证明日志确实没往 root 冒。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture(autouse=True)
def isolated_logging_state():
    """``_CONFIGURED`` 是模块级全局、``wjx`` 是进程级 logger，两者都得还原。

    其它测试文件在 import src.* 时就已经抓好了 logger；这里漏掉 handlers 会
    把它们的输出写进本用例的临时文件，而漏关 FileHandler 会让 Windows 上的
    tmp_path 因句柄未释放而删不掉。
    """
    logger = logging.getLogger(_NS)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_flag = logging_setup._CONFIGURED

    logger.handlers.clear()
    logging_setup._CONFIGURED = False
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            # 只关 FileHandler：StreamHandler(sys.stderr).close() 会关掉真终端
            if isinstance(handler, logging.FileHandler):
                handler.close()
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
        logging_setup._CONFIGURED = saved_flag


def _console_handlers(logger: logging.Logger) -> list[logging.Handler]:
    """FileHandler 是 StreamHandler 的子类，用 isinstance 会把落盘句柄算进来。"""
    return [h for h in logger.handlers if type(h) is logging.StreamHandler]


def _file_handlers(logger: logging.Logger) -> list[logging.FileHandler]:
    return [h for h in logger.handlers if isinstance(h, logging.FileHandler)]


def _wjx() -> logging.Logger:
    return logging.getLogger(_NS)


def _read(path: str) -> str:
    assert os.path.exists(path), f"{path} 未生成"
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
def test_first_call_attaches_one_console_handler() -> None:
    stderr = sys.stderr  # pytest 会替换 sys.stderr，取调用当下这个引用
    setup_logging()

    logger = _wjx()
    assert len(_console_handlers(logger)) == 1
    assert _file_handlers(logger) == []
    assert logger.level == logging.INFO
    # 不关掉 propagate，所有记录都会再被 root 上的 handler 输出一遍
    assert logger.propagate is False
    assert logging_setup._CONFIGURED is True

    handler = _console_handlers(logger)[0]
    assert handler.stream is stderr
    # 级别只设在 logger 上，handler 保持 NOTSET —— 别把过滤做两遍
    assert handler.level == logging.NOTSET
    record = logging.LogRecord(_NS, logging.WARNING, __file__, 1, "msg", None, None)
    assert handler.format(record).endswith("WARNING wjx: msg")


def test_repeat_calls_never_stack_console_handler() -> None:
    setup_logging()
    logger = _wjx()
    handlers_after_first = list(logger.handlers)
    only_console = handlers_after_first[0]

    setup_logging()
    setup_logging()

    assert logger.handlers == handlers_after_first, "_CONFIGURED 守卫失效：console handler 被重复挂载"
    assert _console_handlers(logger) == [only_console]


def test_level_argument_applies_on_first_call() -> None:
    setup_logging(level=logging.WARNING)
    logger = _wjx()
    assert logger.level == logging.WARNING
    assert logger.isEnabledFor(logging.INFO) is False


def test_no_file_handler_when_log_file_is_falsy() -> None:
    """``--log-file`` 缺省是 None；空串也必须等价于「不落盘」。"""
    setup_logging(None)
    setup_logging("")
    assert _file_handlers(_wjx()) == []


def test_file_logging_creates_missing_parent_dirs(tmp_path) -> None:
    log_file = str(tmp_path / "nested" / "deeper" / "run.log")
    setup_logging(log_file)
    assert os.path.isdir(os.path.dirname(log_file)), "目录没建出来，CLI 传 logs/xx.log 会直接崩"

    get_logger("cli").warning("超时降级")
    assert "WARNING wjx.cli: 超时降级" in _read(log_file)


def test_chinese_text_round_trips_as_utf8(tmp_path) -> None:
    """encoding="utf-8" 是这个模块里唯一一处和平台默认编码较真的地方：
    漏传的话文件会按平台默认编码（Windows 上是 gbk）落盘，UTF-8 读回来是
    乱码，遇到 gbk 编不出的字符（emoji 等）直接 UnicodeEncodeError。"""
    log_file = str(tmp_path / "cn.log")
    setup_logging(log_file)
    message = "第 3 题滑块校验失败（人工介入 120s）→ 跳过"

    get_logger("wjx.cli").warning(message)

    content = _read(log_file)
    assert message in content
    assert content.count("\n") == 1, "每条记录应当只落一行"
    # 同一行里时间戳 + 级别 + 名字空间前缀都在，事后才复盘得出来
    assert content.splitlines()[0].endswith(f"WARNING wjx.cli: {message}")
    with open(log_file, "rb") as fh:
        assert message.encode("utf-8") in fh.read()


def test_debug_records_are_dropped_by_logger_level(tmp_path) -> None:
    log_file = str(tmp_path / "lvl.log")
    setup_logging(log_file)
    get_logger("cli").debug("不该落盘")
    get_logger("cli").info("该落盘")
    content = _read(log_file)
    assert "不该落盘" not in content
    assert "该落盘" in content


def test_record_reaches_wjx_file_but_not_root(tmp_path) -> None:
    """契约核心：propagate=False 换来的隔离。带对照组，防止断言是空的。"""
    log_file = str(tmp_path / "iso.log")
    setup_logging(log_file)
    root = logging.getLogger()
    probe = Recorder()
    root.addHandler(probe)
    try:
        get_logger("wjx.cli").warning("only in wjx")
        assert "only in wjx" in _read(log_file)
        assert probe.messages == [], "记录漏到了 root，输出会被重复一份"

        # 对照组：把 propagate 打开就应当立刻被 root 收到，
        # 证明上面那条断言不是因为 probe 根本没接上。
        logging.getLogger(_NS).propagate = True
        try:
            get_logger("wjx.cli").warning("reaches root")
            assert probe.messages == ["reaches root"]
        finally:
            logging.getLogger(_NS).propagate = False
    finally:
        root.removeHandler(probe)
        probe.close()


def test_logger_naming_rules() -> None:
    assert get_logger("cli").name == "wjx.cli"
    assert get_logger("wjx.gui.app").name == "wjx.gui.app", "已经在名字空间里的不该再加前缀"
    assert get_logger().name == _NS, "无参默认就是 wjx 本身"
    assert get_logger("cli").parent is logging.getLogger(_NS)
    # 现状：startswith 是前缀匹配，不是分段匹配。以 wjx 开头但不同段的
    # 名字会原样返回并挂到 root 下，从而绕过本模块的所有 handler。
    assert get_logger("wjxtools").name == "wjxtools"


def test_second_distinct_log_file_adds_another_sink(tmp_path) -> None:
    """已配置后再传新路径应当真的多一个落盘口（docstring 承诺的「按需追加」）。"""
    first = str(tmp_path / "a.log")
    second = str(tmp_path / "b.log")
    setup_logging(first)
    setup_logging(second)

    logger = _wjx()
    assert len(_console_handlers(logger)) == 1
    get_logger("cli").warning("双写")
    assert "双写" in _read(first)
    assert "双写" in _read(second)


def test_same_log_file_twice_does_not_duplicate_records(tmp_path) -> None:
    """同一个路径传两次只挂一个 FileHandler，每条记录落一遍。

    v2.7 之前 ``_add_file_handler`` 追加前既不比对 ``baseFilename`` 也不去重，
    于是两次 ``setup_logging(p)`` 会在同一文件上挂两个 handler —— 每条日志写两遍，
    事后复盘时行号与计数全部失真。
    """
    log_file = str(tmp_path / "dup.log")
    setup_logging(log_file)
    setup_logging(log_file)

    logger = _wjx()
    files = _file_handlers(logger)
    assert len(files) == 1, "同一文件被挂了多个 handler"
    assert files[0].baseFilename == os.path.abspath(log_file)

    get_logger("cli").warning("dup line")
    content = _read(log_file)
    assert content.count("dup line") == 1
    assert len(_console_handlers(logger)) == 1


def test_level_on_repeat_call_applies_to_logger(tmp_path) -> None:
    """第二次传 level 必须真的改到 logger 上。

    v2.7 之前 ``setLevel`` 只在首次调用执行，于是「先 setup_logging() 再
    setup_logging(p, level=DEBUG)」得到的是 DEBUG 的 handler 配 INFO 的 logger ——
    DEBUG 记录在 logger 那层就被丢了，落盘出来是一个空文件。
    """
    log_file = str(tmp_path / "debug.log")
    setup_logging()
    setup_logging(log_file, level=logging.DEBUG)

    logger = _wjx()
    assert logger.level == logging.DEBUG
    assert _file_handlers(logger)[0].level == logging.DEBUG
    get_logger("cli").debug("该落盘")
    assert "该落盘" in _read(log_file)

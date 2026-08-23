"""问卷自动填写工具 — 核心包。

v2.3 模块变更日志（代码可读性改进建议 第 1 批）：
    * 2026-08-23（V2.3 数据模型 + 命名 + 状态对象）：
        - 新增 src/models.py：题型枚举 ``QuestionType`` +
          ``QuestionData`` / ``AnswerData`` / ``SubmitResult`` /
          ``WeightConfigEntry`` dataclass + ``RunState`` 状态对象，
          替代在模块间裸传的 ``dict[str, Any]``（建议第三章「类型和数据模型」）
        - 所有 dataclass 提供 ``from_dict`` / ``as_dict`` 双向兼容，
          保证旧代码继续传 dict 也能跑，新代码可选择用 dataclass
        - ``QuestionType`` 继承 ``str``，与字符串字面量 ``==`` 兼容
          （如 ``QuestionType.SINGLE == "single"`` 返回 ``True``），
          减少散落在多处的 ``"single"`` / ``"multi"`` / ``"text"`` 字符串字面量
        - ``SubmitResult`` 增强 ``SubmitOutcome`` 三态，承载 reason /
          elapsed_ms / detail 上下文，便于日志记录与回溯（建议第三章 3.3）
        - ``RunState`` 集中管理 success_count / fail_count / unknown_count /
          is_interrupted / run_id / total_elapsed_start / attempts_cap /
          current_attempt，CLI ``run_batch`` 改用 ``RunState`` 替代散落局部
          计数器，状态判定下沉到 ``history_status()`` / ``history_error_message()``
          （建议第四章「状态和全局变量」）
        - cli.py 局部变量 ``interrupted`` → ``is_interrupted``、
          pipeline.py 局部变量 ``ok`` → ``is_ok``，符合布尔变量 ``is_`` /
          ``has_`` / ``should_`` / ``use_`` 前缀约定（建议第二章 2.3）
        - README 加术语表（run / submission / attempt / success_count /
          fail_count / target_count / question）统一术语（建议第二章 2.1/2.2）

    待后续批次整改（对照建议文档剩余章节）：
        - 第五章「异常处理」收窄 ``except Exception`` + 加日志 + 可重试/不可重试分层
        - 第一章「模块职责」拆分 gui/app.py（2611 行）+ interaction.py（534 行）
          + 缩小 pipeline.py 职责
        - 第六章「浏览器与 JS」较长 JS 移到独立 .js 或常量模块
        - 第八章「函数长度」共用批量运行逻辑提到 src（CLI/GUI 共用 RunState）
        - 第九章「测试与文档」测试名描述业务行为 + README 与代码自动校验

v2.2 模块变更日志（审查整改）：
    * 2026-08-23（V2.2 审查整改批次）：
        - interaction.SubmitOutcome / SUBMIT_SUCCESS/SUBMIT_FAILED/SUBMIT_UNKNOWN
          提交结果三态：超时不再被误判为成功（P1-1 修复，避免污染 success_count）
        - history.record_answer 幂等化（INSERT OR REPLACE + DELETE+INSERT 兜底）
          + V2.2 schema 迁移：dedup 老库重复行 + 创建 (run_id, submission_index,
          question_number) 唯一索引，从结构上根除 retry 重复答案（P1-2 修复）
        - CLI KeyboardInterrupt → status='interrupted'（P1-3 修复，原仅写
          'running'/'finished'，导致中断的 run 无法被 find_resumable_run 恢复）
        - config_io.validate_weight_config 重写：NaN/Inf 检测、全 0 权重、
          choices 长度不匹配、count_options/count_weights 一致性、scale 长度
          不匹配、matrix row_weights 行长度不匹配 + 总和>0（P2-1 增强）
        - CLI 新增 --no-record-text（隐私：填空答案不落盘）/ --target-success
          / --max-attempts（P2-2 语义厘清：目标份数 vs 总尝试次数）

v2.1 模块变更日志：
    * 2026-08-22 新增（V2.1）：
        - detection.detect_answered_questions   ：扫描 DOM 已填状态（断点续填 Layer A）
        - history.find_resumable_run             ：跨进程断点续传入口（Layer B）
        - history.mark_interrupted / count_done_submissions
        - history.start_run(weight_config=...)   ：权重配置快照持久化
        - history.deserialize_weight_config      ：反序列化 + 键名 str→int
        - history._apply_migrations              ：老 DB 自动 ALTER TABLE 加列
        - pipeline.run_one_submission Step 5.5  ：跳过已答的题（续填核心）
        - GUI _on_start                          ：续传对话框 + 权重自动恢复
        - GUI _restore_weight_table_from_config  ：从持久化权重重建表格显示
        - CLI run_batch(weight_config=...)       ：CLI 也接入权重持久化

v2.0 模块变更日志：
    * 2026-07-04 新增：
        - history        ：SQLite 历史记录与复盘（运行元数据 + 每题答案明细）
        - answering_v2   ：新题型支持（填空/量表/下拉/矩阵 + 内置中文数据池）
        - config_io      ：权重配置 JSON 导入/导出 + 热更新 + 结构校验
    * 题型兼容：原 answering / detection / interaction / pipeline 保持不变，
                GUI 可按需调用 answering_v2 获取新题型答案。
"""

from .config import WEIGHT_CONFIG

__all__ = ["WEIGHT_CONFIG"]
__version__ = "2.3.0"

"""问卷自动填写工具 — 核心包。

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
__version__ = "2.1.0"

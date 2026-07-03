"""智能验证（人机验证）检测与人工等待模块（增强版）。

相比 v1 的改进：
  1. 检测函数「三信号并行」：
       a) DOM 元素显示信号（原逻辑 + 更多选择器）
       b) URL/标题/body 文本内容信号（例如跳转至 /verify，标题含「验证」）
       c) iframe / Shadow DOM 穿透搜索（部分反爬组件放在 Shadow 中）
  2. wait_for_manual_verification 接入 **ManualHoldLock**：
       进入 holding 状态时，外部 pipeline 的任何超时判断看到 lock.is_holding
       就会被挂起，不再误判为「页面超时 → 返回 False → 刷新」，确保人工有足够
       时间处理滑块/图形验证码。
  3. 新增 CAPTCHA_KEYWORDS：检测 body 中是否突然出现「请完成验证」「验证您是真人」
     这类中文提示。
"""

from __future__ import annotations

import time
from typing import Any

from .config import VERIFICATION_TIMEOUT
from .utils import ManualHoldLock

# ---------------------------------------------------------------------------
#  Windows 弹窗强制切前台 — 当检测到验证码时，弹出一个 MessageBox
#  将脚本窗口强制拉到最前面，提醒用户手动处理验证
# ---------------------------------------------------------------------------
try:
    import ctypes  # Windows API 调用库

    def force_focus() -> None:
        """弹出 Windows 系统级消息框，强制将用户注意力拉到脚本。"""
        ctypes.windll.user32.MessageBoxW(
            0,  # 父窗口句柄，0 表示无父窗口
            "智能验证已触发，请在浏览器中手动完成验证！\n\n完成后脚本自动继续。",
            "问卷脚本 — 需要人工处理",
            0x30,  # MB_ICONWARNING | MB_OK
        )

except ImportError:
    # 非 Windows 系统（Linux/Mac）降级
    def force_focus() -> None:
        pass


# 额外的验证码内容中文关键词（检查 body.innerText）
CAPTCHA_KEYWORDS: tuple[str, ...] = (
    "请完成验证", "请拖动", "验证完成后", "滑动解锁", "人机验证",
    "请按住", "按住按钮", "智能验证", "行为验证", "安全验证",
    "图形验证", "请点击", "点击相同的字", "文字验证码",
    "PROOF YOU ARE HUMAN", "verify you are human",
    "Please verify", "Press and hold", "Press & hold",
)


def _dom_has_verification(driver: Any) -> bool:
    """信号 a)：标准 DOM 选择器检查（问卷星自带 / 腾讯 / 极验 / 滑动等）。"""
    return driver.execute_script("""
        var sel = ['#antispam','.antispam','#wjx-antispam','.wjx-antispam',
                   '.captcha','#captcha','.verification','#verification',
                   'iframe[src*="captcha"]','iframe[src*="tcaptcha"]',
                   'iframe[src*="verify"]','iframe[src*="captcha-cloud"]',
                   'iframe[src*="recaptcha"]','iframe[src*="hcaptcha"]',
                   '.sm-dialog','#sm-dialog','.slide-verify','.slider-verify',
                   '.nc-container','.yidun','.geetest_holder','.geetest_widget',
                   '.TencentCaptcha','#TCaptcha','.layui-layer',
                   '[class*="captcha"]','[id*="captcha"]','[class*="verify"]','[id*="verify"]'];
        for (var i=0; i<sel.length; i++) {
            try {
                var el = document.querySelector(sel[i]);
                if (el) {
                    var r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
            } catch(_) {}
        }
        // layui 弹窗遮罩 + 弹窗对话框
        var shade = document.querySelector('.layui-layer-shade');
        if (shade) {
            var rs = shade.getBoundingClientRect();
            if (rs.width > 0) {
                var ds = document.querySelectorAll('.layui-layer, .layui-layer-page, .layui-layer-dialog');
                for (var j = 0; j < ds.length; j++) {
                    var rr = ds[j].getBoundingClientRect();
                    if (rr.width > 0 && rr.height > 0) return true;
                }
            }
        }
        return false;
    """)


def _url_title_body_verification(driver: Any) -> bool:
    """信号 b)：URL / 标题 / body 文本中文关键词检查。"""
    return driver.execute_script("""
        var kw = """ + str(list(CAPTCHA_KEYWORDS)) + """.map(function(s){return s.toLowerCase()});
        try {
            var url = (location.href || '').toLowerCase();
            if (/verify|captcha|antispam|validate|turing|check-human/.test(url)) return true;
            var ti = (document.title || '').toLowerCase();
            if (/验证|verify|captcha|prove you are human|press & hold|press and hold/.test(ti)) return true;
            var bt = ((document.body && document.body.innerText) || '').toLowerCase();
            for (var i = 0; i < kw.length; i++) {
                if (bt.indexOf(kw[i]) !== -1) return true;
            }
        } catch(_) {}
        return false;
    """)


def _shadow_iframe_penetrate(driver: Any) -> bool:
    """信号 c)：iframe 切换扫描 + Shadow DOM 穿透（对腾讯/极验/问卷星新格式有效）。"""
    return driver.execute_script("""
        // 递归 Shadow DOM 查找
        function searchShadow(root) {
            try {
                if (!root) return false;
                var all = root.querySelectorAll('*');
                for (var i = 0; i < all.length; i++) {
                    var el = all[i];
                    if (el.shadowRoot) {
                        try {
                            var html = el.shadowRoot.innerHTML || '';
                            if (html && /captcha|verify|slider|slide|geetest|yidun|tcaptcha|antispam|press.*hold/i.test(html))
                                return true;
                            if (searchShadow(el.shadowRoot)) return true;
                        } catch(_) {}
                    }
                }
            } catch(_) {}
            return false;
        }
        if (searchShadow(document)) return true;

        // 扫描 top-level 的 iframe（跨域不可读）检查 src
        try {
            var ifs = document.querySelectorAll('iframe');
            for (var i=0; i<ifs.length; i++) {
                var src = (ifs[i].src || '').toLowerCase();
                if (/captcha|verify|geetest|turing|antispam|tcaptcha/.test(src)) return true;
                var allow = (ifs[i].getAttribute('allow') || '').toLowerCase();
                // nothing here yet
            }
        } catch(_) {}
        return false;
    """)


def is_smart_verification_showing(driver: Any) -> bool:
    """综合三信号判断当前页面是否处于「需要人工介入的验证」状态。

    只要三种信号里任意一种命中 → 返回 True，进入人工介入流程。
    """
    try:
        if _dom_has_verification(driver):
            return True
        if _shadow_iframe_penetrate(driver):
            return True
        if _url_title_body_verification(driver):
            return True
    except Exception:
        pass
    return False


def wait_for_manual_verification(
    driver: Any,
    timeout_seconds: int = VERIFICATION_TIMEOUT,
    hold_lock: ManualHoldLock | None = None,
) -> bool:
    """等待用户手动完成验证码（接入人工介入锁，避免误判超时刷新）。

    行为流程：
      1. 控制台大字提醒
      2. force_focus 弹出 Windows MessageBox 抢占焦点
      3. 如果传入 hold_lock → 调用 acquire()，外部流程看到 is_holding=True 会挂起
      4. 每 2 秒轮询一次（比 v1 的 3 秒响应更快），每 10 秒打印进度
      5. 用户完成验证 → 释放 lock 并返回 True
      6. 超时后：点击关闭弹窗 + 刷新页面 + 释放 lock + 返回 False

    参数：
      driver           : Selenium WebDriver 实例
      timeout_seconds  : 最长等待秒数，默认 VERIFICATION_TIMEOUT (120s)
      hold_lock        : 可选 ManualHoldLock 实例，外部共享
    """
    print("")
    print("    " + "=" * 50)
    print("    !!  检测到智能验证，请在浏览器中手动完成验证  !!")
    print(f"    !!  等待 {timeout_seconds}s，超时自动刷新              !!")
    print("    " + "=" * 50)
    print("")

    force_focus()

    # 进入人工介入锁（若外部共享了锁实例）
    if hold_lock is None:
        hold_lock = ManualHoldLock()
    hold_lock.acquire()

    try:
        waited = 0
        while waited < timeout_seconds:
            time.sleep(2)
            waited += 2

            if not is_smart_verification_showing(driver):
                print(f"    验证已通过（{waited}s），继续运行")
                return True

            if waited % 10 == 0:
                print(f"    [{waited}s / {timeout_seconds}s] 仍在等待验证...")

        # 超时处理
        print(f"    验证等待超时，正在刷新页面...")
        try:
            driver.execute_script("""
                var c = document.querySelector('.layui-layer-close, .layui-layer-btn0,'
                                          +'[class*="close-btn"], [id*="closeBtn"], button[aria-label="Close"]');
                if (c) c.click();
            """)
        except Exception:
            pass
        try:
            driver.refresh()
        except Exception:
            pass
        time.sleep(2)
        return False
    finally:
        # 保证 lock 一定会被释放（即使抛异常）
        hold_lock.release()

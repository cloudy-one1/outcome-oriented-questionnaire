"""Stealth 反检测 JavaScript 构建器。

在 Selenium 执行 `Page.addScriptToEvaluateOnNewDocument` 时注入，
在页面脚本执行之前运行，确保所有指纹属性都已经被代理层劫持。

设计原则：
  - 所有"敏感属性"都通过 defineProperty + configurable: false 保护
    （问卷星的反爬脚本会尝试重新 defineProperty(navigator,'webdriver') 来确认是否是机器人）
  - 新增对 Object.defineProperty 的拦截：如果目标是 navigator.webdriver，直接拒绝
  - 新增对 Function.prototype.toString 的劫持：当 WebDriver 注入的函数被 toString() 时，
    返回原生代码的样子（`function xxx() { [native code] }`），而不是 `function anonymous() {...}`
"""

from __future__ import annotations

import textwrap


def build_stealth_js(
    *,
    screen_w: int,
    screen_h: int,
    avail_top: int,
    device_memory: int,
    hw_concurrency: int,
    timezone_offset_min: int,
) -> str:
    """构建一份注入用的 Stealth JavaScript 字符串。

    参数全部来自 Python 端随机挑选的"身份指纹"，保证每次启动浏览器都有不同的指纹组合。
    """
    # availLeft = 0（窗口最大化时左边没有间隙），availHeight = 真实高度 - 任务栏高度
    avail_left = 0
    avail_height = max(1, screen_h - avail_top)

    js = f"""
(function() {{
    'use strict';

    // ======================================================================
    // 1. Object.defineProperty 劫持 —— 阻止页面脚本把 webdriver 标志写回去
    // ======================================================================
    var _origDefineProperty = Object.defineProperty;
    var _origGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;

    Object.defineProperty = function(obj, prop, descriptor) {{
        // 任何脚本想改 navigator 的 webdriver → 静默失败，保持为 false
        if (obj === navigator && (prop === 'webdriver' || prop === 'driver' || prop === '__webdriver__')) {{
            return obj;
        }}
        return _origDefineProperty.call(Object, obj, prop, descriptor);
    }};
    Object.defineProperty.toString = function() {{
        return 'function defineProperty() {{ [native code] }}';
    }};

    Object.getOwnPropertyDescriptor = function(obj, prop) {{
        var desc = _origGetOwnPropertyDescriptor.call(Object, obj, prop);
        // 如果想检查 navigator.webdriver 是否 configurable → 伪造为 false
        if (obj === navigator && prop === 'webdriver' && desc) {{
            desc.configurable = false;
        }}
        return desc;
    }};

    // ======================================================================
    // 2. Function.prototype.toString 劫持 —— 把被 Selenium/代理包装过的函数
    //    返回为正常的 "[native code]" 字符串
    // ======================================================================
    var _origFnToString = Function.prototype.toString;
    // 需要"看起来原生"的关键函数名集合
    var _nativeFuncNames = new Set([
        'webdriver', 'plugins', 'mimeTypes', 'languages', 'language',
        'platform', 'vendor', 'hardwareConcurrency', 'deviceMemory',
        'cookieEnabled', 'onLine', 'product', 'appName', 'appCodeName',
        'appVersion', 'maxTouchPoints', 'permissions',
        'chrome', 'csi', 'loadTimes', 'runtime', 'app'
    ]);

    Function.prototype.toString = function() {{
        var src = _origFnToString.call(this);
        // 任何暴露给外部检查的、名字落在敏感集合里的函数 → 伪装成 native
        var name = this.name || '';
        if (_nativeFuncNames.has(name) || src.indexOf('apply(this, arguments)') !== -1) {{
            return 'function ' + name + '() {{ [native code] }}';
        }}
        return src;
    }};
    Function.prototype.toString.toString = function() {{
        return 'function toString() {{ [native code] }}';
    }};

    // ======================================================================
    // 3. 核心 navigator 属性（含 window.chrome 伪造）
    // ======================================================================
    var commonDesc = {{ configurable: false, enumerable: true }};

    // navigator.webdriver —— Selenium 默认 true 是最明显的机器人特征
    _origDefineProperty(navigator, 'webdriver', Object.assign({{ get: function() {{ return false; }} }}, commonDesc));

    // 基本信息（中国大陆 Edge 默认）
    _origDefineProperty(navigator, 'language', Object.assign({{ get: function() {{ return 'zh-CN'; }} }}, commonDesc));
    _origDefineProperty(navigator, 'languages', Object.assign({{ get: function() {{ return ['zh-CN','zh','en']; }} }}, commonDesc));
    _origDefineProperty(navigator, 'platform', Object.assign({{ get: function() {{ return 'Win32'; }} }}, commonDesc));
    _origDefineProperty(navigator, 'vendor', Object.assign({{ get: function() {{ return 'Google Inc.'; }} }}, commonDesc));
    _origDefineProperty(navigator, 'product', Object.assign({{ get: function() {{ return 'Gecko'; }} }}, commonDesc));
    _origDefineProperty(navigator, 'productSub', Object.assign({{ get: function() {{ return '20030107'; }} }}, commonDesc));
    _origDefineProperty(navigator, 'vendorSub', Object.assign({{ get: function() {{ return ''; }} }}, commonDesc));
    _origDefineProperty(navigator, 'appName', Object.assign({{ get: function() {{ return 'Netscape'; }} }}, commonDesc));
    _origDefineProperty(navigator, 'appCodeName', Object.assign({{ get: function() {{ return 'Mozilla'; }} }}, commonDesc));
    _origDefineProperty(navigator, 'cookieEnabled', Object.assign({{ get: function() {{ return true; }} }}, commonDesc));
    _origDefineProperty(navigator, 'onLine', Object.assign({{ get: function() {{ return true; }} }}, commonDesc));
    _origDefineProperty(navigator, 'doNotTrack', Object.assign({{ get: function() {{ return null; }} }}, commonDesc));
    _origDefineProperty(navigator, 'buildID', Object.assign({{ get: function() {{ return '20030107'; }} }}, commonDesc));
    _origDefineProperty(navigator, 'maxTouchPoints', Object.assign({{ get: function() {{ return 0; }} }}, commonDesc));

    // 硬件信息（来自 Python 端随机挑选的组合）
    _origDefineProperty(navigator, 'hardwareConcurrency', Object.assign({{ get: function() {{ return {hw_concurrency}; }} }}, commonDesc));
    _origDefineProperty(navigator, 'deviceMemory', Object.assign({{ get: function() {{ return {device_memory}; }} }}, commonDesc));

    // window.chrome —— 正常 Chrome/Edge 里有这个对象
    _origDefineProperty(window, 'chrome', Object.assign({{ configurable: false, enumerable: true, writable: false, value: {{
        runtime: {{
            OnInstalledReason: {{ INSTALL: 'install', UPDATE: 'update', SHARED_MODULE_UPDATE: 'shared_module_update' }},
            OnRestartRequiredReason: {{ APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }},
            PlatformArch: {{ ARM: 'arm', ARM64: 'arm64', X86_32: 'x86-32', X86_64: 'x86-64' }},
            PlatformOs: {{ MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' }},
            RequestUpdateCheckStatus: {{ THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' }},
            sendMessage: function() {{}},
            getBackgroundPage: function() {{}},
            getManifest: function() {{ return {{ version: '1.0' }}; }},
            getURL: function() {{ return ''; }},
            reload: function() {{ window.location.reload(); }},
            requestUpdateCheck: function(cb) {{ if(cb) cb('no_update', null); }},
            restart: function() {{}},
            connect: function() {{ return null; }},
            connectNative: function() {{ return null; }},
        }},
        loadTimes: function loadTimes() {{
            return {{
                commitLoadTime: Date.now()/1000,
                finishDocumentLoadTime: Date.now()/1000,
                finishLoadTime: Date.now()/1000,
                firstPaintAfterLoadTime: 0,
                firstPaintTime: 0,
                navigationType: 'Other',
                npnNegotiatedProtocol: '',
                requestTime: Date.now()/1000 - 0.1,
                startLoadTime: Date.now()/1000 - 0.05,
                wasAlternateProtocolAvailable: false,
                wasFetchedViaSpdy: false,
                wasNpnNegotiated: false,
            }};
        }},
        csi: function csi() {{
            return {{
                onloadT: Date.now(),
                startE: Date.now() - 200,
                tran: 15
            }};
        }},
        app: {{
            isInstalled: false,
            InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
            RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
            getDetails: function(cb) {{ if(cb) cb(); }},
            getIsInstalled: function() {{ return false; }},
            install: function() {{ return Promise.resolve(); }},
            runningState: function(cb) {{ if(cb) cb('cannot_run'); }},
        }},
    }}}}, commonDesc));

    // ======================================================================
    // 4. navigator.plugins / mimeTypes —— 伪造真实浏览器的 PDF / Native Client 插件
    // ======================================================================
    (function() {{
        function makePlugin(name, desc, version, mimeTypes) {{
            var plugin = {{
                name: name,
                description: desc,
                filename: name + '.dll',
                version: version,
                length: mimeTypes.length,
                item: function(i) {{ return this[i]; }},
                namedItem: function(nm) {{ return null; }},
                refresh: function() {{}},
            }};
            mimeTypes.forEach(function(mt, i) {{
                plugin[i] = mt;
                plugin[mt.type] = mt;
            }});
            return plugin;
        }}
        function makeMimeType(type, suffix, pluginName) {{
            return {{
                type: type,
                suffixes: suffix,
                description: '',
                enabledPlugin: pluginName,
            }};
        }}
        var p1 = makePlugin('PDF Viewer', 'Portable Document Format', '', []);
        var m1 = makeMimeType('application/pdf', 'pdf', p1);
        var m2 = makeMimeType('text/pdf', 'pdf', p1);
        p1[0] = m1; p1[1] = m2;
        p1.length = 2;

        var plugins = [p1];
        plugins.length = plugins.length;

        plugins.item = function(i) {{ return this[i]; }};
        plugins.namedItem = function(nm) {{
            for (var i = 0; i < this.length; i++) if (this[i] && this[i].name === nm) return this[i];
            return null;
        }};
        plugins.refresh = function() {{}};

        var mimes = [m1, m2];
        mimes.length = mimes.length;
        mimes.item = function(i) {{ return this[i]; }};
        mimes.namedItem = function(nm) {{
            for (var i = 0; i < this.length; i++) if (this[i] && this[i].type === nm) return this[i];
            return null;
        }};

        _origDefineProperty(navigator, 'plugins', Object.assign({{ get: function() {{ return plugins; }} }}, commonDesc));
        _origDefineProperty(navigator, 'mimeTypes', Object.assign({{ get: function() {{ return mimes; }} }}, commonDesc));
    }})();

    // ======================================================================
    // 5. screen 属性（窗口尺寸必须与启动时 --window-size 匹配）
    // ======================================================================
    var screenW = {screen_w};
    var screenH = {screen_h};
    var availH = {avail_height};
    var availL = {avail_left};
    var availT = {avail_top};
    function sGet(v) {{ return function() {{ return v; }}; }}
    _origDefineProperty(screen, 'width', Object.assign({{ get: sGet(screenW) }}, commonDesc));
    _origDefineProperty(screen, 'height', Object.assign({{ get: sGet(screenH) }}, commonDesc));
    _origDefineProperty(screen, 'availWidth', Object.assign({{ get: sGet(screenW) }}, commonDesc));
    _origDefineProperty(screen, 'availHeight', Object.assign({{ get: sGet(availH) }}, commonDesc));
    _origDefineProperty(screen, 'availLeft', Object.assign({{ get: sGet(availL) }}, commonDesc));
    _origDefineProperty(screen, 'availTop', Object.assign({{ get: sGet(availT) }}, commonDesc));
    _origDefineProperty(screen, 'colorDepth', Object.assign({{ get: sGet(24) }}, commonDesc));
    _origDefineProperty(screen, 'pixelDepth', Object.assign({{ get: sGet(24) }}, commonDesc));

    // outerWidth / outerHeight 非零（无头浏览器常见 0 值）
    _origDefineProperty(window, 'outerWidth', Object.assign({{ get: sGet(screenW) }}, commonDesc));
    _origDefineProperty(window, 'outerHeight', Object.assign({{ get: sGet(screenH) }}, commonDesc));
    _origDefineProperty(window, 'innerWidth', Object.assign({{ get: sGet(screenW - 16) }}, commonDesc));
    _origDefineProperty(window, 'innerHeight', Object.assign({{ get: sGet(Math.max(1, availH - 120)) }}, commonDesc));

    // ======================================================================
    // 6. Date.getTimezoneOffset() — 中国 UTC+8 必须返回负值 -480
    // ======================================================================
    var _tz = {timezone_offset_min};
    var _origDateGetTimezoneOffset = Date.prototype.getTimezoneOffset;
    Date.prototype.getTimezoneOffset = function getTimezoneOffset() {{ return _tz; }};

    // ======================================================================
    // 7. permissions.query 劫持 —— 所有权限默认 prompt
    // ======================================================================
    if (navigator.permissions && navigator.permissions.query) {{
        var _perm = navigator.permissions;
        var _origQuery = _perm.query;
        _perm.query = function permissions_query(descriptor) {{
            if (!descriptor || !descriptor.name)
                return _origQuery.apply(this, arguments);
            var name = descriptor.name;
            var defaultState = {{
                notifications: 'default',
                geolocation: 'prompt',
                midi: 'prompt',
                midiSysex: 'prompt',
                'screen-wake-lock': 'prompt',
                push: 'prompt',
                camera: 'prompt',
                microphone: 'prompt',
                speaker: 'prompt',
                'clipboard-read': 'granted',
                'clipboard-write': 'granted',
                'accessibility-events': 'granted',
                background-sync: 'granted',
                'payment-handler': 'granted',
                'persistent-storage': 'prompt',
                idle-detection: 'prompt',
                'top-level-storage-access': 'prompt',
                window-management: 'granted',
            }};
            var st = defaultState[name] || 'prompt';
            return Promise.resolve({{
                state: st,
                onchange: null,
                addEventListener: function(){{}},
                removeEventListener: function(){{}},
                dispatchEvent: function(){{ return true; }},
            }});
        }};
    }}

    // ======================================================================
    // 8. 清理 Selenium 注入的全局属性（比 cdc_ 列表更全）
    // ======================================================================
    var _badPrefixes = ['cdc_', 'selenium_', '__webdriver_', '__selenium_', 'webdriver_',
                        '__driver_evaluate', '__webdriver_script_fn',
                        '__webdriver_script_func', '__fxdriver_',
                        'domAutomation', 'domAutomationController'];
    function sweep(o) {{
        try {{
            if (!o) return;
            Object.keys(o).forEach(function(k) {{
                for (var pi = 0; pi < _badPrefixes.length; pi++) {{
                    if (k.indexOf(_badPrefixes[pi]) === 0) {{
                        try {{ delete o[k]; }} catch (_) {{
                            try {{ o[k] = undefined; }} catch (__) {{}}
                        }}
                    }}
                }}
            }});
        }} catch (_) {{}}
    }}
    sweep(window);
    sweep(document);
    sweep(Object.getPrototypeOf(window));
    // 300ms 后再扫一次（防止 Selenium 后续注入）
    setTimeout(function() {{ sweep(window); sweep(document); }}, 300);
    setTimeout(function() {{ sweep(window); sweep(document); }}, 1500);

    // ======================================================================
    // 9. WebGL 指纹扰动（轻微抖动 renderer 名称末尾小版本号）
    // ======================================================================
    try {{
        var _origGetExtension = WebGLRenderingContext.prototype.getExtension;
        WebGLRenderingContext.prototype.getExtension = function(ext) {{
            if (ext === 'WEBGL_debug_renderer_info') {{
                // 返回真实 Google Inc. / ANGLE 的伪装（主流 Win10+ 机器）
                return {{
                    UNMASKED_VENDOR_WEBGL: 0x9245,
                    UNMASKED_RENDERER_WEBGL: 0x9246,
                    getParameter: function(pname) {{
                        if (pname === 0x9245) return 'Google Inc. (Intel)';
                        if (pname === 0x9246) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11-27.20.100.8682)';
                        return null;
                    }},
                }};
            }}
            return _origGetExtension.apply(this, arguments);
        }};
    }} catch (_) {{}}

    // ======================================================================
    // 10. iframe contentWindow 劫持（跨 frame 检测绕过）
    // ======================================================================
    try {{
        _origDefineProperty(HTMLIFrameElement.prototype, 'contentWindow', Object.assign({{
            get: function() {{ return window; }}
        }}, commonDesc));
    }} catch (_) {{}}

    // ======================================================================
    // 11. connection.rtt / downlink 模拟真实网络
    // ======================================================================
    if (navigator.connection) {{
        var _c = navigator.connection;
        _origDefineProperty(_c, 'rtt', Object.assign({{ get: function() {{ return 50 + Math.floor(Math.random()*30); }} }}, commonDesc));
        _origDefineProperty(_c, 'downlink', Object.assign({{ get: function() {{ return 10.0 + Math.random()*50; }} }}, commonDesc));
        _origDefineProperty(_c, 'effectiveType', Object.assign({{ get: function() {{ return '4g'; }} }}, commonDesc));
    }}

    // ======================================================================
    // 12. keyboard 伪造（正常 Chrome 有 navigator.keyboard API）
    // ======================================================================
    if (!navigator.keyboard) {{
        try {{
            _origDefineProperty(navigator, 'keyboard', Object.assign({{
                value: {{
                    getLayoutMap: function() {{ return Promise.resolve(new Map()); }},
                    lock: function() {{ return Promise.resolve(); }},
                    unlock: function() {{ return Promise.resolve(); }},
                    addEventListener: function(){{}},
                    removeEventListener: function(){{}},
                    dispatchEvent: function(){{ return true; }},
                }}
            }}, commonDesc));
        }} catch (_) {{}}
    }}
}})();
"""
    # textwrap.dedent 去掉 4 空格前缀，让 JS 保持规范缩进
    return textwrap.dedent(js)

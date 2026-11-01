"""Stealth 反检测 JavaScript 构建器。

在 Selenium 执行 `Page.addScriptToEvaluateOnNewDocument` 时注入，
在页面脚本执行之前运行，确保所有指纹属性都已经被代理层劫持。

设计原则：
  - 所有"敏感属性"都通过 defineProperty + getter 保护；webdriver 单独用
    configurable: false 锁死（问卷星的反爬脚本会尝试重新
    defineProperty(navigator,'webdriver') 来确认是否是机器人，这是在真实
    环境里验证过的对抗层，不随其他属性一起放松）
  - 其余伪造属性的描述符与真实浏览器对齐（configurable: true）——
    "所有 navigator 属性都不可配置"本身就是可检特征
  - 新增对 Object.defineProperty 的拦截：如果目标是 navigator.webdriver
    （实例或原型），直接拒绝
  - Function.prototype.toString 劫持分三层：(a) 伪造函数登记表（含匿名
    getter，堵"getter 源码穿透"）；(b) 函数名白名单；(c) Selenium 包装
    函数的 apply(this, arguments) 源码特征

v4.4 对照 D4Vinci/Scrapling 的 stealth 实现审计后的补强（docs/stealth_audit.md）：
  - 清扫前缀补 $cdc_ / $wdc_（经典 chromedriver 变量以 $ 开头，indexOf==0 匹配不到）
  - connection 的 rtt/downlink 改为每次文档加载算一次（连读两次值会变 = 机器人特征）
  - plugins 重写为 5 个 PDF 插件 + Plugin/PluginArray 正确原型链（普通 Array 的
    instanceof / Object.prototype.toString.call 会被当场识破）
  - 补 navigator.pdfViewerEnabled、Notification.permission（与 permissions 静态表联动）、
    Intl.DateTimeFormat().resolvedOptions().timeZone（与 getTimezoneOffset 的 -480 对齐）
  - PermissionStatus 用真原型构造（instanceof 成立，事件方法直接继承 EventTarget）
  - WebGL 同款劫持覆盖 WebGL2，并在真实 context 的 getParameter 上拦
    0x9245/0x9246（页面惯用 gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)，走的是原型方法）
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
    var _defPropHook = function(obj, prop, descriptor) {{
        // 任何脚本想改 navigator（含其原型）的 webdriver → 静默失败，保持为 false
        var navProto = null;
        try {{ navProto = Object.getPrototypeOf(navigator); }} catch (_) {{}}
        if ((obj === navigator || obj === navProto) &&
            (prop === 'webdriver' || prop === 'driver' || prop === '__webdriver__')) {{
            return obj;
        }}
        return _origDefineProperty.call(Object, obj, prop, descriptor);
    }};
    Object.defineProperty = _defPropHook;

    // ======================================================================
    // 2. Function.prototype.toString 劫持
    //    (a) __nativeLike 登记表：每个伪造函数（含匿名 getter）都登记成
    //        "function <label>() {{ [native code] }}"，堵匿名 getter 源码穿透
    //    (b) 函数名白名单 + (c) apply(this, arguments) 源码特征：兜 Selenium
    //        运行时注入的包装函数
    // ======================================================================
    var _origFnToString = Function.prototype.toString;
    var _nativeLike = new Map();
    function __nativeLike(fn, label) {{
        _nativeLike.set(fn, 'function ' + label + '() {{ [native code] }}');
        return fn;
    }}
    function __nativeTree(obj) {{
        // 对象字面量里的方法全是匿名函数，逐个登记成原生样
        Object.keys(obj).forEach(function(k) {{
            if (typeof obj[k] === 'function') __nativeLike(obj[k], k);
        }});
        return obj;
    }}
    // 需要"看起来原生"的关键函数名集合（兜 (b)/(c) 两条路径用）
    var _nativeFuncNames = new Set([
        'webdriver', 'plugins', 'mimeTypes', 'languages', 'language',
        'platform', 'vendor', 'hardwareConcurrency', 'deviceMemory',
        'cookieEnabled', 'onLine', 'product', 'appName', 'appCodeName',
        'appVersion', 'maxTouchPoints', 'permissions',
        'chrome', 'csi', 'loadTimes', 'runtime', 'app'
    ]);
    var _fnToStringHook = function() {{
        var nat = _nativeLike.get(this);
        if (nat) return nat;
        var src = _origFnToString.call(this);
        var name = this.name || '';
        if (_nativeFuncNames.has(name) || src.indexOf('apply(this, arguments)') !== -1) {{
            return 'function ' + name + '() {{ [native code] }}';
        }}
        return src;
    }};
    __nativeLike(_fnToStringHook, 'toString');
    __nativeLike(_defPropHook, 'defineProperty');
    Function.prototype.toString = _fnToStringHook;

    // ======================================================================
    // 3. 核心 navigator 属性（含 window.chrome 伪造）
    // ======================================================================
    // v4.4：描述符与真实浏览器对齐（configurable: true）——"所有属性都不可
    // 配置"本身是可检特征；webdriver 例外（见 lockDesc）
    var commonDesc = {{ configurable: true, enumerable: true }};
    // webdriver 例外：configurable: false 是在真实问卷星环境验证过的对抗层
    //（它 redefine webdriver 确认真假），一个属性锁死换整层安全
    var lockDesc = {{ configurable: false, enumerable: true }};

    // 伪造一个访问器属性：getter 走 __nativeLike 登记，toString 报原生样
    function __spoof(obj, prop, label, value) {{
        _origDefineProperty(obj, prop, Object.assign(
            {{ get: __nativeLike(function() {{ return value; }}, label) }}, commonDesc));
    }}

    // navigator.webdriver —— Selenium 默认 true 是最明显的机器人特征
    _origDefineProperty(navigator, 'webdriver', Object.assign(
        {{ get: __nativeLike(function() {{ return false; }}, 'get webdriver') }}, lockDesc));

    // 基本信息（中国大陆 Edge 默认）
    __spoof(navigator, 'language', 'get language', 'zh-CN');
    __spoof(navigator, 'languages', 'get languages', ['zh-CN','zh','en']);
    __spoof(navigator, 'platform', 'get platform', 'Win32');
    __spoof(navigator, 'vendor', 'get vendor', 'Google Inc.');
    __spoof(navigator, 'product', 'get product', 'Gecko');
    __spoof(navigator, 'productSub', 'get productSub', '20030107');
    __spoof(navigator, 'vendorSub', 'get vendorSub', '');
    __spoof(navigator, 'appName', 'get appName', 'Netscape');
    __spoof(navigator, 'appCodeName', 'get appCodeName', 'Mozilla');
    __spoof(navigator, 'cookieEnabled', 'get cookieEnabled', true);
    __spoof(navigator, 'onLine', 'get onLine', true);
    __spoof(navigator, 'doNotTrack', 'get doNotTrack', null);
    __spoof(navigator, 'buildID', 'get buildID', '20030107');
    __spoof(navigator, 'maxTouchPoints', 'get maxTouchPoints', 0);

    // 硬件信息（来自 Python 端随机挑选的组合；deviceMemory 已由 HW_PRESETS 钳在 ≤8）
    __spoof(navigator, 'hardwareConcurrency', 'get hardwareConcurrency', {hw_concurrency});
    __spoof(navigator, 'deviceMemory', 'get deviceMemory', {device_memory});

    // pdfViewerEnabled —— 无头/精简内核常见缺失（creepjs "Like Headless" 检查项）
    try {{ __spoof(navigator, 'pdfViewerEnabled', 'get pdfViewerEnabled', true); }} catch (_) {{}}

    // window.chrome —— 正常 Chrome/Edge 里有这个对象
    //（writable: true 与真实浏览器一致；可写无妨，页面写坏了自己承担）
    _origDefineProperty(window, 'chrome', Object.assign({{ configurable: false, enumerable: true, writable: true, value: __nativeTree({{
        runtime: __nativeTree({{
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
        }}),
        loadTimes: __nativeLike(function loadTimes() {{
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
        }}, 'loadTimes'),
        csi: __nativeLike(function csi() {{
            return {{
                onloadT: Date.now(),
                startE: Date.now() - 200,
                tran: 15
            }};
        }}, 'csi'),
        app: __nativeTree({{
            isInstalled: false,
            InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
            RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
            getDetails: function(cb) {{ if(cb) cb(); }},
            getIsInstalled: function() {{ return false; }},
            install: function() {{ return Promise.resolve(); }},
            runningState: function(cb) {{ if(cb) cb('cannot_run'); }},
        }}),
    }})}}, commonDesc));

    // ======================================================================
    // 4. navigator.plugins / mimeTypes —— 5 个 PDF 插件 + 正确原型链
    //    真实 Chrome/Edge 是 5 个 PDF 插件；旧实现只造 1 个且是普通 Array，
    //    instanceof PluginArray / Object.prototype.toString.call 会被当场识破。
    //    走 Object.create(Plugin.prototype) 后 item/namedItem/refresh 直接从
    //    真原型继承（同样没有 toString 穿透面）。
    // ======================================================================
    try {{
        if (!(navigator.plugins && navigator.plugins.length > 0)) {{
            function makeMime(type, suffixes, plugin) {{
                var m = Object.create(MimeType.prototype);
                _origDefineProperty(m, 'type', {{ value: type, enumerable: true }});
                _origDefineProperty(m, 'suffixes', {{ value: suffixes, enumerable: true }});
                _origDefineProperty(m, 'description', {{ value: '', enumerable: true }});
                _origDefineProperty(m, 'enabledPlugin', {{ value: plugin, enumerable: true }});
                return m;
            }}
            function makePlugin(name, mimeDefs) {{
                var p = Object.create(Plugin.prototype);
                _origDefineProperty(p, 'name', {{ value: name, enumerable: true }});
                _origDefineProperty(p, 'description', {{ value: 'Portable Document Format', enumerable: true }});
                _origDefineProperty(p, 'filename', {{ value: 'internal-pdf-viewer', enumerable: true }});
                _origDefineProperty(p, 'length', {{ value: mimeDefs.length, enumerable: true }});
                mimeDefs.forEach(function(def, i) {{
                    _origDefineProperty(p, String(i), {{ value: makeMime(def[0], def[1], p), enumerable: true }});
                }});
                try {{ p[Symbol.iterator] = Array.prototype[Symbol.iterator]; }} catch (_) {{}}
                return p;
            }}

            var pdfOnly = [['application/pdf', 'pdf']];
            var pdfAndText = [['application/pdf', 'pdf'], ['text/pdf', 'pdf']];
            var p1 = makePlugin('PDF Viewer', pdfAndText);
            var p2 = makePlugin('Chrome PDF Viewer', pdfOnly);
            var p3 = makePlugin('Chromium PDF Viewer', pdfOnly);
            var p4 = makePlugin('Microsoft Edge PDF Viewer', pdfOnly);
            var p5 = makePlugin('WebKit built-in PDF', pdfOnly);

            var all = [p1, p2, p3, p4, p5];
            var plugins = Object.create(PluginArray.prototype);
            _origDefineProperty(plugins, 'length', {{ value: all.length, enumerable: true }});
            all.forEach(function(p, i) {{
                _origDefineProperty(plugins, String(i), {{ value: p, enumerable: true }});
            }});
            try {{ plugins[Symbol.iterator] = Array.prototype[Symbol.iterator]; }} catch (_) {{}}

            // 真实 mimeTypes 就两条：application/pdf、text/pdf（enabledPlugin 指向 PDF Viewer）
            var mimeList = [p1[0], p1[1]];
            var mimes = Object.create(MimeTypeArray.prototype);
            _origDefineProperty(mimes, 'length', {{ value: mimeList.length, enumerable: true }});
            mimeList.forEach(function(m, i) {{
                _origDefineProperty(mimes, String(i), {{ value: m, enumerable: true }});
            }});
            try {{ mimes[Symbol.iterator] = Array.prototype[Symbol.iterator]; }} catch (_) {{}}

            __spoof(navigator, 'plugins', 'get plugins', plugins);
            __spoof(navigator, 'mimeTypes', 'get mimeTypes', mimes);
        }}
    }} catch (_) {{}}

    // ======================================================================
    // 5. screen 属性（窗口尺寸必须与启动时 --window-size 匹配）
    // ======================================================================
    var screenW = {screen_w};
    var screenH = {screen_h};
    var availH = {avail_height};
    var availL = {avail_left};
    var availT = {avail_top};
    __spoof(screen, 'width', 'get width', screenW);
    __spoof(screen, 'height', 'get height', screenH);
    __spoof(screen, 'availWidth', 'get availWidth', screenW);
    __spoof(screen, 'availHeight', 'get availHeight', availH);
    __spoof(screen, 'availLeft', 'get availLeft', availL);
    __spoof(screen, 'availTop', 'get availTop', availT);
    __spoof(screen, 'colorDepth', 'get colorDepth', 24);
    __spoof(screen, 'pixelDepth', 'get pixelDepth', 24);

    // outerWidth / outerHeight 非零（无头浏览器常见 0 值）
    __spoof(window, 'outerWidth', 'get outerWidth', screenW);
    __spoof(window, 'outerHeight', 'get outerHeight', screenH);
    __spoof(window, 'innerWidth', 'get innerWidth', screenW - 16);
    __spoof(window, 'innerHeight', 'get innerHeight', Math.max(1, availH - 120));

    // ======================================================================
    // 6. Date.getTimezoneOffset() — 中国 UTC+8 必须返回负值 -480
    // ======================================================================
    var _tz = {timezone_offset_min};
    Date.prototype.getTimezoneOffset = __nativeLike(
        function getTimezoneOffset() {{ return _tz; }}, 'getTimezoneOffset');

    // ======================================================================
    // 7. Intl.DateTimeFormat 时区 —— getTimezoneOffset 钉了 UTC+8，
    //    resolvedOptions().timeZone 若仍报宿主机时区就自相矛盾
    // ======================================================================
    try {{
        var _origResolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
        Intl.DateTimeFormat.prototype.resolvedOptions = __nativeLike(
            function resolvedOptions() {{
                var opts = _origResolvedOptions.apply(this, arguments);
                try {{ opts.timeZone = 'Asia/Shanghai'; }} catch (_) {{}}
                return opts;
            }},
            'resolvedOptions');
    }} catch (_) {{}}

    // ======================================================================
    // 8. permissions.query 劫持 —— 静态状态表 + 真原型 PermissionStatus
    // ======================================================================
    if (navigator.permissions && navigator.permissions.query) {{
        var _perm = navigator.permissions;
        var _origQuery = _perm.query;
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
            'background-sync': 'granted',
            'payment-handler': 'granted',
            'persistent-storage': 'prompt',
            'idle-detection': 'prompt',
            'top-level-storage-access': 'prompt',
            'window-management': 'granted',
        }};
        _perm.query = __nativeLike(function query(descriptor) {{
            if (!descriptor || !descriptor.name)
                return _origQuery.apply(this, arguments);
            var st = defaultState[descriptor.name] || 'prompt';
            // v4.4：真原型构造 —— instanceof PermissionStatus 成立，
            // addEventListener 等事件方法直接从 EventTarget.prototype 继承
            var status;
            try {{
                status = Object.create(PermissionStatus.prototype);
            }} catch (_) {{
                status = {{
                    onchange: null,
                    addEventListener: function(){{}},
                    removeEventListener: function(){{}},
                    dispatchEvent: function(){{ return true; }},
                }};
                __nativeTree(status);
            }}
            try {{
                _origDefineProperty(status, 'state', {{ value: st, enumerable: true, configurable: true, writable: true }});
                _origDefineProperty(status, 'onchange', {{ value: null, enumerable: true, configurable: true, writable: true }});
            }} catch (_) {{}}
            return Promise.resolve(status);
        }}, 'query');
    }}

    // ======================================================================
    // 9. Notification.permission —— 与上面静态表的 notifications: 'default'
    //    联动；两处不一致即露馅
    // ======================================================================
    try {{
        if (window.Notification && Notification.permission !== 'default') {{
            _origDefineProperty(Notification, 'permission', Object.assign(
                {{ get: __nativeLike(function() {{ return 'default'; }}, 'get permission') }}, commonDesc));
        }}
    }} catch (_) {{}}

    // ======================================================================
    // 10. 清理 Selenium 注入的全局属性（比 cdc_ 列表更全）
    //     v4.4：补 $cdc_ / $wdc_ —— 经典 chromedriver 变量以 $ 开头
    //     （document.$cdc_asdjflasutopfhvcZLmcfl_、window 上的
    //     $cdc_adoQpoasnfa76pfcZLmcfl_*），indexOf(...)==0 永远扫不到
    // ======================================================================
    var _badPrefixes = ['$cdc_', '$wdc_', 'cdc_', 'selenium_', '__webdriver_', '__selenium_', 'webdriver_',
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
    // 11. WebGL 指纹伪装（vendor/renderer 固定为主流 Win10+ Intel 核显）
    //     v4.4：WebGL1/2 原型同款劫持，并在**真实 context 的 getParameter**
    //     上拦 0x9245/0x9246 —— 页面惯用 gl.getParameter(ext.UNMASKED_*)，
    //     走的是原型方法，只伪装 getExtension 返回对象拦不住
    // ======================================================================
    try {{
        var _glVendor = 'Google Inc. (Intel)';
        var _glRenderer = 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11-27.20.100.8682)';
        function __hookWebGL(proto) {{
            if (!proto) return;
            var _origGetExtension = proto.getExtension;
            if (_origGetExtension) {{
                proto.getExtension = __nativeLike(function getExtension(ext) {{
                    if (ext === 'WEBGL_debug_renderer_info') {{
                        return {{
                            UNMASKED_VENDOR_WEBGL: 0x9245,
                            UNMASKED_RENDERER_WEBGL: 0x9246,
                            getParameter: __nativeLike(function getParameter(pname) {{
                                if (pname === 0x9245) return _glVendor;
                                if (pname === 0x9246) return _glRenderer;
                                return null;
                            }}, 'getParameter'),
                        }};
                    }}
                    return _origGetExtension.apply(this, arguments);
                }}, 'getExtension');
            }}
            var _origGetParameter = proto.getParameter;
            if (_origGetParameter) {{
                proto.getParameter = __nativeLike(function getParameter(pname) {{
                    if (pname === 0x9245) return _glVendor;
                    if (pname === 0x9246) return _glRenderer;
                    return _origGetParameter.apply(this, arguments);
                }}, 'getParameter');
            }}
        }}
        __hookWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
        __hookWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
    }} catch (_) {{}}

    // ======================================================================
    // 12. iframe contentWindow —— v4.2 刻意**不**劫持。
    // 此前把所有 iframe 的 contentWindow 都指向顶层 window：同源 iframe 里
    // 页面自己的 self/parent/top 全错（问卷页一旦嵌编辑器/验证组件/广告位就被
    // 破坏），而 `iframe.contentWindow === window` 这个过强不变量本身就是检测点
    // （真实浏览器里永远为 false）。绕过跨 frame 检测的收益抵不过这些代价。
    // ======================================================================

    // ======================================================================
    // 13. connection.rtt / downlink 模拟真实网络
    //     v4.4：值在每次文档加载时算一次 —— 真实浏览器的 rtt/downlink 在
    //     change 事件之间是稳定的，旧实现每次读取重新随机，连读两次值就变，
    //     本身就是机器人特征
    // ======================================================================
    if (navigator.connection) {{
        var _c = navigator.connection;
        var _rtt = 50 + Math.floor(Math.random() * 30);
        var _dl = Math.round((10.0 + Math.random() * 50) * 10) / 10;
        __spoof(_c, 'rtt', 'get rtt', _rtt);
        __spoof(_c, 'downlink', 'get downlink', _dl);
        __spoof(_c, 'effectiveType', 'get effectiveType', '4g');
    }}

    // ======================================================================
    // 14. keyboard 伪造（正常 Chrome 有 navigator.keyboard API）
    // ======================================================================
    if (!navigator.keyboard) {{
        try {{
            _origDefineProperty(navigator, 'keyboard', Object.assign({{
                value: __nativeTree({{
                    getLayoutMap: function() {{ return Promise.resolve(new Map()); }},
                    lock: function() {{ return Promise.resolve(); }},
                    unlock: function() {{ return Promise.resolve(); }},
                    addEventListener: function(){{}},
                    removeEventListener: function(){{}},
                    dispatchEvent: function(){{ return true; }},
                }})
            }}, commonDesc));
        }} catch (_) {{}}
    }}
}})();
"""
    # textwrap.dedent 去掉 4 空格前缀，让 JS 保持规范缩进
    return textwrap.dedent(js)

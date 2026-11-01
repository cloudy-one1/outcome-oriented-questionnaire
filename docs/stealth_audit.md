# Stealth 层对照审计（v4.4）

对照基准：[D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling)（BSD-3）的 stealth 实现
——v0.3.x（rebrowser-patches + 7 个自研注入 JS，`scrapling/engines/toolbelt/bypasses/*.js`）
与 v0.5（patchright 驱动 + 启动参数层，`scrapling/engines/_browsers/_stealth.py`、
`engines/constants.py`）。两代源码均已逐文件读过原文，技术清单见文末附录。

被审对象：`src/browser/driver_factory_stealth.py`（stealth JS）、
`src/browser/driver_factory.py`（CDP 配置 + 启动参数）、`src/config.py`（指纹预设）。

---

## 0. 最重大发现：stealth JS 从进库起就是死代码

`permissions.query` 静态状态表里有三个带连字符的键**没加引号**：
`background-sync:`、`idle-detection:`、`window-management:` —— 这是 JS 语法错误，
整段 IIFE 解析失败。而 `Page.addScriptToEvaluateOnNewDocument` 对解析失败**静默不执行、
不报任何错**，`_apply_stealth_cdp` 的 try/except 也吞不到 —— 结论：

> **v4.4 之前，390 行 stealth JS 在每一次真实运行中都没有生效过。**
> 问卷星没拦住我们，靠的是 `--disable-blink-features=AutomationControlled` +
> `excludeSwitches` + UA/Client-Hints 对齐这些**浏览器级/CDP 级**措施，与这段 JS 无关。

三层测试防线（离线字符串比对、裸 selenium E2E、CI）全都看不见它，原因：
离线断言只做字符串包含比对；E2E 刻意用裸 selenium（不带项目指纹配置）；当时没有
任何测试校验过生成 JS 的语法。

**修复与门禁**：
- 三个键加引号（`driver_factory_stealth.py` permissions 段）；
- `tests/test_driver_factory_offline.py::test_stealth_js_passes_node_syntax_check`：
  生成 JS 过 `node --check`，node 缺席时 skip；
- `tests/test_stealth_e2e.py`：经项目 driver factory 真启动 headless 浏览器加载
  `tests/fixtures/stealth_probe.html`，20+ 项指纹检查逐项断言 —— 补上"真浏览器 +
  真注入路径"这最后一环。

---

## 1. 已修复项

### P0 —— 当下就构成可检特征的真 bug

| # | 问题 | 修复 |
|---|------|------|
| 1 | **stealth JS 整体死代码**（见上节） | 三键加引号 + node --check 门禁 + 探针页验收 |
| 2 | `$cdc_` 前缀漏扫：清扫用 `indexOf(prefix) === 0`，而经典 chromedriver 变量以 `$` 开头（`document.$cdc_asdjflasutopfhvcZLmcfl_`、window 上的 `$cdc_adoQpoasnfa76pfcZLmcfl_*`），永远扫不到 | `_badPrefixes` 头部补 `'$cdc_'`、`'$wdc_'` |
| 3 | `HW_PRESETS` 含 `(16,12)`/`(16,16)`：`navigator.deviceMemory` 规范上限是 8（>8GB 一律报 8），报 16 直接自曝 | 钳到 ≤8：`(8,8) (8,12) (8,16) (4,8) (8,6) (4,4)`（config.py） |
| 4 | connection 的 rtt/downlink **每次读取重新随机** —— 真实浏览器在 change 事件之间值稳定，连读两次值就变 = 机器人特征 | 值在每次文档加载时算一次，闭包固定 |
| 5 | acceptLanguage 两处不一致：请求头 `zh-CN,zh;q=0.9,en;q=0.8` vs UA override `zh-CN,zh;q=0.9`，HTTP 层与 JS 层语言指纹打架 | 收口为模块常量 `_ACCEPT_LANGUAGE`，两处同源 |
| 6 | `wow64: True` 硬编码 —— WOW64 指"32 位进程跑在 64 位系统"，对 `Win64; x64` 的 UA 是错的 | 从 UA 推导（`"WOW64" in ua`） |
| 7 | doc/code 漂移：模块头声称 WebGL"轻微抖动"（实际固定值）、声称伪造 `navigator.webkitPersistentStorage`（JS 中不存在） | 注释改为如实描述 |

### P1 —— 形态加固（对照 Scrapling `webdriver_fully.js` / `navigator_plugins.js`）

| # | 问题 | 修复 |
|---|------|------|
| 8 | toString 劫持对**匿名 getter** 穿透：`Object.getOwnPropertyDescriptor(navigator,'webdriver').get.toString()` 能拿到真实源码；`permissions_query`、`getTimezoneOffset`、chrome.runtime/app 方法同理 | 引入 `__nativeLike(fn, label)` 登记表：每个伪造函数（含匿名 getter、劫持钩子自身）都返回 `function <label>() { [native code] }`；`__nativeTree` 批量登记对象字面量方法；同时删掉 `defineProperty.toString`/`toString.toString` 的自有属性覆盖（自有非原生属性本身可被 `getOwnPropertyDescriptor` 检出，登记表方案没有这个面） |
| 9 | plugins 只造 1 个插件且是普通 Array 冒充：`instanceof PluginArray` 失败、`Object.prototype.toString.call` 为 `[object Array]`、`namedItem` 恒 null；真实 Chrome/Edge 是 5 个 PDF 插件 | 重写为 `Object.create(Plugin.prototype)`/`PluginArray.prototype`/`MimeType.prototype`/`MimeTypeArray.prototype` 真原型链，5 插件 2 mimeType，item/namedItem/refresh/迭代器从真原型继承（顺带消灭了这批函数的 toString 面） |
| 10 | 伪造属性全部 `configurable: false` —— 真实浏览器里 `navigator.language`、`screen.*` 等都是 `configurable: true`，"全部不可配置"是集合级异常 | 描述符对齐真实（configurable: true）；**webdriver 例外**保持 `configurable: false` 锁死（它在真实问卷星环境验证过，防 redefine 确认攻击），`Object.getOwnPropertyDescriptor` 的对应劫持因此成为纯冗余，已删除 |
| 11 | 假 PermissionStatus 是普通对象：`instanceof PermissionStatus` 失败、事件方法要手写桩（桩又引入 toString 面） | `Object.create(PermissionStatus.prototype)` 构造，事件方法从 EventTarget 真原型继承 |
| 12 | `Notification.permission` 与 permissions.query 静态表（notifications: 'default'）不联动 | 钉为 `'default'`（对照 Scrapling `notification_permission.js`） |
| 13 | `getTimezoneOffset` 钉 -480 但 `Intl.DateTimeFormat().resolvedOptions().timeZone` 报宿主机时区，自相矛盾 | resolvedOptions 包一层，timeZone 钉 `'Asia/Shanghai'` |
| 14 | WebGL2 完全未覆盖；且只拦 `getExtension` 返回对象的 `getParameter`，页面惯用的 `gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)` 走**真实 context 的原型方法**，直接泄漏真 GPU | `WebGL1/2` 原型同款劫持；在原型 `getParameter` 上拦 0x9245/0x9246（直接传 pname 也拦） |
| 15 | `navigator.pdfViewerEnabled` 缺失（无头/精简内核特征，creepjs "Like Headless" 检查项；对照 Scrapling `pdf_viewer.js`） | 补 `true` |
| 16 | `window.chrome` 的 `writable: false` 与真实浏览器（可写）不符 | 改 `writable: true`（Scrapling v0.3.x 同款选择） |

### P2 —— 浏览器级参数（对照 Scrapling `STEALTH_ARGS`，driver_factory.py 的 `STEALTH_EXTRA_ARGS`）

| 参数 | 理由 |
|------|------|
| `--disable-ipc-flooding-protection` | 对抗基于 CDP/IPC 调用频率的自动化检测（undetected-chromedriver 亦内置） |
| `--force-color-profile=srgb` | 稳定色彩输出，canvas/WebGL 指纹跨启动一致的前提 |
| `--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4` | 声明"主输入是鼠标"，自动化环境缺该声明是特征（取值照抄 Scrapling） |
| `--fingerprinting-canvas-image-data-noise` | Chrome 原生 canvas 指纹噪声：渲染进程加噪，**没有可被 toString 检出的 JS hook**（JS 钩 toDataURL 的方案反而容易被抓） |

三条浏览器路径（Edge / Chrome 原生 / Chrome UC）均已接入。

---

## 2. 明确不做（及理由）

| 项 | 理由 |
|----|------|
| WebRTC IP 泄漏处理（`--webrtc-ip-handling-policy` 等） | 本项目是本机真实 IP 直连场景，HTTP 与 WebRTC 看到的是同一个 IP，不存在代理泄露面；加策略反而引入行为差异 |
| DoH（`--dns-over-https-templates`） | 同上 + 本机网络受限（代理/镜像环境），DoH 可能直接断网 |
| CDP Runtime.enable / console.debug 泄漏的驱动级修复 | 该泄漏是 Puppeteer/Playwright 系的 `Runtime.enable` 副作用；chromedriver 的 `execute_script` 走 `Runtime.evaluate` 且默认不 enable Runtime 域，Selenium 场景受影响小。驱动级修复（patchright/CDP-Patches 思路）改造成本与收益不成比例 |
| 字体枚举 / AudioContext / mediaDevices / codec 探测的 JS 钩 | 问卷星威胁模型（指纹 + 行为风控）未见使用；每多一个 hook 多一个 toString/一致性面 |
| Sec-CH-UA `Not_A Brand` GREASE 版本轮换 | 弱特征，真实轮换规律按 milestone 走；如需可后续按 UA 大版本哈希选取 |
| iframe contentWindow 关联 | v4.2 决策维持：过强不变量本身是检测点且破坏页内组件（stealth JS 第 12 节注释） |
| Sec-Fetch-\* 请求头伪造 | v4.2 决策维持（`setExtraHTTPHeaders` 作用于所有请求，全请求带 document 头本身可检） |

## 3. 剩余已知局限（有意保留，审计可查）

1. **伪造属性挂在实例而非原型**：真实 Chrome 的 `navigator.language`/`screen.*` 等定义在
   `Navigator.prototype`/`Screen.prototype` 上；高级探针（creepjs 一类）检查原型上是否有
   该 getter 可发现差异。Scrapling 的 `webdriver_fully.js` 就是定义在原型上的。本次未迁移
   是因为实例级方案已在真实环境跑通，迁移是行为面整体变更，应单独验证后再做。
2. **Worker / OOPIF 不覆盖**：`addScriptToEvaluateOnNewDocument` 不进 Worker 作用域；
   跨进程 iframe（OOPIF）与 `window.open` 的新 target 属独立 session。问卷星验证组件
   通常为同进程 iframe（已覆盖），风险低。
3. **WebGL vendor/renderer 全集群同值**（Intel UHD 620）：与 HW_PRESETS 的低配组合存在
   集群一致性弱特征；如需可做成参数化预设（与屏幕/硬件组合联动）。
4. **Selenium 注入包装函数的 toString**：白名单 + `apply(this, arguments)` 源码特征兜底，
   覆盖常见形态，但不穷尽。
5. **`Not_A Brand";v="24"` 固定**（同上表，未做轮换）。

## 4. 验收

- 离线：`tests/test_driver_factory_offline.py`
  - `test_stealth_js_passes_node_syntax_check` —— 语法死代码门禁
  - `test_stealth_extra_args_land_on_every_browser_path` —— 三路径参数
  - `test_hw_presets_keep_device_memory_within_spec`
  - `test_stealth_js_contains_v44_hardening_markers`
  - `test_edge_ua_builds_edge_metadata_payload` / `test_wow64_flag_derived_from_the_ua`
    —— acceptLanguage 同源、wow64 推导
- 真浏览器：`tests/test_stealth_e2e.py`（经 driver factory，`-m integration`）
  - 探针页 `tests/fixtures/stealth_probe.html` 20+ 项：webdriver 三层防御、getter
    toString、plugins 原型链、pdfViewerEnabled、deviceMemory、connection 稳定性、
    Notification/Intl 时区一致性、chrome.csi、screen 自洽、WebGL1/2 双面拦截、$cdc_ 清扫

---

## 附录：Scrapling 两代 stealth 的技术形态（审计基准摘要）

- **v0.5（当前 main）**：Python 编排 + patchright 驱动（规避 Runtime.enable/Console.enable
  泄漏）+ 启动参数层（移除 `--enable-automation` 等 5 个 Playwright 有害默认参数、
  `--disable-blink-features=AutomationControlled`、blink-settings 指针声明、原生 canvas
  噪声 flag、srgb/GPU 稳定 flag、`--start-maximized`+`--window-position=0,0`、
  `--disable-ipc-flooding-protection`）+ context 层（color_scheme=dark + deviceScaleFactor=2
  过 creepjs prefersLightColor、1920x1080 screen/viewport 归一、预授予
  geolocation/notifications、service_workers allow、持久化 user-data-dir、
  `channel="chrome"` 真实品牌浏览器）。JS 注入几乎为零。
- **v0.3.x**：rebrowser-patches 驱动 + 7 个按序 `add_init_script` 的 JS：
  `webdriver_fully.js`（Navigator.prototype 层 getter + toString 伪装）、
  `window_chrome.js`（headless 下造 chrome 对象，属性顺序保持、csi/loadTimes 从真实
  performance timing 派生、报错堆栈净化）、`navigator_plugins.js`（5 插件 + 真原型）、
  `pdf_viewer.js`、`notification_permission.js`、`screen_props.js`、
  `playwright_fingerprint.js`（删 `__pwInitScripts`/`__playwright__binding__`）。
- **两代都没有**的项（双方共同盲区，本项目同样未做）：WebGL vendor/renderer 伪造、
  WebRTC JS 层处理、字体枚举防护、media codec 伪造、hardwareConcurrency/deviceMemory
  的页面 JS 伪造、audio 指纹处理、Sec-CH-UA 手工拼装（由 browserforge/curl_cffi 承担）。
- `cdc_` 在 Scrapling 中无任何处理 —— Playwright 系不用 chromedriver，该检测面天然不适用；
  **Selenium 项目必须自扫**（见 P0 #2）。

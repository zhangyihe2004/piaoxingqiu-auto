# 票星球 WEB / APK 运行机制研究笔记

更新时间：2026-07-31  
研究范围：官方移动网页、Android APK、当前 `piaoxingqiu-auto` 实现  
目的：记录可用于提高兼容性、稳定性和可诊断性的事实，不研究验证码绕过或风控规避。

## 1. 证据范围

### 1.1 官方 WEB

- 检查页面：
  `https://m.piaoxingqiu.com/content/6a6091f85fc7f100012580a7?showId=6a6091f85fc7f100012580a7`
- 页面构建版本：
  `fa29ba0c8b-1785308118`
- 主模块：
  `assets/index-C10_4rbh.js`
- 页面实际加载的相关资源包括：
  - `collect-rangers-v5.2.9.js`
  - `angry_dog_outapi/assets/c.js`
  - `angry_dog_outapi/assets/f.js`
  - `angry_dog_outapi/assets/z.js`
  - `/static/workers/pow.js`
  - `h5-verify-modal.CinGTKbL.js`
  - `antidom.js`

### 1.2 Android APK

- 文件：
  `D:\develop\110_05d68225aecd6e774a4ab6e50e8e961f.apk`
- 大小：55,973,928 bytes
- SHA-256：
  `15C04074B64C9408568E027BC92914AF6745F954BADD831DE278FBFAC1A2B8C3`
- Flutter 主业务：
  `lib/arm64-v8a/libapp.so`
- 关键资源：
  - `assets/captcha_index.html`
  - `assets/flutter_assets/assets/data/property.json`
  - `assets/flutter_assets/assets/i18n/zh.json`
  - `assets/flutter_assets/packages/captcha/assets/...`

### 1.3 当前项目

- 当前提交：`6dad1ad`
- 检查时工作树已有用户修改：
  `piaoxingqiu_auto/platform/cart.py`
- 本次研究没有修改该文件。

## 2. 结论分级

- **A：已证实**  
  能在官方当前 WEB 代码、APK 代码或真实资源中直接定位。
- **B：高可信**  
  官方代码存在完整结构，但尚未在真实触发场次观察完整请求和响应。
- **C：待实抓**  
  目前只是合理推断，不能进入生产判断。

## 3. 风控并不是一个开关，而是多层体系

官方当前流程至少包含以下层次：

```text
页面进入
├── 页面级 checkOnPage
│   └── 可能进入 risk_queue
├── 风控 SDK 初始化
│   ├── 设备指纹
│   ├── Angry-dog
│   ├── Wr-Token
│   ├── Rc-Token
│   └── PoW Worker
├── 业务请求
│   └── 每次生成 front-trace-id
├── 响应头风控指令
│   ├── 33007201：排队
│   └── 33007202：人机验证
└── 业务响应模式
    ├── limiting：自动限流重试
    ├── retry：人工重试
    └── 普通业务成功/失败
```

这些层次不能统一映射成“登录失效”，也不能只读取 JSON 业务码。

## 4. WEB 端已确认机制

### 4.1 风控 SDK 与登录生命周期绑定

**证据等级：A**

官方 WEB 会在登录、退出和页面激活时执行不同操作：

- 登录：更新 `userId` 和 `accessToken`，重新建立指纹环境。
- 退出：重置并重新初始化风险 SDK。
- 页面激活：检查初始化状态并执行 `fpSetup`。
- 登录态不存在：执行 `fpReset`。

因此：

- 风控环境不只是一个可长期复制的静态 token。
- 浏览器上下文、账号登录态、页面生命周期和 show 上下文共同参与。
- 把某次 `Angry-dog` 值复制到另一个上下文不能视为等价实现。

### 4.2 官方请求客户端动态添加多类请求头

**证据等级：A**

WEB 请求拦截器可见以下行为：

- 每个请求添加新的 `front-trace-id`。
- 有缓存值时添加 `Angry-dog`。
- 根据接口和 `showId` 添加 `Wr-Token`。
- 根据接口和 `showId` 添加 `Rc-Token`。
- 购物车涉及多个节目时，使用 `showIdList` 取得列表型 token。

这说明 `pre_order` / `create_order` 的安全上下文是动态且与节目相关的。

当前项目通过 booking 页面内置客户端发送请求，这一方向正确：
[cart.py](../piaoxingqiu_auto/platform/cart.py#L428)。

### 4.3 风控控制信号位于响应头

**证据等级：A**

官方 WEB 会读取：

- `rc-code` / `Rc-Code`
- `rc-id` / `Rc-Id`
- `rc-show-id` / `Rc-Show-Id`

目前确认：

- `33007201`：进入官方排队流程。
- `33007202`：进入官方人机验证流程。
- `rc-id`：作为本次排队或验证会话的标识。
- `rc-show-id`：指出该风控事件对应的节目。

重构前的创建响应监听只读取 JSON，未读取这些响应头。当前
[submission.py](../piaoxingqiu_auto/platform/submission.py)
已同时解析响应 JSON 与 `rc-code / rc-id / rc-show-id`。

### 4.4 页面级排队先于创建订单

**证据等级：A**

官方在订单相关页面激活时调用：

```text
checkOnPage({showId, scene: "order"})
```

返回 `action == "queue"` 时，页面直接进入 `risk_queue`。  
请求响应中出现 `33007201` 时，也会通过 `queueId` 进入相同队列。

因此排队不一定由 `create_order` 的 JSON 错误触发，也可能在进入购票页面时就发生。

### 4.5 人机验证成功后的行为与请求类型有关

**证据等级：A**

官方收到 `33007202` 后：

- 非 `create_order` 请求：启动验证，但当前拦截器不会直接自动重发该请求。
- `create_order` 请求：完成验证后，使用原始 `requestOptions` 通过官方客户端重新发送。

因此建议：

- `create_order`：保留原请求，交给官方客户端在验证成功后重发。
- `pre_order`：验证成功后由业务流程重新执行预下单，不假定原调用已经成功。

### 4.6 `mode=limiting` 是独立的官方重试协议

**证据等级：A**

官方限流弹层的真实行为：

- 保留原始请求。
- 首次等待 3 秒。
- 每 3 秒重新发送一次原请求。
- 最多重试 3 次。
- 只有新响应仍为 `mode=limiting` 才继续。
- 用户切换页面、响应不再是 limiting、HTTP 异常或用户取消时立即停止。
- 用户取消后返回上一页并刷新业务页面。

因此不能仅凭提示“正在为您自动尝试”判断登录失效，也不能在每次重试时重新构造座位和订单。

官方通用请求器本身还支持：

- `interval`
- `maxInterval`
- `maxRetryCount`
- `shouldStop`
- `onRetry`

说明重试策略属于响应模式，不应仅靠业务码硬编码。

### 4.7 特定 HTTP 状态会转为业务重试

**证据等级：A**

对高重要性请求，官方会把部分 HTTP 状态转换成统一重试响应：

- 461～469
- 999
- 418
- 403
- 405

转换后使用 `mode=retry`，提示访问人数过多或网络错误。

这说明 HTTP 405 等错误不一定意味着接口永久不可用；是否重试还取决于请求重要性和官方响应模式。

### 4.8 演出模型预留主动验证时间窗

**证据等级：A（字段存在） / B（具体触发条件待实抓）**

官方节目模型包含：

- `captchaSwitch`
- `captchaVerifyStartTime`
- `captchaVerifyEndTime`
- `captchaTypes`

这与用户观察到“开抢前提前出现看图计算”等现象一致。  
字段存在已确认，但仍需抓到一个真实开启该开关的演出，验证：

- 时间单位和时区。
- 是否每个账号都必须验证。
- 验证结果是 show 级、账号级还是浏览器上下文级。
- 验证完成后的有效期。

### 4.9 其他官方能力开关

**证据等级：A**

节目模型还包含：

- `supportOneClickPurchase`
- `supportGrabActivity`
- `grabActivityId`
- `existWaitList`
- `countdownSessionVOS`

这些不是普通的“票档有无票”字段，而是不同购票模式的能力开关。后续若支持官方一键抢票、组队抢票或候补，应优先读取这些字段，而不是从按钮文字猜测。

## 5. APK 已确认机制

### 5.1 购票风控题型完整存在

**证据等级：A**

Flutter 业务中存在：

- `ComputeCaptchaState`：图片计算题。
- `ClickIconCaptchaState`：依次点击指定图标。
- `BlockPuzzleCaptchaState`：拼图。
- `MaskSlideCaptchaState`：滑块。
- `CurveFittingCaptchaState`
- `SemanticCaptchaState`
- `SpaceCaptchaState`
- `EmptyCaptchaState`

APK 同时包含：

- `ShowCaptcha`
- `showCaptchaInfos`
- `needCaptcha`
- `verifyCaptcha`
- `captchaSwitch`
- `captchaVerifyStartTime`
- `captchaVerifyEndTime`
- `captchaTypes`

用户所说的“看图片算数”就是其中的 `ComputeCaptchaState`。

### 5.2 Angry-dog 验证接口

**证据等级：A**

APK 中包含：

- `/angry_dog_outapi/api/captcha/get`
- `/angry_dog_outapi/api/captcha/verify`
- `/angry_dog_outapi/api/v2/morefun/captcha/get`
- `/angry_dog_outapi/api/v2/morefun/captcha/verify`

同时包含：

- `33007201`
- `33007202`
- `Wr-Token`
- `Rc-Token`
- `Angry-Dog-Token`
- `Angry-Dog-Trace-Id`

WEB 与 APK 对这套协议的命名一致。

### 5.3 验证次数与冷却时间由服务端控制

**证据等级：A**

APK 中文资源包含：

- “仅限验证 {totalCount} 次”
- “{totalTtl} 分钟内最多可验证 {totalCount} 次”
- “验证次数已达上限，请 {ttl} 秒后再试”
- “验证失败，请重新计算”
- “验证失败，请重新拖动滑块”
- “验证失败，请依次点击图标，顺序不可更改”

因此程序不应自定义无限重试，也不应把达到上限视为网络故障。

### 5.4 APK 还集成了独立的阿里云验证码桥

**证据等级：A**

`assets/captcha_index.html`：

- 加载阿里云验证码 SDK。
- `mode = embed`
- `deviceType = native`
- `immediate = true`
- 支持 `SLIDING`。
- 明确把 `TRACELESS` 视为当前视图不支持的类型。
- 成功后取得 `certifyId` 并回传原生层。
- 生成 `u_atoken / u_asig / u_aref` 后继续原 GET/POST 请求。

这套桥接与 Angry-dog 内部的计算题、点图、拼图类状态不是同一个 UI 实现，不能合并成一种验证码。

### 5.5 IP 限流参数

**证据等级：A**

APK `property.json`：

- `httpLimitDelayTime = 3000`
- `IPLimitedImageUrl`

中文资源：

- “抢票太火爆啦！”
- “您正在排队中，稍后再试”

这与 WEB `mode=limiting` 的 3 秒间隔互相印证。

### 5.6 设备环境与加固线索

**证据等级：B**

DEX / Flutter 字符串包含：

- Frida
- Xposed
- Root / Rooted
- Emulator
- VPN
- Proxy
- Debugger

APK 原生库包含：

- `libdexvmp.so`
- `libEncryptorP.so`
- `libentryexpro.so`
- `libtiger_tally.so`
- `libzxprotect.so`

这些足以说明 APK 集成了加固、设备环境或安全相关组件，但目前不能证明每一个标识都会直接影响创建订单。除非取得真实运行日志或调用链，否则不得写入生产判断。

## 6. 与抢票相关的其他有价值机制

### 6.1 预填信息不是订单锁定

**证据等级：A**

APK 明确提示：

- 抢票期间会根据预填数量和实名信息自动勾选。
- 如果实际购票数量少于预填数量，需要人工选择。
- 购买任意票品生成订单后，该预填信息失效。
- 最终信息以抢票开启后的数据为准。

因此预填适合减少 UI 操作，不能作为实时库存、最终票档或最终实名结果的依据。

### 6.2 官方存在组队抢票和预约抢票

**证据等级：A**

APK 包含接口：

- `/buyer/v1/grab/pre_field/list`
- `/buyer/v1/grab/team/create`
- `/buyer/v1/grab/team/detail`
- `/buyer/v1/grab/team/operate`
- `/buyer/v1/grab/team/unlock_slot`
- `/buyer/v3/pre_filed_and_grab`
- `/buyer/order/team/v1/create_order`

中文资源包含：

- “邀请好友一起抢票，能大大增加抢票成功率”
- “通知队长支付”
- “抢票成功”
- “继续抢票”

它是独立订单通道，不应直接复用普通 `cart/v1/create_order` 的构造规则。

### 6.3 官方候补按完整需求匹配

**证据等级：A**

候补规则明确：

- 按候补订单创建顺序出票。
- 新库存必须同时满足候补订单的数量和票价需求。
- 不满足前一个订单时，可顺延给后一个满足条件的订单。
- 多张候补票不保证连座。
- 候补订单需要预付票款，并可能包含候补服务费。
- 正式订单或其他候补订单已占用同一观演人时，候补可能不兑现。

这意味着“出现任意库存就提醒”不等同于“满足候补需求”。

### 6.4 实名年龄限制是结构化规则

**证据等级：A**

APK 中存在：

- 指定年龄范围。
- 至少包含某年龄范围观演人。
- 某票档限若干名特定年龄观演人。
- `ageLimit` / `audience_age_limit`。

错误 `27902332` 之类不应简单归类为“实名人数不足”；未来应由官方观演人规则给出结构化诊断。

### 6.5 人脸核验不是购票验证码

**证据等级：A**

APK 包含：

- `faceAuthSuccess`
- `faceDemoted`
- `faceLimited`
- `faceTimeout`
- “需要使用摄像头校验面部信息”
- “人脸识别实人认证授权”

这是实名身份或入场资格验证，涉及摄像头和生物信息授权，不应进入自动化流程。

### 6.6 自动选座也可能排队

**证据等级：A**

APK 包含：

- `/buyer/v1/auto_seat_picking_state/`
- “排队选座中，请勿离开”
- “当前抢票人数较多，请返回详情页查看最新库存”

因此 `SUPPORT_NONE` 与自动分配座位并不总是完全等价；某些项目可能走服务端自动选座状态机。

### 6.7 订单创建成功响应有明确保护字段

**证据等级：A**

APK 和当前 WEB 模型均包含：

- `orderId`
- `orderNumber`
- `paidDeadLineTime`
- `unPaidTransactionIds`

当前项目已经解析这些字段：
[submission.py](../piaoxingqiu_auto/platform/submission.py)。

### 6.8 频繁取消订单可能影响后续购买

**证据等级：A（提示存在） / B（适用项目待确认）**

APK 提示：

> 当天取消订单达到 3 次，将不可再次购买。

这说明用“创建后取消”反复做真实测试存在账号级副作用。后续实验应优先使用网络层拦截，不应把取消订单当作无成本操作。

## 7. 当前项目覆盖情况

| 能力 | 当前状态 | 结论 |
|---|---|---|
| 复用官方 booking 请求客户端 | 已有 | 正确，应保留 |
| 官方动态风控头 | 由官方客户端产生 | 正确，不应本地伪造 |
| 创建成功响应字段 | 已解析 | 已覆盖 |
| JSON 业务码分类 | 已有 | 只覆盖业务响应体 |
| `rc-code / rc-id / rc-show-id` | 已解析 | 只记录非敏感状态字段 |
| `33007201` 排队 | 已建模 | 保留页面、请求体与提交租约 |
| `33007202` 人机验证 | 已建模 | 等待官方页面验证后重发 |
| 主动验证时间窗 | 缺失 | PREWARM 无法提前提示 |
| `mode=limiting` 官方重试语义 | 已建模 | 原业务体每 3 秒重发，最多 3 次 |
| 人工验证状态 | 运行时已区分 | 不复用账号级 `NEEDS_LOGIN` |
| 服务器端交互式验证入口 | 缺失 | headless 环境无法完成滑块/点图 |
| 设备完整性判断 | 未证实 | 暂不实现 |

重构前曾将 `33000000` 统一映射成 `RESELECT`。该映射现已删除：
只有明确的座位冲突码才重选；只有响应明确包含
`mode=limiting` 才执行同一业务体的三秒重发。

## 8. 最小兼容方案

以下最小模型已由
[submission.py](../piaoxingqiu_auto/platform/submission.py)
实现；尚未实抓的协议细节仍按第 14 节验证，不凭字段名称继续扩展。

### 8.1 增加独立状态

建议使用绑定级状态：

```text
VERIFY_REQUIRED
QUEUEING
RATE_LIMITED
```

不要把它们写入账号级 `NEEDS_LOGIN`。

### 8.2 统一风险信号

每次高重要性响应同时读取：

```text
HTTP 状态
JSON statusCode / code / subCode / mode
rc-code
rc-id
rc-show-id
```

只保留非敏感诊断字段，不记录：

- Cookie
- Authorization
- Angry-dog 值
- Wr-Token
- Rc-Token
- 完整证件号

### 8.3 保留原浏览器上下文

出现排队或验证时：

- 不关闭 booking 热页面。
- 不重建浏览器上下文。
- 不清空官方风控状态。
- 不重新构造并狂发 create_order。

验证结果与原页面、show 和风险会话相关，重建上下文可能让验证失效。

### 8.4 请求恢复规则

```text
33007201 / queue
→ 保留页面
→ 进入官方 risk_queue
→ 等待官方放行

33007202 / captcha
→ 保留页面
→ 等待人工完成官方验证
→ create_order 由官方客户端重发原请求
→ pre_order 由业务流程重新执行

mode=limiting
→ 原请求、3 秒、最多 3 次
→ mode 改变或页面改变立即停止
```

### 8.5 人工验证而非自动解题

计算题只是题型之一；还存在点图、拼图和滑块。即使能识别答案，也仍需要官方 SDK 在原上下文签发验证凭证。

因此可靠方案是提供一个：

- 仅管理员可访问；
- 一次性链接；
- 短时有效；
- 绑定准确账号和任务；
- 直接操作原 Playwright 页面；

的远程验证入口。

不建议把验证码图片和答案经飞书拼装成通用自动验证流程，更不能实现验证码绕过。

## 9. 下一轮实抓清单

以下项目仍需真实触发后确认：

1. 一个 `captchaSwitch=true` 的预售演出完整动态数据。
2. `captchaVerifyStartTime / EndTime` 的单位、时区和刷新来源。
3. `33007201` 时真实 `rc-id / rc-show-id` 组合。
4. `33007202` 分别发生在 `pre_order` 和 `create_order` 时的页面行为。
5. 验证成功后 token 的有效范围：账号、show、页面或浏览器上下文。
6. 同一账号在另一个设备完成验证，服务器上下文是否会同步。
7. `33000000` 对应 JSON `mode` 和响应头的真实组合。
8. APK 中 Root / Emulator / VPN / Proxy 标识是否实际进入购票风控判定。
9. 自动选座排队接口的状态集合和终止条件。
10. 候补、组队抢票与普通购物车订单是否共用订单保护字段。

## 10. 最重要的工程结论

1. **继续使用官方 booking 请求客户端是正确方向。**
2. **风险判断必须同时读取响应头和响应体。**
3. **排队、验证码、限流和登录失效是四种不同状态。**
4. **验证完成前必须保留原浏览器上下文。**
5. **`33000000` 不能脱离 `mode`、响应头和真实页面状态单独解释。**
6. **真实订单测试存在取消次数、实名限购和风控副作用，应优先使用安全拦截实验。**

## 11. 第二轮 WEB 深入取证

### 11.1 风控头只覆盖特定高重要性接口

**证据等级：A**

当前 WEB 构建中可以直接确认三组接口判断：

- `Wr-Token` 接口组：
  - `buyer/order/v5/pre_order`
  - `buyer/order/cart/v1/pre_order`
  - `buyer/order/v5/create_order`
  - `buyer/order/v3/create_order`
  - `buyer/order/cart/v1/create_order`
  - `show_user`
- `Rc-Token` 接口组：
  - 包含上述接口；
  - 另包含组队抢票详情和组队订单创建接口。
- 多节目购物车接口：
  - 从 `orders[]` 中提取多个 `showId`；
  - 普通订单从首个商品中提取单一 `showId`。

每次请求还会生成新的 `front-trace-id`，形式为当前时间和随机串组合。由此可以确定：

1. 风控头并不是全站统一添加。
2. `pre_order` 和 `create_order` 都属于高重要性请求。
3. 多节目购物车不能只拿一个 `showId` 生成风险上下文。
4. 生产代码继续调用官方请求客户端是正确的；把这些头复制成静态配置是错误方向。

### 11.2 官方排队是持续状态机

**证据等级：A**

`risk_queue` 页面内部状态完整可见：

```text
Init
→ Idle
→ Captcha
→ POW
→ Fingerprint
→ Success / Fail
```

进入页面后只启动一次 `runQueue(showId)`。队列尚未结束而页面被卸载时，会调用
`endQueue(showId)`。成功后再跳转，失败后结束本次队列。

这带来三个明确结论：

- 排队不是固定等待若干秒后重发。
- 验证、PoW、设备指纹可能是同一个排队会话中的不同阶段。
- 关闭页面、重建上下文或错误返回上一页，可能主动终止官方队列。

因此 `QUEUEING` 必须持有原页面和 `rc-id`，不能交给普通任务轮询器销毁。

### 11.3 官方验证码恢复保留原请求

**证据等级：A**

收到 `rc-code = 33007202` 时，官方行为按请求类型分开：

- `create_order`：
  - 保存原始 `requestOptions`；
  - 打开官方验证；
  - 验证通过后用官方请求客户端重新发送同一份业务请求；
  - 风控头在重发时重新生成。
- `pre_order`：
  - 打开官方验证；
  - 当前响应拦截器不直接自动重发；
  - 验证完成后需要业务层重新执行预下单。

这里的“同一份请求”指业务参数不变，不代表复用旧的动态风控头。

另外，懒加载模块 `h5-verify-modal` 实际是学生身份认证方式选择器，不是购票风控验证码。仅凭文件名把它接入风控流程会走错方向。

### 11.4 主动验证码时间窗仍不能进入生产判断

**证据等级：A（字段） / C（主动触发行为）**

`captchaSwitch`、`captchaVerifyStartTime`、`captchaVerifyEndTime` 和
`captchaTypes` 已确认存在于节目模型，但当前 WEB 主模块和已加载业务模块中只确认了字段定义，尚未确认某个页面根据这些字段主动打开风控验证。

在真实开启项目被抓到之前，只能把它们视为服务端能力元数据，不能据此：

- 提前暂停账号；
- 假定验证码已完成；
- 推算验证有效期；
- 自动跳转到某个验证页面。

## 12. 第二轮 APK 深入取证

### 12.1 阿里云验证证明与原请求绑定

**证据等级：A**

APK 的 `assets/captcha_index.html` 明确生成：

```text
u_atoken = requestInfo.token
u_asig   = certifyId
u_aref   = requestInfo.refer
```

随后继续原 GET 或 POST。由此可以确定：

- `certifyId` 不是可跨请求长期复用的通用登录凭证。
- 验证结果至少与原请求 token 和 referer 绑定。
- 在另一个浏览器、另一个页面或另一个节目中复用验证结果没有代码依据。

### 12.2 APK 验证页本身是原生桥接流程

**证据等级：A**

该页面使用阿里云 SDK：

- `mode = embed`
- `deviceType = native`
- `immediate = true`
- `verifyType = 1.0`

并通过原生桥处理：

- 验证参数上送；
- 成功；
- 失败；
- 初始化错误；
- WebView 高度变化；
- 加载状态。

页面支持 `SLIDING`，但遇到 `TRACELESS` 会把它报告为当前 WebView 不支持，而不是伪造成功。

失败后页面提供 `captcha.refresh()`，说明正确恢复方式是在同一验证流程中刷新挑战，而不是关闭账号页面后重新登录。

### 12.3 两个时间配置不能混为一谈

**证据等级：A（字段） / B（完整调用路径）**

APK 配置中存在：

- `httpLimitDelayTime = 3000`
- `retryThresholdSeconds = 15`

前者与 WEB `mode=limiting` 的三秒间隔相互印证。后者位于商店全局配置中，目前没有证据证明它是订单创建次数或创建重试间隔。

APK 的 `monitorInterval = 300000` 属于性能/系统监控配置，也不是余票轮询频率。

这些字段不能仅因名称相似就合并进抢票调度器。

### 12.4 APK 暴露了更多风险协议名称

**证据等级：A（存在） / B（关系）**

除 WEB 已确认的头外，APK 还包含：

- `Angry-Dog-Token`
- `Angry-Dog-Trace-Id`
- `show-captcha-token`
- `captcha_rc_id`
- `userCaptchaToken`
- `needPOW`
- `needFingerprint`
- `inQueue`
- `outQueue`

这些字符串与 WEB 的排队状态机能够互相印证，但 AOT 编译后的字符串邻接关系不足以还原每个字段的完整调用顺序。生产实现应以 WEB 真实响应和运行时抓包为准，不能凭字符串拼出请求。

## 13. 重构前项目中确认的冲突

### 13.1 “一个逻辑订单”不等于“一个 HTTP 请求”

**证据等级：A**

重构前的 `OrderFirewall` 每次武装后只允许一个物理
`create_order` 请求。

但官方一个逻辑下单过程可能包含：

```text
首次 create_order
├── 正常终态响应
├── 人机验证
│   └── 验证成功后重发原 create_order
└── mode=limiting
    └── 每 3 秒重发原 create_order，最多 3 次
```

因此当前防火墙会把官方验证码恢复或限流恢复产生的第二个物理请求视为“重复订单”并拦截。它保护了请求次数，却破坏了官方的逻辑订单状态机。

正确的安全边界应是“一个逻辑提交租约”：

- 只允许一个账号、一个任务、一个确定的订单业务体进入提交态。
- 官方客户端可以在同一租约内按官方状态机重发。
- 重发必须保持业务体摘要、账号、任务和节目一致。
- 只接受一个终态成功订单。
- 页面离开、业务体变化、超时或终态响应后立即关闭租约。

这并不意味着开放无限创建请求。

### 13.2 当前响应监听看不到风险控制信号

**证据等级：A**

重构前的 `CreateResponseWatcher` 只解析响应 JSON。

它没有记录：

- `rc-code`
- `rc-id`
- `rc-show-id`
- JSON `mode`

因此它可能把一个尚在官方验证或排队状态中的中间响应，当作普通创建失败处理。

### 13.3 重构前 `33000000 → RESELECT` 证据不足

**证据等级：A（重构前代码） / C（统一语义）**

重构前代码把 `33000000` 映射成 `RESELECT`，最多进行五次完整重选：

- 重新查询库存；
- 可能重新选择座位；
- 重新预下单；
- 再次创建。

官方已确认的 `mode=limiting` 行为则是：

- 保留原业务请求；
- 等待三秒；
- 重新生成风控头并重发原请求；
- 最多三次；
- 响应不再是 limiting 时停止。

目前仍没有证据证明所有 `33000000` 都带 `mode=limiting`。所以不能简单把五次重选改成三次原请求重发；必须先读取 `mode` 和风险响应头，再选择对应状态机。

### 13.4 最小修正模型

当前实现采用以下最小模型：

```text
首次请求响应
├── 终态成功
│   └── CREATED
├── rc-code=33007201
│   └── QUEUEING，保留页面和租约
├── rc-code=33007202
│   └── VERIFY_REQUIRED，等待人工验证
├── mode=limiting
│   └── RATE_LIMITED，由官方客户端原请求重发
├── 明确座位冲突码
│   └── 关闭当前租约，刷新库存并重选
├── 明确业务拒绝
│   └── 关闭租约并按业务规则停止或等待
└── 请求已发出但无终态
    └── UNKNOWN，冻结该绑定并人工核对
```

排队、验证和限流期间，订单保护仍然有效，但不能把官方内部重发误判为第二个独立订单。

## 14. 后续最有价值的安全实验

1. 在 Lab 中只记录高重要性响应的非敏感元数据：
   - URL 路径；
   - HTTP 状态；
   - `rc-code / rc-id / rc-show-id`；
   - JSON `code / subCode / mode`；
   - 不保存 token 和请求体值。
2. 找到真实 `33007201` 后观察：
   - 队列页面是否复用 booking 上下文；
   - 页面卸载是否立即结束队列；
   - 成功后恢复到哪个业务动作。
3. 找到真实 `33007202` 后分别观察：
   - `pre_order` 是否只完成验证而不重发；
   - `create_order` 是否自动重发完全相同的业务体；
   - 重发时 `front-trace-id` 是否变化。
4. 抓取一个真实 `33000000`：
   - 同时保存非敏感响应头和 `mode`；
   - 确认它究竟属于 limiting、验证码、排队还是普通业务拒绝。
5. APK 实机仅观察官方日志和网络元数据，不尝试自动解题或伪造设备证明。

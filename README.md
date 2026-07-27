# piaoxingqiu-auto

票星球余票监控与自动创建订单服务。使用公开库存接口、Playwright、SQLite
和飞书企业自建应用；程序只创建待支付订单，不执行支付。

## 设计

```text
piaoxingqiu_auto/
├── domain/      纯业务规则：运行模型、销售阶段、座位选择、执行结果
├── platform/    票星球适配：HTTP、认证、库存、预下单、订单
├── runtime/     运行时：浏览器池、单账号购买流程、耗时统计
├── app/         应用服务：数据库、任务调度、命令、登录、绑定配置
├── adapters/    外部通道：飞书长连接与消息卡片
├── config.py    系统配置与数据路径
└── cli.py       doctor / serve
```

依赖关系保持清晰：`domain` 不依赖外部实现；`platform` 只依赖领域模型；
`runtime` 组合领域规则与票星球适配；`app` 编排运行时、数据库和飞书通道；
`cli` 只负责创建并连接这些组件。

正式服务和诊断入口使用同一套领域规则与运行时代码。项目不包含 Lab、
benchmark、旧数据库迁移和历史命令兼容层。

## 安装

```powershell
cd D:\develop\piaoxingqiu-auto
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m playwright install chromium
python -m piaoxingqiu_auto doctor
```

Linux：

```bash
cd /home/zhangyihe2004/piaoxingqiu-auto
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
python -m piaoxingqiu_auto doctor
```

首次运行会生成：

```text
~/.piaoxingqiu-auto/config.json
~/.piaoxingqiu-auto/piaoxingqiu-auto.db
~/.piaoxingqiu-auto/accounts/
```

可以通过 `PIAOXINGQIU_AUTO_DIR` 修改运行目录。

## 配置

```json
{
  "feishu_app_id": "cli_xxx",
  "feishu_app_secret": "xxx",
  "feishu_admin_open_ids": ["ou_xxx"],
  "feishu_default_chat_id": "oc_xxx",
  "browser_headless": true,
  "browser_timeout_seconds": 10,
  "max_concurrent_accounts": 4,
  "create_order_enabled": true
}
```

## 启动

```bash
python -m piaoxingqiu_auto doctor
python -m piaoxingqiu_auto serve
```

systemd：

```ini
[Unit]
Description=Piaoxingqiu Auto
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=zhangyihe2004
WorkingDirectory=/home/zhangyihe2004/piaoxingqiu-auto
Environment=PIAOXINGQIU_AUTO_DIR=/home/zhangyihe2004/.piaoxingqiu-auto
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/zhangyihe2004/piaoxingqiu-auto/.venv/bin/python -m piaoxingqiu_auto serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 飞书指令

```text
搜索 <关键词>
抢票 <搜索序号>
列表
详情 <任务ID>
暂停 <任务ID>
恢复 <任务ID>
删除 <任务ID>
间隔 <任务ID> <秒>

登录
账号
删除账号 <账号ID>

绑定 <任务ID> <账号ID>
启动 <任务ID> <账号ID>
停止 <任务ID> <账号ID>
解绑 <任务ID> <账号ID>
```

登录和绑定配置是分步流程，发送 `取消` 即可退出。

## 工作方式

```text
任务（一个具体场次）
└── 绑定（任务 + 账号）
    ├── 票档优先级
    ├── 目标数量
    └── 观演人
```

- 手机号全局唯一；一个账号可以绑定多个任务，但同一时刻只执行一个。
- 账号共享登录资料和浏览器缓存；每个绑定独立保存票档、数量、观演人和订单状态。
- `暂停/恢复` 控制整个任务，`启动/停止` 只控制一个任务与账号的绑定。
- 删除任务或解绑会保留账号；只有 `删除账号` 才删除登录资料。
- 回流检测默认 60 秒，最低 10 秒；程序根据官方状态和开售时间自动进入预热、开售或回流阶段。

绑定流程按顺序引导选择票档、数量和票星球账号中已有的观演人。无需实名不选人；
选座数量按“张”，非选座数量按票档“份”配置；亲子票、家庭票的一份就是一个官方票档商品。
一单一证选择一位；一票一证选择与目标数量相同的人数。配置只有在发送
`完成` 后才会通过一个 SQLite 事务整体保存，之后仍需明确发送 `启动`。

选座演出优先用一个票档满足全部数量，其次允许跨票档组合；多人依次选择同排连续、
同看台紧凑和全场紧凑座位。非选座演出一单只使用一个票档，无法买齐时先购买该票档
当前能满足的最大数量。选座 `FREE_COMBO` 优先覆盖尽可能多的座位，不能组成套票的余数
使用同一基础票档的普通单张；覆盖数量相同时选择实付最低方案，同价时优先组合实例更多。
普通 `BASE` 票档的满减、满折不在本地计算，完整继承官方 `pre_order` 的计价结果；
优惠只映射到实时标记为参加活动的票档，并按票价比例精确分摊。创建请求由 booking
页面内置客户端生成动态风控头。

预售任务会在开售前初始化 booking、风控客户端、观演人、定位和静态座位缓存，但不占用
`max_concurrent_accounts`；官方放行后才申请创建并发名额。日常回流复用账号浏览器、
认证请求头和账号元数据；绑定启动后即保留 booking 热页面，命中库存后直接执行
`dynamic → pre_order → create_order`。每个账号最多保留两个任务热页面。

## 运行数据

```text
~/.piaoxingqiu-auto/
├── config.json
├── piaoxingqiu-auto.db
└── accounts/<手机号哈希>/
    ├── browser-profile/
    ├── orders/
    └── artifacts/
```

手机号不会出现在目录名中。服务日志包含库存、座位解码与评分、预下单和创建订单耗时，
Linux 可实时查看：

```bash
journalctl -u piaoxingqiu-auto -f -o cat
```

## 安全边界

- 创建订单默认由 `create_order_enabled` 总开关控制。
- 创建请求只放行一次，其他创建请求由防火墙拦截。
- `CREATED` 和 `UNKNOWN` 状态阻止重复创建。
- 座位被抢或临时票据失效时，程序刷新实时库存并重新选择。
- `28217767` 套票规则拒绝和 `33000000` 风控都会停止当前“任务 + 账号”绑定，
  不自动改方案重试，也不影响该账号的其他任务。
- 证件已购时只移除已完成的目标，剩余目标继续等待。
- 程序不会执行支付。
- 登录资料和订单保护按账号、任务分别保存。
- 停止或暂停后，无其他活动任务的浏览器会自动释放。

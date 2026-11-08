# WorkBuddy Console

轻量的 WorkBuddy 多账户控制台，支持手机和电脑。它包装未修改的
[hawklithm/workbuddy2api](https://github.com/hawklithm/workbuddy2api)，通过同一账号库提供模型 API、签到、用量账本和告警。

## 功能

- 一个账号只授权一次，分别开启 API 与签到；唯一账号库负责刷新令牌。
- 同会话固定账号，新会话可轮询、最少在途、积分最早到期或固定账号。仅在流开始前做有限故障切换，不重放已输出的响应。
- 独立客户端密钥，创建时显示一次，服务器保存摘要；支持到期和立即撤销。迁移时可保留已有密钥。
- 记录实际账户、客户端、请求思考档位、默认档位、Token、缓存命中/未命中、耗时及上游实报积分。缺失字段显示未知，0 积分保持为 0。
- 官方账号模型目录按需每 12 小时刷新，支持手动同步和明确标注的失败缓存；目录同时提供 OpenAI `data` 与 Codex `models` 列表。
- 展示余额、到期批次和未来 7 天到期积分。每天北京时间 09:00、21:00 签到，最多 5 分钟随机延迟。
- SMTP SSL/STARTTLS 告警、失败去重及恢复通知。默认不进行消耗积分的自动推理探测。
- 手动更新 LiveBench 评分或导入带来源、日期、精确模型和思考档位的评分；编程推荐优先。不同版本不做模糊匹配。性价比只使用至少 3 次本平台实测费用，免费值不会变成无穷分。

## 安装

Python 3.11+，适合 ARM64 小服务器，无需 Docker、Redis 或数据库服务。依赖安装需要 Git。

```bash
git clone https://github.com/leo-cheung-itlger/workbuddy-console.git
cd workbuddy-console
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export WB_STATE_DIR="$PWD/runtime"
export WB_PUBLIC_ORIGIN=https://console.example.com
.venv/bin/python app/initialize.py
.venv/bin/python app/launch.py
```

通过 HTTPS 反向代理访问 `/console/`，用户名 `admin`，密码为初始化时设置的密码。
在“账户管理”完成官方授权，然后在“API 密钥”创建客户端密钥。
API Base URL 为 `https://console.example.com/v1`；不要把管理页以无认证的原始 `/admin` 转发出去。

`deploy/` 提供受限 systemd 服务、签到 timer、无凭据服务状态快照及回环 Caddy 示例。
模板使用 `/opt/workbuddy-console/current/app/` 和 `/opt/workbuddy-python/venv/`；按安装位置调整。
在 `/etc/workbuddy-console.env` 设置 `.env.example` 中的部署参数。
`workbuddy-go-checkin` 的名称用于兼容旧部署，实际执行 `app/checkin_task.py`，完全不依赖 Go。
启用 `workbuddy-console-status.timer` 后控制台才显示 systemd 运行状态。
配置邮件并成功发送测试邮件后，可启用 `workbuddy-console-watchdog.timer`。独立看门狗
通过 systemd 凭据读取 SMTP 设置，在 API 进程停止时仍能通知；它只检查健康和服务状态。

## 会话与缓存

客户端应为每段对话传递稳定的 `X-Session-ID` 或 `X-Conversation-ID`。不同客户端密钥会分开会话空间。
没有显式 ID 时，使用首条用户消息的摘要作后备；网关不保存消息正文，但不同对话若开头相同可能共用后备标识。
同一会话固定账户有利于复用缓存，实际命中以上游返回为准。账号区域、端点和模型冷却独立处理。

请求记录的缓存率是上游实报的命中 Token / 可计量输入 Token；缺少可计量分母时保持未知。
榜单评分对应外部评测条件，不是对这个账号或代理服务的质量保证。

## 邮件与故障

控制台“邮件告警”配置 SMTP。密码只保存在受限服务器状态文件，读取设置只返回是否已配置。
只有管理员主动点击才发测试邮件；正常运行按被动事件报警。同类 API 异常在 5 分钟内达到 3 次后提醒，
同一事件冷却 1 小时；恢复后通知一次。429、网络失败和上游认证拒绝分别分类，不推断封号。
若整台服务器离线，服务器自身无法发信；需要独立的外部监控才能覆盖这一情况。

## 数据与升级

运行目录包含账号令牌、SMTP 配置、密码散列、会话及 SQLite 账本，必须备份到私有位置并限制权限。
本仓库不含运行数据、真实密钥、邮件设置或私人服务器清单。
旧费用数据库会原位增列，历史缺失字段不会伪造。账本保留 30 天；元数据记录失败不会中断模型输出。
升级前备份状态文件和 SQLite 一致性快照，保留上一版代码及服务配置，以便回退。

新增客户端密钥的明文只在创建时出现；原有迁移密钥可通过管理员的 CSRF 保护操作复制。
公开 API 的所有 `/v1/` 请求均要求有效密钥，控制台密码独立，Cookie 为 Secure/HttpOnly/SameSite=Strict。

## 测试

```bash
PYTHONPATH=app .venv/bin/python -m unittest discover -s tests -v
node --check app/console_assets/app.js
```

测试覆盖旧账本迁移、密钥拒绝/撤销、CSRF、会话调度与冷却、流式字节保持、缓存统计、
到期批次、告警去重和密码掩码。测试不连接真实模型、不发邮件。第二个有效账号仍需在实际部署中验证。

## 许可

本项目为 MIT。上游及评分来源说明见 [THIRD_PARTY.md](THIRD_PARTY.md)。
WorkBuddy、CodeBuddy 及模型名称属于各自权利人；本项目非官方产品。
